#!/bin/bash
# 一键启动脚本 - 同时启动签名服务和 Python 主程序

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

# 等待签名服务启动
sleep 3

# 启动 Python 主程序
echo "🚀 启动 Python 主程序..."
python app.py

# 清理
kill $SIGNER_PID 2>/dev/null