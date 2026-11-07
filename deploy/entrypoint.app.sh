#!/bin/bash
# ============================================================================
# Douyin PPP — Python 主程序容器入口脚本
#
# 职责：
#   1) 把容器外的 ./data 挂载目录权限拉起来（避免 root 写入宿主机 UID 错乱）
#   2) 等待签名服务健康（compose depends_on 已控顺序，这里再保险一次）
#   3) 启动 NiceGUI 主程序
# ============================================================================

set -e

log() { echo "[entrypoint.app $(date +%H:%M:%S)] $*"; }

# ---- 1) 数据目录就绪 ----
mkdir -p \
    /app/data/db \
    /app/data/exports \
    /app/data/logs \
    /app/data/browser_data \
    /app/data/xhs_browser_data \
    /app/logs

# ---- 2) 等待签名服务（如果配置了 SIGNER_URL）----
SIGNER_URL="${SIGNER_URL:-${DOUYIN_PPP_SIGNER_URL:-}}"
if [ -n "$SIGNER_URL" ]; then
    # 从 URL 抽出 host:port
    SIGNER_HOSTPORT=$(echo "$SIGNER_URL" | sed -E 's#^https?://##' | sed -E 's#/.*$##')
    SIGNER_HOST=$(echo "$SIGNER_HOSTPORT" | cut -d: -f1)
    SIGNER_PORT=$(echo "$SIGNER_HOSTPORT" | cut -d: -f2)
    SIGNER_PORT="${SIGNER_PORT:-3010}"

    log "⏳ 等待签名服务 ${SIGNER_HOST}:${SIGNER_PORT} 就绪..."
    for i in $(seq 1 30); do
        if (echo > /dev/tcp/${SIGNER_HOST}/${SIGNER_PORT}) 2>/dev/null; then
            log "✅ 签名服务端口可达"
            break
        fi
        if [ "$i" = "30" ]; then
            log "⚠️  签名服务端口在 30s 内未就绪，继续启动主程序（运行时签名可能失败）"
        fi
        sleep 1
    done
fi

# ---- 3) 启动主程序 ----
cd /app
log "🚀 启动 NiceGUI 主程序 (端口 ${DOUYIN_PPP_APP_PORT:-9527})..."
exec python -u app.py