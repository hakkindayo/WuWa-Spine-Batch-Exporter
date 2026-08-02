import base64
import gc
import http.server
import io
import json
import shutil
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

# ---------- 0. Configuration (JSON saving, automatic folder creation) ----------

CONFIG_PATH = Path(__file__).resolve().parent / "wuwa_config.json"

# Initial values if config file doesn't exist (if empty, required input on first run)
SOURCE_ROOT = Path("")
OUTPUT_ROOT = Path("")


def load_or_create_config():
    """Load source/destination from wuwa_config.json (same folder as script).
    If missing, treats it as the first run, prompts for paths directly in the console, and creates a new one.
    Auto-creates the destination folder if it does not exist."""
    global SOURCE_ROOT, OUTPUT_ROOT

    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            SOURCE_ROOT = Path(cfg["source_root"])
            OUTPUT_ROOT = Path(cfg["output_root"])
            OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            return
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"[Warning] Failed to load {CONFIG_PATH}, redoing initial setup: {e}")

    print(f"It looks like your first run. Configuration file ({CONFIG_PATH.name}) not found, please enter paths.")
    print("(Pressing Enter without input uses default values inside [ ]. Items with empty defaults are required)")

    while True:
        src_in = input(f"Path to FModel export source folder [{SOURCE_ROOT}]: ").strip().strip('"')
        if src_in:
            SOURCE_ROOT = Path(src_in)
            break
        if str(SOURCE_ROOT):
            break
        print("  -> Cannot be empty. Please enter a path.")

    while True:
        out_in = input(f"Path to destination folder [{OUTPUT_ROOT}]: ").strip().strip('"')
        if out_in:
            OUTPUT_ROOT = Path(out_in)
            break
        if str(OUTPUT_ROOT):
            break
        print("  -> Cannot be empty. Please enter a path.")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    save_config()
    print(f"Settings saved to {CONFIG_PATH}. Values from this file will be used automatically from next time.")


def save_config():
    """Save current SOURCE_ROOT/OUTPUT_ROOT to wuwa_config.json (next to script).
    Auto-creates destination folder if missing."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"source_root": str(SOURCE_ROOT), "output_root": str(OUTPUT_ROOT)},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


SPINE_WEBGL_VERSION = "4.1.*"
FPS = 30
CANVAS_DIM = 2400         # Rendering resolution (square, px)
MARGIN = 1.15             # Skeleton bounds margin multiplier
MAKE_ALPHA_MOV = True     # Also export transparent background version (*_alpha.mov, PNG codec)
SHOW_BROWSER_LOGS = False  # When True, displays all browser console.logs (slot lists, etc.).
                            # Warnings/errors are always displayed even if False. Useful to enable only when debugging.


def get_free_port() -> int:
    """Have OS assign an available port on localhost (to avoid conflicts with fixed ports)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


HTTP_PORT = None  # Free port is assigned automatically inside main()


# ---------- 1. Extraction Process ----------

DONE_MANIFEST_NAME = "_done.json"  # Completion marker placed directly under OUTPUT_ROOT (one shared file for all characters)


def done_manifest_path() -> Path:
    return OUTPUT_ROOT / DONE_MANIFEST_NAME


def load_done_manifest() -> dict:
    p = done_manifest_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_done_manifest(manifest: dict):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    done_manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def try_extract_spine_json(json_path: Path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        atlas_raw = data[0]["Properties"]["rawData"]
        skel_raw = data[1]["Properties"]["rawData"]
        if not isinstance(atlas_raw, str) or not isinstance(skel_raw, list):
            return None
        return atlas_raw.replace("\\n", "\n"), bytes(bytearray(skel_raw))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, UnicodeDecodeError, OSError):
        return None


