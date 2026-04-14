#!/bin/bash
# douyin-ppp 环境检测脚本
# 兼容: macOS / Linux (Ubuntu/CentOS/OpenCloudOS/RHEL 等)
# 用法: ./env_check.sh

cd "$(dirname "$0")"

echo "=========================================="
echo "  douyin-ppp 环境检测"
echo "=========================================="
echo ""

check_cmd() {
    local cmd=$1
    local name=$2
    if command -v "$cmd" >/dev/null 2>&1; then
        local version=$($cmd --version 2>/dev/null | head -1 || echo "(已安装)")
        echo "✅ $name: $version"
        return 0
    else
        echo "❌ $name: 未安装"
        return 1
    fi
}

check_python_version() {
    if command -v python3 >/dev/null 2>&1; then
        local ver=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
        echo "✅ Python: $(python3 --version 2>&1)"
        if [ "$(echo "$ver < 3.10" | bc 2>/dev/null || echo 0)" = "1" ]; then
            echo "⚠️  建议 Python 3.10+，部分功能可能受限"
        fi
    elif command -v python >/dev/null 2>&1; then
        local ver=$(python --version 2>&1 | grep -oP '\d+\.\d+')
        echo "✅ Python: $(python --version 2>&1)"
        if [ "$(echo "$ver < 3.10" | bc 2>/dev/null || echo 0)" = "1" ]; then
            echo "⚠️  建议 Python 3.10+，部分功能可能受限"
        fi
    else
        echo "❌ Python: 未安装"
    fi
}

check_port() {
    local port=$1
    local name=$2
    if command -v lsof >/dev/null 2>&1; then
        if lsof -i :$port >/dev/null 2>&1; then
            echo "⚠️  端口 $port ($name): 已被占用"
        else
            echo "✅ 端口 $port ($name): 可用"
        fi
    elif command -v ss >/dev/null 2>&1; then
        if ss -tlnp 2>/dev/null | grep -q ":$port "; then
            echo "⚠️  端口 $port ($name): 已被占用"
        else
            echo "✅ 端口 $port ($name): 可用"
        fi
    else
        echo "ℹ️  端口 $port: 工具不可用，无法检测"
    fi
}

# 系统信息
echo "--- 系统信息 ---"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "系统: $NAME $VERSION"
elif [ -f /etc/redhat-release ]; then
    cat /etc/redhat-release
else
    uname -s
fi
echo ""

# 核心依赖
echo "--- 核心依赖 ---"
check_cmd "node" "Node.js"
check_cmd "npm" "npm"
check_python_version
check_cmd "pip3" "pip3"
echo ""

# Python 包
echo "--- Python 包检查 ---"
if command -v python3 >/dev/null 2>&1; then
    for pkg in nicegui httpx websockets protobuf xhshow pandas openpyxl aiosqlite loguru jieba wordcloud; do
        if python3 -c "import $pkg" 2>/dev/null; then
            echo "✅ $pkg"
        else
            echo "❌ $pkg (未安装，请运行: pip install -r requirements.txt)"
        fi
    done
fi
echo ""

# Playwright 浏览器
echo "--- Playwright 浏览器 ---"
if command -v python3 >/dev/null 2>&1; then
    if python3 -c "import playwright" 2>/dev/null; then
        # 尝试检测浏览器
        local_browser=$(python3 -c "
import os, sys
paths = [
    os.path.expanduser('~/.cache/ms-playwright'),
    '/root/.cache/ms-playwright',
]
found = False
for p in paths:
    if os.path.exists(p):
        print('✅ Playwright 浏览器: 已安装')
        found = True
        break
if not found:
    print('❌ Playwright 浏览器: 未安装 (请运行: playwright install chromium)')
" 2>/dev/null || echo "⚠️  Playwright 未安装或检测失败")
        echo "$local_browser"
    else
        echo "⚠️  Playwright 未安装 (可选，用于小红书评论采集)"
    fi
fi
echo ""

# 端口检查
echo "--- 端口检查 ---"
check_port 3010 "签名服务"
check_port 9527 "主程序"
echo ""

# Node.js 签名服务
echo "--- Node.js 签名服务 ---"
if [ -d "websdk/node_modules" ]; then
    echo "✅ 签名服务依赖: 已安装"
else
    echo "⚠️  签名服务依赖: 未安装 (请运行: cd websdk && npm install)"
fi
echo ""

echo "=========================================="
echo "  检测完成"
echo "=========================================="
echo ""
echo "快速启动 (Linux):"
echo "  ./start.sh"
echo ""
echo "快速启动 (macOS):"
echo "  ./start.sh"
echo ""
echo "安装 Python 依赖:"
echo "  pip3 install -r requirements.txt"
echo ""
echo "安装 Playwright 浏览器:"
echo "  playwright install chromium"