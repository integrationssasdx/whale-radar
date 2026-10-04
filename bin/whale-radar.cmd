@echo off
rem whale-radar launcher for Windows cmd and PowerShell.
rem Locates the whale_radar package next to this script: no PYTHONPATH,
rem no pip install, no network. Safe when the repo path contains spaces.
setlocal
set "LAUNCHER=%~dp0whale-radar"
python "%LAUNCHER%" %*
exit /b %ERRORLEVEL%
