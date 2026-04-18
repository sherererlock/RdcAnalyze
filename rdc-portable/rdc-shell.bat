@echo off
set "DIR=%~dp0"
set "RENDERDOC_PYTHON_PATH=%DIR%renderdoc"
set "PATH=%DIR%platform-tools;%DIR%..\python;%PATH%"
doskey rdc="%DIR%..\python\python.exe" -c "from rdc.cli import entry; entry()" $*
echo.
echo  rdc-cli portable shell
echo  Type "rdc --help" to get started.
echo  Type "exit" to quit.
echo.
cmd /k "title rdc-cli"
