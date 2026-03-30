@echo off
chcp 65001 >nul
echo ============================
echo  板块异动监控 - 启动中...
echo ============================
cd /d "%~dp0"
echo [1/2] 安装依赖...
pip install -r requirements.txt -q
echo [2/2] 启动监控服务...
echo.
echo 浏览器将自动打开监控面板
echo 按 Ctrl+C 停止服务
echo.
python main.py
pause
