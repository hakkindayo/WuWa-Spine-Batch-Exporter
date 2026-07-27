# WuWa Spine Batch Exporter

A Python script that automatically extracts, reconstructs, and batch-exports Spine animations (PNG sequences and videos) from FModel-exported JSON files (SpineAtlasAsset), tailored for Wuthering Waves assets.

The browser is operated automatically in the background via Playwright (headless Chromium), so no manual browser operation is required.

---

## Features

1. **Asset Reconstruction:**
   - Reconstructs `.skel` and `.atlas` files from FModel-exported JSONs under the Character folder.
   - Groups them together with their corresponding PNG textures into character-specific folders inside the output directory.
2. **Headless Deterministic Rendering:**
   - Renders all animations for each character frame-by-frame using `spine-webgl`.
3. **Dual Video Output (via FFmpeg):**
   - **Standard Playback:** `<anim_name>.mp4` (Opaque)
   - **Transparent Background:** `<anim_name>_alpha.mov` (PNG codec + Alpha channel support)
4. **Automatic Cleanup:**
   - Automatically deletes intermediate sequence PNGs and extracted raw assets after successful conversion (keeps sequence PNGs as a fallback only if FFmpeg fails).
5. **Config Persistence:**
   - Source and destination paths are automatically saved to `wuwa_config.json` in the same directory.

---

## Prerequisites

Run the following commands in your Command Prompt (first time only):

```bash
pip install playwright numpy pillow
playwright install chromium
```
