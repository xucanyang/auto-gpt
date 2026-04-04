@echo off
setlocal

set "BACKEND_PORT=%BACKEND_PORT%"
if "%BACKEND_PORT%"=="" set "BACKEND_PORT=8000"
set "SOLVER_PORT=%SOLVER_PORT%"
if "%SOLVER_PORT%"=="" set "SOLVER_PORT=8889"
set "GROK2API_PORT=%GROK2API_PORT%"
if "%GROK2API_PORT%"=="" set "GROK2API_PORT=8011"
set "CLIPROXYAPI_PORT=%CLIPROXYAPI_PORT%"
if "%CLIPROXYAPI_PORT%"=="" set "CLIPROXYAPI_PORT=8317"
set "FULL_STOP=%FULL_STOP%"
if "%FULL_STOP%"=="" set "FULL_STOP=1"

echo [INFO] 准备停止后端相关服务
if "%FULL_STOP%"=="1" (
  powershell -ExecutionPolicy Bypass -File "%~dp0stop_backend.ps1" -BackendPort %BACKEND_PORT% -SolverPort %SOLVER_PORT% -Grok2ApiPort %GROK2API_PORT% -CLIProxyAPIPort %CLIPROXYAPI_PORT% -FullStop 1
) else (
  powershell -ExecutionPolicy Bypass -File "%~dp0stop_backend.ps1" -BackendPort %BACKEND_PORT% -SolverPort %SOLVER_PORT% -FullStop 0
)
