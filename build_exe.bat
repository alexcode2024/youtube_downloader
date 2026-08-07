@echo off
REM ============================================================
REM  YouTube 视频下载器 - PyInstaller 打包脚本
REM  生成 dist\YouTubeDownloader.exe（单文件，双击即可运行）
REM ============================================================
chcp 65001 >nul
echo.
echo [1/2] 检查依赖...
python -m pip install --upgrade pip >nul
REM curl_cffi 必须限制 <0.16：yt-dlp 仅支持 0.10.x~0.15.x，0.16+ 不被识别
python -m pip install PyQt6 "yt-dlp>=2024" "curl_cffi>=0.10,<0.16" pyinstaller

echo.
echo [2/2] 开始打包（生成单文件 exe）...
echo 这可能需要几分钟，请耐心等待...
echo.

pyinstaller --noconfirm --onefile --windowed ^
  --name "YouTubeDownloader" ^
  --icon "app.ico" ^
  --add-data "app.ico;." ^
  --collect-all yt_dlp ^
  --exclude PySide6 ^
  --exclude PySide6_Essentials ^
  app.py

echo.
if exist "dist\YouTubeDownloader.exe" (
    echo ============================================================
    echo  打包成功！
    echo  输出文件: dist\YouTubeDownloader.exe
    echo ============================================================
    echo.
    echo 是否立即打开 dist 目录？
    choice /c YN /m "Y=打开, N=不打开"
    if errorlevel 2 goto :end
    explorer "dist"
) else (
    echo ============================================================
    echo  打包失败，请检查上方的错误信息。
    echo ============================================================
)

:end
pause
