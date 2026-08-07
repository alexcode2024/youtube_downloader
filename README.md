# YouTube 视频下载器

一个带图形界面的 YouTube 视频下载器，支持检测可用分辨率、选择画质下载，并提供「仅音频 MP3」模式。

基于 PyQt6 + yt-dlp 开发，可打包为 Windows 单文件 exe，双击即用。

## 功能

- 输入 YouTube 链接，点击「检测分辨率」查看该视频提供的所有画质
- 选择画质（best / 1080p / 720p / 480p / ...）与格式（mp4 / webm / mkv）
- 仅音频模式：直接下载音频流并转换为 MP3（192kbps）
- 实时进度条 + 阶段提示（正在下载视频流 / 音频流 / 合并 / 转换为 MP3）
- 可随时取消下载
- 自动剥离播放列表参数，只下载单个视频
- 网络异常（SSL / 403）自动重试

## 下载使用

从 [Releases](https://github.com/alexcode2024/youtube_downloader/releases/latest) 下载 `YouTubeDownloader.exe`，双击运行即可。

> **运行前提（重要）**：系统需安装以下工具并加入 PATH：
> - **[Node.js](https://nodejs.org/)** —— *必需*。新版 yt-dlp 解析 YouTube 签名脚本需要它，缺失会导致下载失败（403）。
> - **[FFmpeg](https://ffmpeg.org/download.html)** —— *推荐*。合并高画质视频流、转换 MP3 需要它。缺失时会自动降级为单流下载。

## 从源码运行

```bash
pip install -r requirements.txt
python app.py
```

## 打包为 exe

```bash
build_exe.bat
```

生成的 exe 在 `dist/YouTubeDownloader.exe`。

> 注意：`curl_cffi` 版本必须为 `0.10.x ~ 0.15.x`，0.16+ 不被 yt-dlp 支持（见 requirements.txt 的版本约束）。

## 项目结构

```
├── app.py              # PyQt6 GUI 主程序
├── downloader.py       # 下载 / 检测线程（封装 yt-dlp）
├── scripts/download_video.py  # 原始命令行脚本
├── requirements.txt    # 依赖清单
├── build_exe.bat       # PyInstaller 打包脚本
└── SKILL.md            # 命令行脚本说明
```

## 技术要点

- **后台线程下载**：`QThread` + yt-dlp Python API + `progress_hooks`，界面不卡顿
- **多流进度归一化**：合并格式会分别下载视频流和音频流，进度按流数归一化避免回跳
- **QThread 子进程死锁修复**：yt-dlp 调用 node 子进程时，在 worker 线程重定向 OS 层 stdout/stderr 到 NUL，避免管道死锁
- **URL 规范化**：自动剥离 `list=` / `start_radio=` 等播放列表参数
- **快速取消**：progress_hook 检查取消标志，抛异常中断下载循环

## 致谢

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 核心下载引擎
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) —— GUI 框架
