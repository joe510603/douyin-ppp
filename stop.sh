#!/bin/bash
# douyin-ppp 停止脚本
# 用法: ./stop.sh

cd "$(dirname "$0")"

stop_service() {
    local pid_file=$1
    local name=$2
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "🛑 停止 $name (PID: $pid)..."
            kill "$pid"
            # 等待进程退出，最多10秒
            for i in {1..10}; do
                if ! kill -0 "$pid" 2>/dev/null; then
                    echo "   ✅ $name 已停止"
                    break
                fi
                sleep 1
            done
            # 如果还在运行，强制杀掉
            if kill -0 "$pid" 2>/dev/null; then
                echo "   ⚠️  $name 未响应，强制终止..."
                kill -9 "$pid" 2>/dev/null
                echo "   ✅ $name 已强制终止"
            fi
        else
            echo "ℹ️  $name 未运行 (PID 文件存在但进程已不存在)"
        fi
        rm -f "$pid_file"
    else
        echo "ℹ️  $name 未运行 (无 PID 文件)"
    fi
}

echo "=========================================="
echo "  douyin-ppp 停止脚本"
echo "=========================================="

# 停止主程序
stop_service "data/app.pid" "主程序"

# 停止签名服务
stop_service "data/signer.pid" "签名服务"

echo ""
echo "=========================================="
echo "  ✅ 停止完成"
echo "=========================================="