def atlas_page_images(atlas_text: str):
    lines = atlas_text.splitlines()
    images = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or ":" in s:
            continue
        prev_blank = (i == 0) or (lines[i - 1].strip() == "")
        next_is_size = (i + 1 < len(lines)) and lines[i + 1].strip().startswith("size:")
        if prev_blank and next_is_size:
            images.append(s)
    return images


def find_png(char_dir: Path, image_name: str):
    stem = Path(image_name).stem
    for tex_dir_name in ("Textures", "textures"):
        tex_dir = char_dir / tex_dir_name
        if tex_dir.is_dir():
            hit = list(tex_dir.glob(f"{stem}.png"))
            if hit:
                return hit[0]
    hit = list(char_dir.rglob(f"{stem}.png"))
    return hit[0] if hit else None


def stage_textures(atlas_text: str, images: list, char_dir: Path, stem: str, out_dir: Path):
    """Copy texture PNGs to out_dir and rewrite page names inside the atlas.

    When multiple assets (with different stems) are mixed in the same output folder (out_dir),
    there is a possibility that texture file names are identical but contents differ.
    Directly copying with the same name could cause assets processed later to mistakenly use
    images from the ones processed earlier (image order/mix-up bug). To avoid this,
    always append `stem` to the destination file name to make it unique, and rewrite
    page name lines inside `atlas_text` to those new names as well.

    Returns: (rewritten atlas_text, list of copied Paths, list of missing original image names)
    """
    lines = atlas_text.splitlines()
    copied = []
    missing = []
    for img in images:
        src = find_png(char_dir, img)
        if src is None:
            missing.append(img)
            continue
        new_name = f"{stem}__{src.name}"
        dest = out_dir / new_name
        if not dest.exists():
            shutil.copy2(src, dest)
        copied.append(dest)
        for i, line in enumerate(lines):
            if line.strip() == img:
                lines[i] = new_name
    return "\n".join(lines), copied, missing


def extract_all(done_manifest: dict):
    """Returns: [(output folder name, skel file name, atlas file name, manifest key), ...]"""
    entries = []
    if not SOURCE_ROOT.exists():
        print(f"Not found: {SOURCE_ROOT}")
        return entries

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for json_path in SOURCE_ROOT.rglob("*.json"):
        result = try_extract_spine_json(json_path)
        if result is None:
            continue
        atlas_text, skel_bytes = result
        char_dir = json_path.parent
        stem = json_path.stem
        manifest_key = f"{char_dir.name}__{stem}"

        if manifest_key in done_manifest:
            print(f"[Skip] {manifest_key} : Completion marker exists (already processed)")
            continue

        images = atlas_page_images(atlas_text)
        out_dir = OUTPUT_ROOT / char_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)

        new_atlas_text, pngs, missing = stage_textures(atlas_text, images, char_dir, stem, out_dir)

        if not pngs:
            print(f"[Skip] {json_path.relative_to(SOURCE_ROOT)} : Texture not found ({', '.join(images)})")
            continue
        if missing:
            print(f"[Notice] {json_path.relative_to(SOURCE_ROOT)} : Missing textures {missing}")

        skel_name, atlas_name = f"{stem}.skel", f"{stem}.atlas"
        (out_dir / skel_name).write_bytes(skel_bytes)
        (out_dir / atlas_name).write_text(new_atlas_text, encoding="utf-8")

        entries.append((char_dir.name, skel_name, atlas_name, manifest_key))
        print(f"[Extraction OK] {manifest_key} : png x{len(pngs)}")

    return entries


# ---------- 2. Rendering HTML Harness (Internal use, users do not open) ----------

HARNESS_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://unpkg.com/@esotericsoftware/spine-webgl@__SPINE_VERSION__/dist/iife/spine-webgl.js"></script>
</head><body style="margin:0">
<canvas id="canvas" width="__DIM__" height="__DIM__"></canvas>
<script>
window.ready = false;
window.loadError = null;
window.animNames = [];

window.addEventListener('error', (e) => {
  window.loadError = 'window.onerror: ' + e.message;
  console.error('CAUGHT', e.message, e.filename, e.lineno);
});

