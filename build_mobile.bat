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
rem 版本号 = git 提交数（与 CI 对同一提交算出同一个号，每推一次自动 +1）；
rem App 内检查更新靠 versionCode 比较新旧，必须与本机/CI 一致
for /f %%i in ('git rev-list --count HEAD') do set "BUILD_NUM=%%i"
if not defined BUILD_NUM (
  echo [警告] 取不到 git 提交数，回退 build-number 1（检查更新会比较不准）
  set "BUILD_NUM=1"
)
echo 版本号：0.1.%BUILD_NUM% (build %BUILD_NUM%)
rem 统一签名：keystore 已入库（android/keystore/app.keystore，口令 android /
rem alias androiddebugkey，即本机历史 debug 密钥——与手机上已装包同签名，
rem 覆盖安装不被系统拒绝）。CI 传同一组参数，两边包指纹一致。
"%VENV%\Scripts\flet.exe" build apk --yes ^
  --build-number %BUILD_NUM% --build-version 0.1.%BUILD_NUM% ^
  --android-signing-key-store "%~dp0android\keystore\app.keystore" ^
  --android-signing-key-store-password android ^
  --android-signing-key-alias androiddebugkey ^
  --android-signing-key-password android ^
  --android-adaptive-icon-background "#2A3550" %*
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
