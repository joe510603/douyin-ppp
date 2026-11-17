#!/bin/bash
# ============================================================================
# 一键更新 douyin-ppp 到最新代码（含重启 + 自检）
#
# 用法：在**另一台电脑**上跑
#     bash scripts/update_code.sh
#
# 为什么需要它：
#   2026-11-17 修的「自家门店官方号被当成客户线索、程序给人家发留言广告」
#   这个 bug，只在**代码里**修好了。另一台电脑还跑旧代码的话，
#   它会继续给门店官方号发广告 —— 拉代码不等于修好，必须重启到新代码。
#
# 这个脚本做三件事：
#   1. 安全拉代码（本地有改动就停手，不冲掉人家正在改的东西）
#   2. 重启服务（launchd 拉起新代码）
#   3. **自检**：确认新代码真的生效了（而不是「以为更新了」）
# ============================================================================

set -uo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/Users/jack/github/douyin-ppp-main}"
STARTER="${STARTER:-$HOME/bin/start-douyin.sh}"
cd "$PROJECT_ROOT" || { echo "❌ 找不到项目目录: $PROJECT_ROOT"; exit 1; }

echo "=================================================="
echo " douyin-ppp 更新"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo " 目录: $PROJECT_ROOT"
echo "=================================================="

# ---------- 1. 拉代码 ----------
echo
echo "【1/4】检查本地状态"
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    echo "❌ 本地有未提交的改动，为安全起见先不更新。"
    echo "   请先处理（git status 看清楚要保留什么），再重跑本脚本。"
    git status --short
    exit 1
fi
if [ -n "$(git ls-files --others --exclude-standard)" ]; then
    echo "⚠️  有未跟踪的新文件（不影响更新，继续）"
fi
echo "✅ 工作区干净"

OLD_HEAD=$(git rev-parse --short HEAD)
echo "   更新前: $OLD_HEAD  $(git log -1 --format=%s)"

echo
echo "【2/4】拉取最新代码"
git fetch origin 2>&1 | tail -3
if ! git checkout main 2>&1 | tail -2; then
    echo "❌ 切分支失败"
    exit 1
fi
if ! git pull --ff-only origin main 2>&1 | tail -5; then
    echo "❌ 拉取失败（可能远端有分叉），**不要**用 reset --hard 强推，先人工看"
    git status
    exit 1
fi
NEW_HEAD=$(git rev-parse --short HEAD)
echo "   更新后: $NEW_HEAD  $(git log -1 --format=%s)"
if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    echo "   （本来就已经是最新）"
else
    echo "   ✅ 更新了 $OLD_HEAD → $NEW_HEAD"
fi

# ---------- 2. 重启 ----------
echo
echo "【3/4】重启服务"
if [ -x "$STARTER" ]; then
    bash "$STARTER" stop 2>&1 | tail -5
    sleep 3
    bash "$STARTER" start 2>&1 | tail -8
else
    echo "⚠️  找不到启动脚本 $STARTER，跳过自动重启"
    echo "   请手动重启（launchctl 或 start-douyin.sh）"
fi

echo
echo "   等待服务起来..."
READY=0
for i in $(seq 1 30); do
    sleep 2
    CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9527/ 2>/dev/null)
    if [ "$CODE" = "200" ]; then
        echo "   ✅ 网页起来了（$((i*2))s）"
        READY=1
        break
    fi
done
if [ "$READY" = "0" ]; then
    echo "   ⚠️  60 秒内网页没起来，请看日志：tail -50 $PROJECT_ROOT/data/logs/app.log"
fi

# ---------- 3. 自检：新代码真的生效了吗 ----------
echo
echo "【4/4】自检：确认关键修复已在运行"
FAILED=0

# 3a. 源码里必须有 secUid 排除逻辑
if grep -q "user_sec_id" src/intelligence/lead_service.py 2>/dev/null \
   && grep -q "_drop_own_accounts" src/outreach/store.py 2>/dev/null; then
    echo "   ✅ 自家账号排除逻辑（secUid）在代码里"
else
    echo "   ❌ 代码里没有自家账号排除逻辑 —— 版本不对！"
    FAILED=1
fi

# 3b. 数据库里不能再有自家账号混在待触达队列里
if [ -x ".venv/bin/python" ]; then
    .venv/bin/python - <<'PY'
import sqlite3, sys, yaml, os
root = os.getcwd()
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8')) or {}
own = {m['sec_user_id'] for m in (cfg.get('monitors') or []) if m.get('sec_user_id')}
db = 'data/db/douyin_ppp.db'
if not os.path.exists(db):
    print("   ⚠️  找不到数据库，跳过队列检查")
    sys.exit(0)
c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT id, nickname, outreach_status FROM leads "
    "WHERE outreach_status IN ('pending','not_found')"))
bad = [r for r in rows if r['id'] and
       (c.execute("SELECT sec_uid FROM leads WHERE id=?", (r['id'],)).fetchone() or [None])[0] in own]
if bad:
    print(f"   ❌ 待触达队列里有 {len(bad)} 条自家账号：")
    for r in bad:
        print(f"      #{r['id']} {r['nickname']} ({r['outreach_status']})")
    print("      → 跑 scripts/cleanup_own_account_leads.py 清理")
    sys.exit(1)
print(f"   ✅ 待触达队列 {len(rows)} 条，自家账号 0 条")
PY
    [ $? -ne 0 ] && FAILED=1
else
    echo "   ⚠️  找不到 .venv，跳过队列检查"
fi

# 3c. 检测链路自检
if grep -a "检测自检\|最终结果" data/logs/app.log 2>/dev/null | tail -3 | grep -q .; then
    echo "   ℹ️  最近检测自检："
    grep -a "检测自检\|最终结果" data/logs/app.log 2>/dev/null | tail -3 | sed 's/^/      /'
fi

echo
echo "=================================================="
if [ "$FAILED" = "0" ]; then
    echo " ✅ 更新完成，自检通过"
else
    echo " ⚠️  更新完成，但自检有没过的地方 —— 看上面 ❌ 那几行"
fi
echo "=================================================="
echo
echo "提醒：飞书表里「已留资」等状态要等回拉同步（约 1 分钟）才一致。"
echo "      另一台机器上的触达队列是独立的，两边都靠飞书表同步。"