if (typeof spine === 'undefined') {
  window.loadError = 'spine-webgl script did not load (CDN blocked/offline?)';
}

const canvas = document.getElementById("canvas");
const gl = canvas.getContext("webgl", {alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: true, antialias: true});
if (!gl) {
  window.loadError = 'WebGL context could not be created';
}
let renderer, assetManager;
if (gl && typeof spine !== 'undefined') {
  renderer = new spine.SceneRenderer(canvas, gl, true);
  assetManager = new spine.AssetManager(gl, "");
  assetManager.loadBinary("__SKEL__");
  assetManager.loadTextureAtlas("__ATLAS__");
}

let skeleton, animations = {};

function poll() {
  if (window.loadError) return;
  if (!assetManager) { setTimeout(poll, 30); return; }
  if (assetManager.hasErrors && assetManager.hasErrors()) {
    window.loadError = 'asset load error: ' + JSON.stringify(assetManager.getErrors());
    return;
  }
  if (assetManager.isLoadingComplete()) {
    const atlas = assetManager.get("__ATLAS__");
    const atlasLoader = new spine.AtlasAttachmentLoader(atlas);
    const skeletonBinary = new spine.SkeletonBinary(atlasLoader);
    const skeletonData = skeletonBinary.readSkeletonData(assetManager.get("__SKEL__"));
    skeleton = new spine.Skeleton(skeletonData);
    skeletonData.animations.forEach(a => { animations[a.name] = a; window.animNames.push(a.name); });

    skeleton.setToSetupPose();
    skeleton.updateWorldTransform();

    // For debugging: List rendering order (slot order), attachments, and blend modes.
    // Used to identify which slot is responsible when weird white plates or artifacts appear.
    console.log('SLOTS(draw order): ' + skeleton.slots.map((s, i) => {
      const att = s.getAttachment();
      return `#${i}:${s.data.name}=[${att ? att.name : 'null'}]/blend=${s.data.blendMode}`;
    }).join(' | '));

    window.ready = true;
  } else {
    setTimeout(poll, 30);
  }
}
if (gl && typeof spine !== 'undefined') poll();

// Placeholder slots such as "counters/data displays" dynamically injected inside the game
// can sometimes render as empty (e.g., white plates) and stand out when exported as still frames.
// Slots whose names contain any of these keywords are forcefully hidden whenever animations are applied.
const HIDE_SLOT_KEYWORDS = ['数字', '数据', 'shuju'];
// Diagnostic flag: When true, hides all slots with Screen blend mode (=3).
// A flag to isolate whether the cause of "white plates" that aren't removed by keyword specification
// stems from Screen composition in general. Once the cause is identified, it should ideally be reverted to individual slot name specifications.
const DEBUG_HIDE_ALL_SCREEN_BLEND = true;
function hideDebugSlots() {
  skeleton.slots.forEach(s => {
    const nameHit = HIDE_SLOT_KEYWORDS.some(kw => s.data.name.includes(kw));
    const screenHit = DEBUG_HIDE_ALL_SCREEN_BLEND && s.data.blendMode === 3;
    if (nameHit || screenHit) {
      s.setAttachment(null);
    }
  });
}

window.renderFrame = function(animName, time, bgR, bgG, bgB) {
  // bgR/bgG/bgB are background colors from 0 to 1. Additive/Screen blend VFX do not blend correctly
  // on a transparent canvas (colors and opacity get corrupted), so they are always rendered on an opaque background.
  // If transparency is needed, render once with a black background and once with a white background, then calculate alpha from the difference
  // (refer to compose_alpha_from_black_white on the Python side).
  const anim = animations[animName];
  skeleton.setToSetupPose();
  anim.apply(skeleton, 0, time, false, null, 1, spine.MixBlend.setup, spine.MixDirection.mixIn);
  hideDebugSlots();
  skeleton.updateWorldTransform();
  gl.clearColor(bgR, bgG, bgB, 1);
  gl.clear(gl.COLOR_BUFFER_BIT);
  renderer.begin();
  renderer.drawSkeleton(skeleton, false);
  renderer.end();
  return canvas.toDataURL("image/png");
};

