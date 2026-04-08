#!/bin/bash
# 启动 Node.js 签名服务

cd "$(dirname "$0")"

# 检查 node 和 npm
if ! command -v node &> /dev/null; then
    echo "❌ Node.js 未安装"
    exit 1
fi

# 安装依赖
if [ ! -d "node_modules" ]; then
    echo "📦 安装签名服务依赖..."
    npm install
fi

# 启动服务
echo "🚀 启动签名服务 (端口 3010)..."
node server.js