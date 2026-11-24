@echo off
cd /d "%~dp0"

rem 按优先级查找程序：同目录（分发场景）> Release 构建 > release 目录
set "APP="
if exist "%~dp0xHR.exe" set "APP=%~dp0xHR.exe"
if not defined APP if exist "%~dp0bin\Release\net9.0-windows\win-x64\xHR.exe" set "APP=%~dp0bin\Release\net9.0-windows\win-x64\xHR.exe"
if not defined APP if exist "%~dp0release\xHR.exe" set "APP=%~dp0release\xHR.exe"

if not defined APP goto :missing

echo 正在启动 x.HR 小客户端...
start "" "%APP%"
exit /b 0

:missing
echo.
echo  未找到 xHR.exe，请确认以下任一位置存在程序：
echo    1. 与本 bat 同目录：xHR.exe
echo    2. %~dp0bin\Release\net9.0-windows\win-x64\xHR.exe
echo.
echo  如需编译，请在项目目录执行：dotnet build -c Release
echo.
pause
exit /b 1