// Optimization: Process a range of frames together in a single browser invocation.
// Going back and forth between Python <-> browser for each frame piles up communication overhead
// by frame count x 2 (black background + white background), becoming a dominant bottleneck.
// This processes a specified range of frames in batch and returns them as an array.
// Intermediate captures are kept as PNG (lossless) because JPEG compression noise caused
// low-alpha determinations to flicker per frame, resulting in transparent video blinking bugs.
// If needAlpha is false, white background rendering is entirely skipped (speed optimization when transparent mov is not needed.
// In this case, the black background rendering results are used as-is for opacity).
window.renderFrameBatch = function(animName, startFrame, count, fps, needAlpha) {
  const anim = animations[animName];
  const blackUrls = [];
  const whiteUrls = [];
  for (let k = 0; k < count; k++) {
    const t = (startFrame + k) / fps;
    skeleton.setToSetupPose();
    anim.apply(skeleton, 0, t, false, null, 1, spine.MixBlend.setup, spine.MixDirection.mixIn);
    hideDebugSlots();
    skeleton.updateWorldTransform();

    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    renderer.begin();
    renderer.drawSkeleton(skeleton, false);
    renderer.end();
    blackUrls.push(canvas.toDataURL("image/png"));

    if (needAlpha) {
      gl.clearColor(1, 1, 1, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      renderer.begin();
      renderer.drawSkeleton(skeleton, false);
      renderer.end();
      whiteUrls.push(canvas.toDataURL("image/png"));
    }
  }
  return { black: blackUrls, white: whiteUrls };
};

window.getAnimDuration = function(animName) {
  return animations[animName].duration;
};

// Sample the entire animation (multiple frames) to find the total bounds range,
// and re-align the camera for that animation. If the camera is fixed looking only at the setup pose,
// it will misalign with the actual rendering position during large movements like VFX.
window.fitCameraToAnim = function(animName, samples) {
  const anim = animations[animName];
  const n = Math.max(1, samples || 20);
  const offset = new spine.Vector2(), size = new spine.Vector2();
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let i = 0; i <= n; i++) {
    const t = (anim.duration * i) / n;
    skeleton.setToSetupPose();
    anim.apply(skeleton, 0, t, false, null, 1, spine.MixBlend.setup, spine.MixDirection.mixIn);
    hideDebugSlots();
    skeleton.updateWorldTransform();
    skeleton.getBounds(offset, size, []);
    minX = Math.min(minX, offset.x);
    minY = Math.min(minY, offset.y);
    maxX = Math.max(maxX, offset.x + size.x);
    maxY = Math.max(maxY, offset.y + size.y);
  }
  const width = maxX - minX, height = maxY - minY;
  renderer.camera.position.set(minX + width / 2, minY + height / 2, 0);
  const squareDim = Math.max(width, height) * __MARGIN__;
  renderer.camera.viewportWidth = squareDim;
  renderer.camera.viewportHeight = squareDim;
  renderer.camera.update();
};
</script>
</body></html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def serve_output_root(port: int):
    def handler(*a, **kw):
        return QuietHandler(*a, directory=str(OUTPUT_ROOT), **kw)
    httpd = socketserver.TCPServer(("localhost", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def decode_frame_rgb(data_url: str) -> np.ndarray:
    """Decode canvas.toDataURL() PNG data URL into an RGB float32 array"""
    png_bytes = base64.b64decode(data_url.split(",", 1)[1])
    return np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.float32)


def compose_alpha_from_black_white(black_arr: np.ndarray, white_arr: np.ndarray) -> Image.Image:
    """Restore the original alpha value and colors from two rendering results on black/white backgrounds (difference matte).

    VFX such as screen/additive blending do not blend correctly when drawn directly on a transparent canvas
    (colors and opacity collapse), so they are always rendered twice on opaque backgrounds and reverse-calculated from the difference.
    Assuming "over" composition: white - black = (1 - alpha) (difference in background color contribution),
    so alpha = 1 - (white - black), and straight_color = black / alpha is used for restoration.

    However, this division is vulnerable to noise in regions where alpha is very low (barely visible),
    causing parts that should be nearly transparent and invisible to have only their colors abnormally amplified,
    resulting in weird color spots/bleeding. To prevent this:
      - Place a cap on color amplification (alphas lower than ALPHA_FLOOR do not amplify colors any further)
      - Treat even lower alphas (less than ALPHA_CUTOFF) as completely transparent
    """
    ALPHA_FLOOR = 48.0   # Cap color amplification factor at this value for alphas lower than this
    ALPHA_CUTOFF = 40.0  # Treat alphas lower than this as noise and make them fully transparent (alpha=0)

    diff = white_arr - black_arr
    alpha = 255.0 - np.clip(diff.max(axis=2), 0.0, 255.0)
    alpha_safe = np.maximum(alpha, ALPHA_FLOOR)
    color = np.clip(black_arr * 255.0 / alpha_safe[..., None], 0.0, 255.0)
    alpha = np.where(alpha < ALPHA_CUTOFF, 0.0, alpha)
    rgba = np.dstack([color, alpha]).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def opaque_rgba_from_black(black_arr: np.ndarray) -> Image.Image:
    """For cases where transparency is not needed (MAKE_ALPHA_MOV=False): Skips white background rendering,
    and uses the black background rendering result as-is as an opaque image with alpha 255."""
    alpha = np.full(black_arr.shape[:2], 255.0, dtype=np.float32)
    rgba = np.dstack([black_arr, alpha]).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def cleanup_extracted_assets(out_dir: Path):
    """Delete extracted .skel/.atlas/texture PNGs used for rendering (no longer needed once mp4/mov are generated).
    Sequential PNG folders (_tmp_frames_*) left behind due to ffmpeg conversion failures are kept as-is as fallback."""
    for item in out_dir.iterdir():
        if item.is_dir():
            continue  # _tmp_frames_* are fallbacks on failure, do not delete
        if item.suffix.lower() in (".skel", ".atlas", ".png"):
            item.unlink(missing_ok=True)


def encode_outputs(rgba_pattern: str, out_dir: Path, name: str, canvas_dim: int):
    """Export both <name>.mp4 (composited on black background, opaque) for normal playback and <name>_alpha.mov
    (PNG codec + alpha, only when MAKE_ALPHA_MOV is enabled) from a single type of rgba_pattern (RGBA sequential PNGs restored via difference matte).

    Previously, a separate sequence (flat_) rendered directly on a black background was saved for mp4,
    but since it was verified to yield numerically identical results to "overlaying RGBA on top of a black background via filter before converting to yuv420p",
    it was streamlined to rely on a single frame_ (RGBA) type to cover both (reducing saved files and simplifying).

    Transparency was initially tried with VP9 (webm), but it was confirmed that alpha channels failed to decode correctly
    in many players like VLC, Discord, and YMM4. It was changed to the more reliable .mov with PNG codec,
    but since PNG requires decompressing frame-by-frame causing high load on the playback software end and leading to stuttering/flicking,
    it was changed to ProRes 4444, which supports proper video compression and alpha.
    The mp4 side uses straightforward yuv420p + simple settings to avoid color shifts
    (yuv444p + full range settings were tested, but conversely caused colors to change in some playback environments, so it was reverted).

    Returns: (mp4_ok, mov_ok). Success is determined by file size and ffmpeg exit code
    (checking only existence would treat corrupted/unplayable files as successful).
    """
    mp4_path = out_dir / f"{name}.mp4"
    r1 = subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=black:s={canvas_dim}x{canvas_dim}:r={FPS}",
        "-framerate", str(FPS), "-i", rgba_pattern,
        "-filter_complex", "[0:v][1:v]overlay=shortest=1:format=auto,format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-crf", "18", "-preset", "medium",
        "-movflags", "+faststart",
        str(mp4_path)
    ], check=False, capture_output=True)
    mp4_ok = mp4_path.exists() and mp4_path.stat().st_size > 1024 and r1.returncode == 0
    if not mp4_ok:
        mp4_path.unlink(missing_ok=True)
        print(f"    [ffmpeg mp4 Error] {name} : {r1.stderr.decode(errors='ignore')[-800:]}")

    mov_ok = False
    if MAKE_ALPHA_MOV:
        mov_path = out_dir / f"{name}_alpha.mov"
        r2 = subprocess.run([
            "ffmpeg", "-y", "-framerate", str(FPS),
            "-i", rgba_pattern,
            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
            str(mov_path)
        ], check=False, capture_output=True)
        mov_ok = mov_path.exists() and mov_path.stat().st_size > 1024 and r2.returncode == 0
        if not mov_ok:
            mov_path.unlink(missing_ok=True)
            print(f"    [ffmpeg mov (transparent) Error] {name} : {r2.stderr.decode(errors='ignore')[-800:]}")

    return mp4_ok, mov_ok


