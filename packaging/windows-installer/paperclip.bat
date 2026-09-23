@echo off
REM Paperclip double-click launcher - runs from install dir
REM This batch file ensures console stays open when double-clicked
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Paperclip - double-click detected, running doctor...
  echo.
  "%~dp0paperclip.exe" doctor
  echo.
  echo Press any key to exit...
  pause >nul
) else (
  "%~dp0paperclip.exe" %*
)
endlocal
