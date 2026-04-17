#!/bin/bash
# douyin-ppp 启动脚本
# 兼容: macOS / Linux (Ubuntu/CentOS/OpenCloudOS 等)
# 用法: ./start.sh

cd "$(dirname "$0")"

# 跨平台工具检测
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# 检查端口是否被占用（优先用 ss/netstat，lsof 只作备选）
is_port_listening() {
    local port=$1
    # 优先 ss（Linux/macOS 都支持）
    if command_exists ss; then
        ss -tlnp 2>/dev/null | grep -qE "(:$port|\.$port )" && return 0
    fi
    # 其次 netstat
    if command_exists netstat; then
        netstat -tlnp 2>/dev/null | grep -q ":$port " && return 0
    fi
    # 最后 lsof（macOS 有时会返回已关闭连接的残留，需过滤 LISTEN）
    if command_exists lsof; then
        lsof -i :$port -sTCP:LISTEN 2>/dev/null | grep -q LISTEN && return 0
    fi
    return 1
}

# 检查是否已在运行
check_already_running() {
    local pid_file=$1
    local name=$2
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  $name 已在运行 (PID: $pid)，请先执行 ./stop.sh 或 ./restart.sh"
            return 1
        else
            echo "🧹 清理过期 PID 文件: $pid_file"
            rm -f "$pid_file"
        fi
    fi
    return 0
}

# 检查端口是否被占用
check_port() {
    local port=$1
    local name=$2
    if is_port_listening $port; then
        echo "⚠️  端口 $port ($name) 已被占用"
        echo "   查看占用: ss -tlnp | grep $port"
        return 1
    fi
    return 0
}

echo "=========================================="
echo "  douyin-ppp 启动脚本"
echo "=========================================="

# 前置检查
check_already_running "data/signer.pid" "签名服务" || exit 1
check_already_running "data/app.pid" "主程序" || exit 1
check_port 3010 "签名服务" || exit 1
check_port 9527 "主程序" || exit 1

# 确保日志目录存在
mkdir -p logs
mkdir -p data/logs

# 启动签名服务
echo ""
echo "📦 启动签名服务 (端口 3010)..."
cd websdk
if [ ! -d "node_modules" ]; then
    echo "   📦 安装签名服务依赖..."
    npm install
fi
nohup node server.js > ../logs/signer.log 2>&1 &
SIGNER_PID=$!
cd ..
echo $SIGNER_PID > data/signer.pid
echo "   ✅ 签名服务已启动 (PID: $SIGNER_PID)"

# 等待签名服务就绪
sleep 3

# 检查签名服务是否真的启动成功
if ! kill -0 $SIGNER_PID 2>/dev/null; then
    echo "❌ 签名服务启动失败，查看日志: tail -20 logs/signer.log"
    rm -f data/signer.pid
    exit 1
fi

# 检查签名服务端口
sleep 1
if ! is_port_listening 3010; then
    echo "❌ 签名服务未监听端口 3010，查看日志: tail -20 logs/signer.log"
    kill $SIGNER_PID 2>/dev/null
    rm -f data/signer.pid
    exit 1
fi

# 启动主程序
echo ""
echo "🚀 启动主程序 (端口 9527)..."
nohup python app.py > data/logs/app.log 2>&1 &
APP_PID=$!
echo $APP_PID > data/app.pid
echo "   ✅ 主程序已启动 (PID: $APP_PID)"

# 等待主程序就绪
sleep 3

# 检查主程序是否启动成功
if ! kill -0 $APP_PID 2>/dev/null; then
    echo "❌ 主程序启动失败，查看日志: tail -20 data/logs/app.log"
    kill $SIGNER_PID 2>/dev/null
    rm -f data/signer.pid data/app.pid
    exit 1
fi

echo ""
echo "=========================================="
echo "  ✅ 启动完成"
echo "  签名服务: PID $SIGNER_PID (端口 3010)"
echo "  主程序:   PID $APP_PID (端口 9527)"
echo "  Web UI:   http://localhost:9527"
echo "=========================================="
echo ""
echo "日志查看:"
echo "  签名服务: tail -f logs/signer.log"
echo "  主程序:   tail -f data/logs/app.log"