def render_character(page, char_dir_name, skel_name, atlas_name):
    """Returns: True if export succeeds for all animations"""
    out_dir = OUTPUT_ROOT / char_dir_name
    harness_path = out_dir / "_harness.html"
    html = (HARNESS_TEMPLATE
            .replace("__SPINE_VERSION__", SPINE_WEBGL_VERSION)
            .replace("__SKEL__", skel_name)
            .replace("__ATLAS__", atlas_name)
            .replace("__DIM__", str(CANVAS_DIM))
            .replace("__MARGIN__", str(MARGIN)))
    harness_path.write_text(html, encoding="utf-8")

    page.goto(f"http://localhost:{HTTP_PORT}/{char_dir_name}/_harness.html")
    try:
        page.wait_for_function(
            "() => window.ready === true || window.loadError !== null",
            timeout=600000,
        )
    except Exception:
        load_error = page.evaluate("window.loadError")
        raise RuntimeError(f"Load timeout (10 minutes). window.loadError={load_error!r}")

    load_error = page.evaluate("window.loadError")
    if load_error:
        raise RuntimeError(f"Load failed: {load_error}")

    anim_names = page.evaluate("window.animNames")
    all_ok = True

    for anim in anim_names:
        duration = page.evaluate("(a) => window.getAnimDuration(a)", anim)
        if not duration or duration <= 0:
            continue
        # Re-align camera to match movement of this entire animation (mitigates positional shifts)
        page.evaluate("([a, s]) => window.fitCameraToAnim(a, s)", [anim, 20])
        n_frames = max(1, int(duration * FPS))
        tmp_frame_dir = out_dir / f"_tmp_frames_{anim}"
        tmp_frame_dir.mkdir(parents=True, exist_ok=True)

        BATCH_SIZE = 20  # Unit of frames sent to browser together (speeds up by reducing roundtrips)
        idx = 0
        t_start = time.time()
        for start in range(0, n_frames, BATCH_SIZE):
            count = min(BATCH_SIZE, n_frames - start)
            result = page.evaluate(
                "([a, s, c, fps, na]) => window.renderFrameBatch(a, s, c, fps, na)",
                [anim, start, count, FPS, MAKE_ALPHA_MOV]
            )
            for k in range(count):
                black_arr = decode_frame_rgb(result["black"][k])
                if MAKE_ALPHA_MOV:
                    white_arr = decode_frame_rgb(result["white"][k])
                    rgba_img = compose_alpha_from_black_white(black_arr, white_arr)
                    del white_arr
                else:
                    rgba_img = opaque_rgba_from_black(black_arr)
                rgba_img.save(tmp_frame_dir / f"frame_{idx:04d}.png")
                del black_arr, rgba_img
                idx += 1
            del result
            gc.collect()

            elapsed = time.time() - t_start
            pct = idx / n_frames * 100
            eta = (elapsed / idx) * (n_frames - idx) if idx > 0 else 0
            print(f"    [Progress] {char_dir_name} / {anim} : {idx}/{n_frames} frames "
                  f"({pct:.0f}%) Elapsed {elapsed:.0f}s Remaining ~{eta:.0f}s", flush=True)

        if not shutil.which("ffmpeg"):
            print(f"[Warning] {char_dir_name} / {anim} : ffmpeg not found, leaving as sequential PNGs -> {tmp_frame_dir}")
            all_ok = False
            continue

        mp4_ok, mov_ok = encode_outputs(
            str(tmp_frame_dir / "frame_%04d.png"), out_dir, anim, CANVAS_DIM
        )

        if mp4_ok:
            print(f"[Export OK] {char_dir_name} / {anim} : {out_dir / (anim + '.mp4')}"
                  + (f" / {out_dir / (anim + '_alpha.mov')}" if mov_ok else ""))
            shutil.rmtree(tmp_frame_dir, ignore_errors=True)
        else:
            print(f"[Error] {char_dir_name} / {anim} : mp4 conversion failed, leaving as sequential PNGs -> {tmp_frame_dir}")
            all_ok = False

    harness_path.unlink(missing_ok=True)
    cleanup_extracted_assets(out_dir)
    print(f"    [Cleanup] {char_dir_name} : Deleted extracted skel/atlas/texture PNGs (leaving mp4/mov only)")

    return all_ok


