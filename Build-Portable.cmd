@echo off
setlocal
cd /d "%~dp0"

echo Choicer Voicer Pack Creator - Portable Windows Build
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build-Portable.ps1" %*
set "BUILD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%BUILD_EXIT_CODE%"=="0" (
    echo Build failed. Review the error above.
) else (
    echo Build completed successfully.
)

if /I "%CVPC_NO_PAUSE%"=="1" exit /b %BUILD_EXIT_CODE%
pause
exit /b %BUILD_EXIT_CODE%