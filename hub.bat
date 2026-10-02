@echo off
setlocal
rem Shortcuts for the hub. Run "hub" without arguments for the list.
rem The settings can be overridden with environment variables of the same name.

set "REPO=%~dp0"
if "%HUB_PROXMOX_HOST%"=="" set "HUB_PROXMOX_HOST=192.168.178.114"
if "%HUB_CONTAINER%"=="" set "HUB_CONTAINER=102"
if "%HUB_URL%"=="" set "HUB_URL=http://192.168.178.185:8787"
if "%HUB_USER%"=="" set "HUB_USER=%USERNAME%"

rem A machine has the collector either in the repository's virtual
rem environment or in the global Python. Use whichever exists.
set "PY=py"
if exist "%REPO%.venv\Scripts\python.exe" set "PY=%REPO%.venv\Scripts\python.exe"

set "CMD=%~1"
if "%CMD%"=="" goto help
shift

if /i "%CMD%"=="collect" goto collect
if /i "%CMD%"=="check" goto check
if /i "%CMD%"=="loop" goto loop
if /i "%CMD%"=="config" goto config
if /i "%CMD%"=="open" goto open
if /i "%CMD%"=="token" goto token
if /i "%CMD%"=="deploy" goto deploy
if /i "%CMD%"=="ship" goto ship
if /i "%CMD%"=="status" goto status
if /i "%CMD%"=="logs" goto logs
if /i "%CMD%"=="wsl-setup" goto wsl_setup
if /i "%CMD%"=="wsl-config" goto wsl_config
if /i "%CMD%"=="wsl-check" goto wsl_check
if /i "%CMD%"=="wsl-collect" goto wsl_collect
if /i "%CMD%"=="wsl-loop" goto wsl_loop
echo Unknown command "%CMD%".
echo.

:help
echo Usage: hub COMMAND
echo.
echo On this machine:
echo   collect        Upload sessions, worktrees, and rules once
echo   check          Test the connection and the configuration
echo   loop           Upload every five minutes until you close the window
echo   config         Open the collector configuration in Notepad
echo   open           Open the hub in the browser
echo.
echo Inside WSL on this machine:
echo   wsl-setup      Install or update the collector inside WSL
echo   wsl-config     Open the WSL collector configuration
echo   wsl-check      Test the WSL collector
echo   wsl-collect    Upload from WSL once
echo   wsl-loop       Upload from WSL every five minutes
echo.
echo On the server (needs SSH access to the Proxmox host):
echo   token NAME     Create the token for the machine NAME
echo   deploy         Install the last commit on the server
echo   ship "TEXT"    Commit everything with the message TEXT, push, and deploy
echo   status         Show the state of the hub service
echo   logs           Show the last log lines of the hub service
goto end

:collect
"%PY%" -m claude_hub.collector.cli run
goto end

:check
"%PY%" -m claude_hub.collector.cli check
goto end

:loop
"%PY%" -m claude_hub.collector.cli run --interval 300
goto end

:config
if not exist "%APPDATA%\claude-hub\collector.toml" "%PY%" -m claude_hub.collector.cli init
notepad "%APPDATA%\claude-hub\collector.toml"
goto end

:open
start "" "%HUB_URL%"
goto end

:token
if "%~1"=="" (
    echo Give the machine name: hub token NAME
    goto fail
)
ssh root@%HUB_PROXMOX_HOST% pct exec %HUB_CONTAINER% -- /usr/local/bin/claude-hub token add %~1 --user %HUB_USER%
goto end

:ship
git -C "%REPO%." add -A
if errorlevel 1 goto fail
git -C "%REPO%." diff --cached --quiet
if not errorlevel 1 goto ship_push
if "%~1"=="" (
    echo There are changes to commit. Give a message: hub ship "TEXT"
    goto fail
)
git -C "%REPO%." commit -m "%~1"
if errorlevel 1 goto fail
:ship_push
git -C "%REPO%." push
if errorlevel 1 goto fail
goto deploy

:deploy
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%deploy\deploy.ps1" -ProxmoxHost %HUB_PROXMOX_HOST% -Container %HUB_CONTAINER%
goto end

:status
ssh root@%HUB_PROXMOX_HOST% pct exec %HUB_CONTAINER% -- systemctl status claude-hub --no-pager
goto end

:logs
ssh root@%HUB_PROXMOX_HOST% pct exec %HUB_CONTAINER% -- journalctl -u claude-hub -n 50 --no-pager
goto end

:wsl_setup
rem WSL starts in the matching Linux folder, so the script finds the repository.
pushd "%REPO%."
wsl bash ./scripts/wsl-setup.sh
popd
goto end

:wsl_config
wsl bash -lc "nano $HOME/.config/claude-hub/collector.toml"
goto end

:wsl_check
wsl bash -lc "$HOME/.claude-hub-venv/bin/claude-hub-collector check"
goto end

:wsl_collect
wsl bash -lc "$HOME/.claude-hub-venv/bin/claude-hub-collector run"
goto end

:wsl_loop
wsl bash -lc "$HOME/.claude-hub-venv/bin/claude-hub-collector run --interval 300"
goto end

:fail
endlocal
exit /b 1

:end
endlocal
