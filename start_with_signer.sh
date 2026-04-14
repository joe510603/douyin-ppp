#!/bin/bash
# 一键启动脚本 - 同时启动签名服务和 Python 主程序
# 兼容: macOS / Linux (Ubuntu/CentOS/OpenCloudOS 等)

cd "$(dirname "$0")"

# 跨平台端口检测
is_port_listening() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -i :$port >/dev/null 2>&1 && return 0
    elif command -v ss >/dev/null 2>&1; then
        ss -tlnp 2>/dev/null | grep -q ":$port " && return 0
    elif command -v netstat >/dev/null 2>&1; then
        netstat -tlnp 2>/dev/null | grep -q ":$port " && return 0
    fi
    return 1
}

# 启动 Node.js 签名服务（后台）
cd websdk
if [ ! -d "node_modules" ]; then
    echo "📦 安装签名服务依赖..."
    npm install
fi

echo "🚀 启动签名服务 (端口 3010)..."
nohup node server.js > ../logs/signer.log 2>&1 &
SIGNER_PID=$!
cd ..

# 等待签名服务启动（兼容 Linux）
sleep 3

# 检查签名服务是否正常监听
if ! is_port_listening 3010; then
    echo "❌ 签名服务启动失败，查看日志: tail -20 logs/signer.log"
    exit 1
fi

# 启动 Python 主程序
echo "🚀 启动 Python 主程序..."
python3 app.py