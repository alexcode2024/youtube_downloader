# YouTube 视频下载器 / YouTube Video Downloader

一个带图形界面的 YouTube 视频下载器，支持批量下载、字幕嵌入、多语言界面。

A YouTube video downloader with a GUI. Supports batch download, subtitle embedding, and a multilingual interface (Chinese / English).

基于 PyQt6 + yt-dlp 开发，可打包为 Windows 单文件 exe，双击即用。

Built with PyQt6 + yt-dlp. Ships as a single-file Windows exe — just double-click.

---

## 功能 / Features

- **批量下载**：URL 框支持多行，每行一个链接，依次下载 / **Batch download**: paste multiple URLs (one per line), download sequentially
- **检测分辨率**：点击「检测分辨率」查看可用画质 + 估算体积（如 `1080p (148.0 MB)`）/ **Detect quality**: see available resolutions with estimated size
- **字幕嵌入**：勾选「嵌入字幕」选择语言，字幕烧进视频（支持自动+人工字幕）/ **Subtitle embedding**: burn subtitles into video (auto + manual)
- **仅音频 MP3**：直接下载音频流并转码为 192kbps MP3 / **Audio-only MP3**: download audio stream, convert to 192kbps MP3
- **多语言界面**：中文 / English 实时切换，记忆偏好 / **Multilingual UI**: switch Chinese / English on the fly, preference saved
- **实时进度**：进度条 + 阶段提示（视频流/音频流/合并/转换）+ 速度 ETA / **Live progress**: progress bar + stage hints + speed ETA
- **可取消**：随时取消，响应迅速 / **Cancellable**: cancel anytime, fast response

## 下载使用 / Download & Use

从 [Releases](https://github.com/alexcode2024/youtube_downloader/releases/latest) 下载 `YouTubeDownloader.exe`，双击运行。

Download `YouTubeDownloader.exe` from [Releases](https://github.com/alexcode2024/youtube_downloader/releases/latest) and double-click to run.

> **运行前提 / Prerequisites**（需加入系统 PATH / must be in PATH）：
> - **[Node.js](https://nodejs.org/)** —— *必需 / required*。yt-dlp 解析 YouTube 签名需要它，缺失会导致 403。Needed to solve YouTube signature challenges; without it downloads fail with 403.
> - **[FFmpeg](https://ffmpeg.org/download.html)** —— *推荐 / recommended*。合并高画质流、转换 MP3、嵌入字幕需要。For merging streams, MP3 conversion, and subtitle embedding.

## 从源码运行 / Run from Source

```bash
pip install -r requirements.txt
python app.py
```

## 打包为 exe / Build exe

```bash
build_exe.bat
```

生成的 exe 在 `dist/YouTubeDownloader.exe`。

Output: `dist/YouTubeDownloader.exe`.

> 注意：`curl_cffi` 版本必须为 `0.10.x ~ 0.15.x`，0.16+ 不被 yt-dlp 支持。
> Note: `curl_cffi` must be `0.10.x ~ 0.15.x`; 0.16+ is not supported by yt-dlp.

## 项目结构 / Project Structure

```
├── app.py              # PyQt6 GUI 主程序 / GUI entry
├── downloader.py       # 下载/检测线程（封装 yt-dlp）/ download & probe workers
├── i18n.py             # 多语言翻译表 / translations
├── app.ico             # 程序图标 / app icon
├── scripts/download_video.py  # 原始命令行脚本 / original CLI script
├── requirements.txt    # 依赖清单 / dependencies
├── build_exe.bat       # PyInstaller 打包脚本 / build script
└── SKILL.md            # 命令行脚本说明 / CLI docs
```

## 技术要点 / Technical Notes

- **后台线程下载**：`QThread` + yt-dlp Python API + `progress_hooks`，界面不卡顿 / Background-thread download, non-blocking UI
- **多流进度归一化**：合并格式分别下载视频流和音频流，进度按流数归一化避免回跳 / Multi-stream progress normalization
- **QThread 子进程死锁修复**：worker 线程重定向 OS 层 stdout/stderr 到 NUL，避免 node/ffmpeg 管道死锁 / Fix node/ffmpeg subprocess deadlock in QThread
- **URL 规范化**：自动剥离 `list=` / `start_radio=` 等播放列表参数 / Auto-strip playlist params
- **快速取消**：progress_hook 检查取消标志，抛异常中断下载循环 / Fast cancel via progress_hook exception
- **SSL 容错**：外层重试 5 次，缓解 YouTube TLS 限流 / Outer retry for TLS throttling

## 致谢 / Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 核心下载引擎 / core download engine
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) —— GUI 框架 / GUI framework
