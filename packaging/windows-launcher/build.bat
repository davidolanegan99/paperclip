@echo off
setlocal
cd /d "%~dp0\..\.."
echo Building Zig launchers...

where zig >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo Zig not found. Install from https://ziglang.org/download/
  echo Or: npm install -g @ziglang/zig-win32-x64
  exit /b 1
)

echo Using: 
zig version

if not exist packaging\exe\dist mkdir packaging\exe\dist

echo [..] Building paperclip.exe (windows x64)...
zig build-exe packaging/windows-launcher/launcher.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip.exe
if %ERRORLEVEL% NEQ 0 exit /b 1

echo [..] Building paperclip-installer.exe (windows x64)...
zig build-exe packaging/windows-launcher/installer.zig -target x86_64-windows-gnu -O ReleaseSmall -femit-bin=packaging/exe/dist/paperclip-installer.exe
if %ERRORLEVEL% NEQ 0 exit /b 1

echo.
echo Built:
dir packaging\exe\dist\paperclip*.exe
echo.
echo Test (requires Node):
echo   packaging\exe\dist\paperclip.exe --launcher-doctor
