#!/bin/bash
# douyin-ppp 停止脚本
# 兼容: macOS / Linux (Ubuntu/CentOS/OpenCloudOS 等)
# 用法: ./stop.sh

cd "$(dirname "$0")"

# 跨平台：按端口杀进程
kill_by_port() {
    local port=$1
    local name=$2
    local pids=""

    # 用 ss 找 LISTEN 进程的 PID
    if command -v ss >/dev/null 2>&1; then
        pids=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | sort -u)
    fi
    # 用 lsof 备选
    if [ -z "$pids" ] && command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -i :$port -sTCP:LISTEN -t 2>/dev/null | sort -u)
    fi

    if [ -n "$pids" ]; then
        for pid in $pids; do
            echo "🛑 停止 $name (端口 $port, PID: $pid)..."
            kill "$pid" 2>/dev/null
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                echo "   ⚠️  未响应，强制终止..."
                kill -9 "$pid" 2>/dev/null
            fi
        done
        echo "   ✅ $name 已停止"
    else
        echo "ℹ️  $name (端口 $port): 未检测到运行进程"
    fi
}

# 按 PID 文件杀进程
kill_by_pid_file() {
    local pid_file=$1
    local name=$2
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "🛑 停止 $name (PID: $pid)..."
            kill "$pid" 2>/dev/null
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null
            fi
            echo "   ✅ $name 已停止"
        else
            echo "ℹ️  $name: PID 文件存在但进程已不存在"
        fi
        rm -f "$pid_file"
    else
        echo "ℹ️  $name: 无 PID 文件"
    fi
}

echo "=========================================="
echo "  douyin-ppp 停止脚本"
echo "=========================================="

# 先按 PID 文件杀
kill_by_pid_file "data/app.pid" "主程序"
kill_by_pid_file "data/signer.pid" "签名服务"

# 再按端口补刀（兜底）
kill_by_port 9527 "主程序"
kill_by_port 3010 "签名服务"

echo ""
echo "=========================================="
echo "  ✅ 停止完成"
echo "=========================================="
