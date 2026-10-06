@echo off
chcp 65001 >nul
rem 自动提权（UAC 确认一次）并以管理员身份执行真正的构建脚本。
rem 用途：未开启 Windows 开发者模式时，symlink 需要管理员权限。

net session >nul 2>&1
if %errorlevel% == 0 (
  echo 已是管理员，直接构建...
  call "%~dp0build_mobile.bat"
  pause
  exit /b %errorlevel%
)

echo 正在请求管理员权限（请在弹窗中点「是」）...
powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs" 
exit /b 0
