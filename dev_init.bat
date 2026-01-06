@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ===== 本地开发初始化脚本（Windows）=====
REM 请按你的本机路径修改
set "PROJECT_DIR=D:\Website\ff14_zsb"
set "VENV_DIR=%PROJECT_DIR%\venv"

REM 绑定地址与端口（按需改）
set "HOST=127.0.0.1"
set "PORT=8000"

REM 如果中文乱码可启用
chcp 65001 >nul

echo [1/7] Enter project directory...
if exist "%PROJECT_DIR%\" (
    cd /d "%PROJECT_DIR%"
) else (
    echo Error: Project directory not found: %PROJECT_DIR%
    exit /b 1
)

REM （可选）拉取最新代码：如果你只是本地调试且不需要更新，可注释掉这一段
echo [2/7] Updating code (optional)...
git pull
if errorlevel 1 (
    echo Error: Git pull failed.
    exit /b 1
)

echo [3/7] Create venv if missing...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    py -3 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Error: Failed to create venv.
        exit /b 1
    )
)

echo [4/7] Activate venv...
if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
) else (
    echo Error: activate.bat not found: %VENV_DIR%\Scripts\activate.bat
    exit /b 1
)

echo [5/7] Install/upgrade dependencies...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo Error: pip upgrade failed.
    exit /b 1
)
pip install -r requirements.txt
if errorlevel 1 (
    echo Error: pip install -r requirements.txt failed.
    exit /b 1
)

echo [6/7] Apply database migrations...
python manage.py migrate
if errorlevel 1 (
    echo Error: migrate failed.
    exit /b 1
)

REM 本地调试通常不强制 collectstatic；如果你依赖静态文件收集，可保留
echo [7/7] Collect static files (optional)...
python manage.py collectstatic --noinput
if errorlevel 1 (
    echo Error: collectstatic failed.
    exit /b 1
)

echo.
echo Starting Django dev server: http://%HOST%:%PORT%/
python manage.py runserver %HOST%:%PORT%
