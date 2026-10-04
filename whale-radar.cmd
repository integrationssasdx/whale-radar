@echo off
rem whale-radar product entry for Windows cmd / PowerShell.
rem Runs the repo-root Python launcher, which locates the bundled
rem whale_radar package next to itself; no PYTHONPATH, pip install,
rem or network needed. Quoted %~dp0 keeps paths with spaces working.
python "%~dp0whale-radar" %*
exit /b %ERRORLEVEL%
