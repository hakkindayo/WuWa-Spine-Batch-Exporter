# List of Known Issues (Confirmed)
**1. Videos with heavy visual effects may flicker significantly when exported as `.mov`. (`.mp4` output does **not** flicker, but it is exported with a black background.)**
   **Note:** This appears to be an issue with video players such as VLC, rather than the exporter itself.

**2. Processing multiple items at once may cause the extraction process to fail or produce incorrect results.**
   Running the extraction again may fix the issue.

# Not a Bug
The slow processing speed is a known limitation and is not considered a bug.
If I find a way to improve the performance in the future, I'll update the code accordingly.

# バグが起きるもの一覧(確認したもので)
**1.エフェクトが激しいものが`.mov`で映像で出力した場合激しく点滅する可能性があります！(`mp4`は黒背景だけどし点滅することはありません)**
※これはVLCとかの動画再生ソフトの問題のようです。

**2.一気にやると処理が正しく行われなくなります**
これは再度実行すると直る可能性があります！

# バグではありませんが
処理が遅いのはどうしようもありません(処理が早くできるようになったらコードを書き換えておきます)
