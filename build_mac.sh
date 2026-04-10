#!/bin/bash
# ============================================================
#  板块异动监控 —— macOS 一键打包脚本
#  使用方法：
#    1. 把整个 sector_monitor 文件夹拷贝到 Mac
#    2. 打开"终端"（Terminal），cd 到本文件夹
#    3. 执行：bash build_mac.sh
#    4. 打包完成后，dist/ 目录里会出现「板块异动监控.app」
# ============================================================

set -e   # 任何命令失败就停止

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo ""
echo -e "${BOLD}================================================${RESET}"
echo -e "${BOLD}   板块异动监控 macOS 打包脚本${RESET}"
echo -e "${BOLD}================================================${RESET}"
echo ""

# ── 1. 检查 Python ───────────────────────────────────────────
echo -e "${YELLOW}[1/5] 检查 Python 环境...${RESET}"

if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ 未找到 python3，请先安装 Python 3.9+${RESET}"
    echo "    安装方式：https://www.python.org/downloads/macos/"
    echo "    或使用 Homebrew：brew install python"
    exit 1
fi

PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✅ Python ${PYTHON_VER} 已安装${RESET}"

# Python 版本需要 >= 3.9
MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 9 ]); then
    echo -e "${RED}❌ Python 版本过低（${PYTHON_VER}），需要 3.9 或以上${RESET}"
    exit 1
fi

# ── 2. 创建/激活虚拟环境 ─────────────────────────────────────
echo ""
echo -e "${YELLOW}[2/5] 准备虚拟环境...${RESET}"

if [ ! -d "venv_mac" ]; then
    echo "    创建虚拟环境 venv_mac ..."
    python3 -m venv venv_mac
fi

source venv_mac/bin/activate
echo -e "${GREEN}✅ 虚拟环境已激活${RESET}"

# ── 3. 安装依赖 ──────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/5] 安装依赖包（首次约需 1-2 分钟）...${RESET}"

pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install pyinstaller -q

echo -e "${GREEN}✅ 依赖安装完成${RESET}"

# ── 4. 打包 ──────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[4/5] 开始打包（约需 1-3 分钟）...${RESET}"

# 清理旧产物
rm -rf build dist

python3 -m PyInstaller sector_monitor_mac.spec --clean --noconfirm

# ── 5. 检查产物 ──────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[5/5] 检查打包结果...${RESET}"

APP_PATH="dist/板块异动监控.app"

if [ -d "$APP_PATH" ]; then
    echo ""
    echo -e "${BOLD}${GREEN}================================================${RESET}"
    echo -e "${BOLD}${GREEN}  ✅ 打包成功！${RESET}"
    echo -e "${BOLD}${GREEN}================================================${RESET}"
    echo ""
    echo -e "  产物位置：${BOLD}$(pwd)/dist/板块异动监控.app${RESET}"
    echo ""
    echo -e "  使用方法："
    echo -e "    ① 双击「板块异动监控.app」启动"
    echo -e "    ② 如提示「无法打开」，右键 → 打开 → 打开（绕过 Gatekeeper）"
    echo -e "    ③ 启动后在浏览器打开：${BOLD}http://127.0.0.1:18888${RESET}"
    echo ""
    echo -e "  ${YELLOW}注意：首次运行系统可能提示安全警告，选「打开」即可${RESET}"
    echo ""

    # 询问是否直接打开 .app 测试
    read -p "  现在就测试运行一下吗？(y/n): " RUN_NOW
    if [[ "$RUN_NOW" == "y" || "$RUN_NOW" == "Y" ]]; then
        echo ""
        echo "  正在启动，请在浏览器打开 http://127.0.0.1:18888 ..."
        open "$APP_PATH"
    fi
else
    echo -e "${RED}❌ 打包失败，未找到 ${APP_PATH}${RESET}"
    echo "   请查看上方的错误信息"
    exit 1
fi

deactivate
