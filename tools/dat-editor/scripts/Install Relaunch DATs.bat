@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  Install Relaunch DATs.bat
REM
REM  Copies every DAT under this script's ROM\ subfolder into
REM  the FFXI client's ROM\ tree, preserving the numbered
REM  subfolder structure. Any file that would be overwritten
REM  is first renamed to <name>_backup_<timestamp>.DAT next to
REM  itself, so the previous version is always recoverable.
REM
REM  Put this .bat directly next to your ROM\ folder. Example:
REM    D:\server_relaunch\Custom DATs\Relaunch Custom DATs\
REM        ROM\
REM        Install Relaunch DATs.bat
REM
REM  Edit CLIENT_ROM below if your FFXI install is elsewhere.
REM  Close FFXI before running - the game locks its DAT files.
REM ============================================================

set "CLIENT_ROM=C:\Program Files (x86)\PlayOnline\SquareEnix\FINAL FANTASY XI\ROM"
set "SRC_ROM=%~dp0ROM"
if "%SRC_ROM:~-1%"=="\" set "SRC_ROM=%SRC_ROM:~0,-1%"

for /f %%A in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%A"
set "LOG=%~dp0install_%TS%.log"

call :log ============================================================
call :log Install Relaunch DATs - %TS%
call :log Source: %SRC_ROM%
call :log Target: %CLIENT_ROM%
call :log ============================================================

if not exist "%SRC_ROM%" (
    call :log ERROR: Source folder not found: %SRC_ROM%
    call :log        This .bat expects a ROM\ folder next to it.
    goto :end
)
if not exist "%CLIENT_ROM%" (
    call :log ERROR: Client folder not found: %CLIENT_ROM%
    call :log        Edit CLIENT_ROM at the top of this script.
    goto :end
)

net session >nul 2>&1
if errorlevel 1 (
    call :log WARNING: not running as Administrator.
    call :log          Writes under "Program Files (x86)" usually need admin.
    call :log          If copies below fail with Access denied, right-click
    call :log          this .bat and choose "Run as administrator".
)

set /a COPIED=0
set /a BACKED_UP=0
set /a FAILED=0

for /r "%SRC_ROM%" %%F in (*.DAT) do call :install "%%~fF"

call :log ------------------------------------------------------------
call :log Copied: !COPIED!   Backed up: !BACKED_UP!   Failed: !FAILED!
call :log Full log written to: %LOG%
call :log ------------------------------------------------------------

:end
echo.
echo Press any key to close...
pause >nul
endlocal
exit /b


REM -----------------------------------------------------------
REM  :install <full source DAT path>
REM  Compute REL path under SRC_ROM, back up any existing dest
REM  in place with timestamp, then copy source over dest.
REM -----------------------------------------------------------
:install
    set "SRC=%~1"
    set "REL=!SRC:%SRC_ROM%\=!"
    set "DEST=%CLIENT_ROM%\!REL!"

    for %%D in ("!DEST!") do set "DEST_DIR=%%~dpD"
    if not exist "!DEST_DIR!" mkdir "!DEST_DIR!" 2>nul

    if exist "!DEST!" (
        for %%X in ("!DEST!") do (
            set "STEM=%%~nX"
            set "EXT=%%~xX"
        )
        move /y "!DEST!" "!DEST_DIR!!STEM!_backup_%TS%!EXT!" >nul
        if errorlevel 1 (
            call :log FAIL backup: !REL!
            set /a FAILED+=1
            goto :eof
        )
        set /a BACKED_UP+=1
        call :log backup: !REL! renamed to !STEM!_backup_%TS%!EXT!
    )

    copy /y "!SRC!" "!DEST!" >nul
    if errorlevel 1 (
        call :log FAIL copy:   !REL!
        set /a FAILED+=1
        goto :eof
    )
    set /a COPIED+=1
    call :log copy:   !REL!
goto :eof


REM -----------------------------------------------------------
REM  :log <message>
REM  Echo to console AND append to %LOG%.
REM -----------------------------------------------------------
:log
    echo %*
    >> "%LOG%" echo %*
goto :eof
