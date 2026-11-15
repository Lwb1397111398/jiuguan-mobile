@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "REPO=E:\AI Agent\work area\酒馆小说生成器"
set "VENV=%REPO%\.venv"

echo [1/3] 同步 core 到手机工程...
"%VENV%\Scripts\python" sync_core.py
if errorlevel 1 (
  echo.
  echo [失败] core 同步失败 —— 请把上面报错截图给 AI。
  pause
  exit /b 1
)

echo [2/3] 检查构建环境...
set "ANDROID_SDK_ROOT=E:\engine\androidstudioSDK"
set "ANDROID_HOME=E:\engine\androidstudioSDK"
set "JAVA_HOME=C:\Program Files\Android\Android Studio\jbr"
rem flet pins Flutter 3.44.8; SDK lives on ASCII path E:\flutter-3.44.8 (managed copy moved out of Chinese-username home, 2026-11-15)
set "PATH=E:\flutter-3.44.8\bin;%PATH%"
rem PUB_CACHE on pure-ASCII path: CMake writes GBK JSON for Chinese user-name
rem paths and gradle fails to parse it (root cause of jni buildCMake errors)
set "PUB_CACHE=E:\pubcache"
rem China mirrors for pub.dev and flutter storage
set "PUB_HOSTED_URL=https://pub.flutter-io.cn"
set "FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn"
rem Gradle user home relocated to E drive; wrapper auto-downloads missing dists
set "GRADLE_USER_HOME=E:\engine\gradle-home"
set "APK=%~dp0build\flutter\build\app\outputs\apk\release\app-release.apk"

set "MISSING="
if not exist "%ANDROID_SDK_ROOT%" set "MISSING=%MISSING% AndroidSDK"
if not exist "%JAVA_HOME%\bin\java.exe" set "MISSING=%MISSING% JDK"
if not exist "E:\flutter-3.44.8\bin\flutter.bat" set "MISSING=%MISSING% Flutter"
if not exist "%VENV%\Scripts\flet.exe" set "MISSING=%MISSING% Flet"
rem gradle dist version depends on flet template; only hint when nothing cached
set "GRADLE_DISTS=%GRADLE_USER_HOME%\wrapper\dists"
if not exist "%GRADLE_DISTS%" set "GRADLE_DISTS=%USERPROFILE%\.gradle\wrapper\dists"
if not exist "%GRADLE_DISTS%" echo [提示] 尚无 Gradle 缓存，首次构建将自动下载（需网络）。

echo [3/3] 开始构建 APK（10~25 分钟，不要关闭这个窗口）...
"%VENV%\Scripts\flet.exe" build apk --yes %*
if errorlevel 1 (
  echo.
  echo [失败] APK 构建失败 —— 请把上面的报错截图给 AI。
  pause
  exit /b 1
)

echo.
echo ================================================
echo  构建成功！APK 位置：
echo  %APK%
echo ================================================
if exist "%APK%" start "" explorer /select,"%APK%"
pause
exit /b 0
