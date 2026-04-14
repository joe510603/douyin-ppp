#!/bin/bash
# douyin-ppp 重启脚本
# 用法: ./restart.sh

cd "$(dirname "$0")"

echo "=========================================="
echo "  douyin-ppp 重启脚本"
echo "=========================================="

# 先停止
echo ""
echo ">>> 第一步：停止现有服务..."
./stop.sh

# 再启动
echo ""
echo ">>> 第二步：启动服务..."
./start.sh
