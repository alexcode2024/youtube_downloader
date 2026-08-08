# YouTube Video Downloader

English | [中文](README.md)

A YouTube video downloader with a GUI. Supports batch download, subtitle embedding, and a multilingual interface (Chinese / English).

Built with PyQt6 + yt-dlp. Ships as a single-file Windows exe — just double-click.

## Features

- **Batch download**: paste multiple URLs (one per line), download sequentially
- **Detect quality**: click "Detect Quality" to see available resolutions with estimated size (e.g. `1080p (148.0 MB)`)
- **Subtitle embedding**: check "Embed subtitle" and pick a language; subtitles are burned into the video (auto + manual)
- **Audio-only MP3**: download the audio stream directly and convert to 192kbps MP3
- **Multilingual UI**: switch between Chinese / English on the fly; preference is saved
- **Live progress**: progress bar + stage hints (video stream / audio stream / merging / converting) + speed & ETA
- **Cancellable**: cancel anytime with a fast response

## Download & Use

Download `YouTubeDownloader.exe` from [Releases](https://github.com/alexcode2024/youtube_downloader/releases/latest) and double-click to run.

> **Prerequisites** (must be in system PATH):
> - **[Node.js](https://nodejs.org/)** — *required*. Needed to solve YouTube signature challenges; without it downloads fail with 403.
> - **[FFmpeg](https://ffmpeg.org/download.html)** — *recommended*. For merging streams, MP3 conversion, and subtitle embedding.

## Run from Source

```bash
pip install -r requirements.txt
python app.py
```

## Build exe

```bash
build_exe.bat
```

Output: `dist/YouTubeDownloader.exe`.

> Note: `curl_cffi` must be `0.10.x ~ 0.15.x`; 0.16+ is not supported by yt-dlp.

## Project Structure

```
├── app.py                      # PyQt6 GUI entry
├── downloader.py               # download & probe workers (wraps yt-dlp)
├── i18n.py                     # translation table
├── app.ico                     # app icon
├── scripts/download_video.py   # original CLI script
├── requirements.txt            # dependencies
├── build_exe.bat               # PyInstaller build script
└── SKILL.md                    # CLI docs
```

## Technical Notes

- **Background-thread download**: `QThread` + yt-dlp Python API + `progress_hooks`, non-blocking UI
- **Multi-stream progress normalization**: merged formats download video and audio streams separately; progress is normalized across streams to avoid jumping back
- **QThread subprocess deadlock fix**: worker thread redirects OS-level stdout/stderr to NUL to avoid node/ffmpeg pipe deadlocks
- **URL normalization**: auto-strips `list=` / `start_radio=` playlist params
- **Fast cancel**: progress_hook checks a cancel flag and raises to abort the download loop
- **SSL tolerance**: outer retry (5 attempts) mitigates YouTube TLS throttling

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — core download engine
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) — GUI framework