BROWSER_RESTART_EVERY = 2   # Restart browser to release memory every this many characters processed


def new_browser_and_page(p):
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": CANVAS_DIM, "height": CANVAS_DIM})
    page.on("console", lambda msg: (
        print(f"    [Browser console] {msg.type}: {msg.text}")
        if SHOW_BROWSER_LOGS or msg.type in ("warning", "error")
        else None
    ))
    page.on("pageerror", lambda exc: print(f"    [Browser pageerror] {exc}"))
    return browser, page


def main():
    load_or_create_config()
    done_manifest = load_done_manifest()

    entries = extract_all(done_manifest)
    if not entries:
        print("No convertible characters found (or all already processed)")
        return

    global HTTP_PORT
    HTTP_PORT = get_free_port()
    httpd = serve_output_root(HTTP_PORT)
    try:
        with sync_playwright() as p:
            browser, page = new_browser_and_page(p)
            for i, (char_dir_name, skel_name, atlas_name, manifest_key) in enumerate(entries):
                if i > 0 and i % BROWSER_RESTART_EVERY == 0:
                    print(f"    [Maintenance] Processed {BROWSER_RESTART_EVERY} characters, restarting browser to free memory")
                    browser.close()
                    gc.collect()
                    browser, page = new_browser_and_page(p)
                try:
                    ok = render_character(page, char_dir_name, skel_name, atlas_name)
                    if ok:
                        done_manifest[manifest_key] = {
                            "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        save_done_manifest(done_manifest)
                    else:
                        print(f"    [Notice] {char_dir_name} : Some animations failed, completion marker not created (will be processed again next time)")
                except Exception as e:
                    print(f"[Error] {char_dir_name} : {e}")
            browser.close()
    finally:
        httpd.shutdown()

    print(f"\nDone. <anim>.mp4 (normal) and <anim>_alpha.mov (transparent background) "
          f"written under {OUTPUT_ROOT} for each character.")


if __name__ == "__main__":
    main()
