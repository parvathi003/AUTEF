@echo off
REM Run AUTEF v2 without installing it or setting anything up.
REM
REM The package lives under src\, so a bare "python -m autef2" from the project
REM root cannot find it. This puts src\ on the path and hands everything through:
REM
REM     autef2.bat web
REM     autef2.bat check https://github.com/astanin/python-tabulate
REM     autef2.bat compare tests_autef2\sample_project -n 4

setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src;%PYTHONPATH%"

if exist "%HERE%.venv\Scripts\python.exe" (
    set "PY=%HERE%.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m autef2 %*
endlocal
