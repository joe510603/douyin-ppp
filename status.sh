#!/bin/bash
# douyin-ppp 状态检查脚本
# 兼容: macOS / Linux (Ubuntu/CentOS/OpenCloudOS 等)
# 用法: ./status.sh

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

check_service() {
    local pid_file=$1
    local name=$2
    local port=$3
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "✅ $name: 运行中 (PID: $pid)"
            if [ -n "$port" ]; then
                if is_port_listening $port; then
                    echo "   端口 $port: 监听中"
                else
                    echo "   端口 $port: ❌ 未监听"
                fi
            fi
            return 0
        else
            echo "⚠️  $name: PID 文件存在但进程已终止"
            rm -f "$pid_file"
            return 1
        fi
    else
        echo "⚪  $name: 未运行"
        return 1
    fi
}

echo "=========================================="
echo "  douyin-ppp 服务状态"
echo "=========================================="
echo ""

check_service "data/signer.pid" "签名服务" "3010"
check_service "data/app.pid" "主程序" "9527"

echo ""
if [ -f "data/signer.pid" ] && [ -f "data/app.pid" ]; then
    echo "🟢 所有服务正在运行"
    echo ""
    echo "  Web UI: http://localhost:9527"
else
    echo "🔴 部分或全部服务未运行"
fi

echo ""
echo "=========================================="
echo "  日志查看命令"
echo "=========================================="
echo "  签名服务: tail -20 logs/signer.log"
echo "  主程序:   tail -20 data/logs/app.log"