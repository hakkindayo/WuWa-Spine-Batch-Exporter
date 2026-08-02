# About Image Quality When Zooming
* You can improve the image quality by increasing the `CANVAS_DIM` value.
  > **Note:** This will also increase the processing time before the output is generated.
As a test, a separate folder will be created, and the modified output/code will be placed there.

# Known Issues (Confirmed)
**1. Processing multiple items at once may become very slow or appear to stop progressing.**
Running the extraction again may resolve the issue. In some cases, the process is still running in the background, but it may take a long time to complete.

# `.mov` Videos May Flicker or Appear Transparent in Some Video Players (e.g. VLC)
**Videos with heavy visual effects may flicker significantly when exported as `.mov`. (`.mp4` output does not have this issue.)**
> **Note:** This appears to be an issue with video players such as VLC rather than the exporter itself.

# Not a Bug
Slow processing is currently unavoidable. If I find a way to improve the processing speed in the future, I'll update the code accordingly.

# ズームした際の画質の粗さについて
・`CANVAS_DIM`を変更することで画質は良くなります！
> ※ですが出力されるまで時間が更に掛かるようになりました
これはテストとしてフォルダーを作成しそこにコードが追加されます

# バグが起きるもの一覧(確認したもので)
**1.一気にやると処理が遅くなって進行しなくなる可能性があります！**
これは再度実行すると直る可能性があるのと、時間がかかりますが一応動いてる可能性があります！

# 動画再生ソフト(VLCなど)で`.mov`形式の動画を見た際に点滅したり透けてたりする可能性があります
**.エフェクトが激しいものが`.mov`で映像で出力した場合激しく点滅する可能性があります！(`mp4`は大丈夫です)**
> ※これはVLCとかの動画再生ソフトの問題のようです。

# バグではありませんが
処理が遅いのはどうしようもありません(処理が早くできるようになったらコードを書き換えておきます)
