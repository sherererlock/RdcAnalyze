@echo off
setlocal
set "DIR=%~dp0"
set "RENDERDOC_PYTHON_PATH=%DIR%renderdoc"
set "PATH=%DIR%platform-tools;%DIR%..\python;%PATH%"
"%DIR%..\python\python.exe" -c "from rdc.cli import entry; entry()" %*
endlocal
