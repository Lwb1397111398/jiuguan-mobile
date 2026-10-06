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
set "PATH=E:\engine\flutter\3.44.8\bin;%PATH%"
rem Pub 缓存放全英文路径：CMake 生成构建配置时对中文用户名路径会用 GBK 写 JSON，
rem gradle 按 UTF-8 解析报错（jni 插件 buildCMake 失败的根因）
set "PUB_CACHE=E:\pub-cache"
set "APK=%~dp0build\flutter\build\app\outputs\apk\release\app-release.apk"

set "MISSING="
if not exist "%ANDROID_SDK_ROOT%" set "MISSING=%MISSING% AndroidSDK"
if not exist "%JAVA_HOME%\bin\java.exe" set "MISSING=%MISSING% JDK"
if not exist "E:\engine\flutter\3.44.8\bin\flutter.bat" set "MISSING=%MISSING% Flutter"
if not exist "%VENV%\Scripts\flet.exe" set "MISSING=%MISSING% Flet"
if not exist "%USERPROFILE%\.gradle\wrapper\dists\gradle-8.14-all\c2qonpi39x1mddn7hk5gh9iqj\gradle-8.14\bin\gradle.bat" set "MISSING=%MISSING% Gradle8.14"
if defined MISSING (
  echo [失败] 缺少构建依赖：%MISSING%
  echo        请把这一行截图给 AI。
  pause
  exit /b 1
)

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
