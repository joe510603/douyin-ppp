#!/usr/bin/env python3
"""抖音触达控制台 —— 在终端实时看到每一步在干什么

为什么要有这个
--------------
网页上的「运行日志」要刷新才看得到，而触达是个长流程：
每条线索要等几十秒（限速）、失败要重试、命中风控要停下来。
不实时输出的话只能干等，不知道它到底卡在哪一步。

这个控制台把整个过程实时打到终端：

    ▶ 读取待触达线索…
       待触达 200 条 → 本次挑 3 条
    ② 启动浏览器（窗口在屏幕外）…
    [1/3] 42917865256　深夜的我　SU7　high
      搜索：42917865256
      ✓ 定位到 深夜的我（作品 67 条）
      ✓ 选中作品 7698682290624408683　「77年9月的蛇出来结合一下」
      ✍️ [price] 老板，看到你在直播间问小米汽车的价格，可以找我了解下…
      🧪 干跑，不发送、不回写
      ⏳ 干跑，等 6s 看下一条

用法::

    # 干跑（默认）：不发送、不改状态，只验证能不能找到人
    .venv/bin/python scripts/outreach_console.py --limit 3

    # 真发（需要配置页把「触达功能启用」打开）
    .venv/bin/python scripts/outreach_console.py --limit 3 --real

    # 只处理指定抖音号
    .venv/bin/python scripts/outreach_console.py --only 42917865256 --real

⚠️ 账号被判限流时冷却期内会拒绝启动（见 risk_cooldown_minutes）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_config  # noqa: E402


def _banner(text: str) -> None:
    print("\n" + "═" * 62, flush=True)
    print(f"  {text}", flush=True)
    print("═" * 62, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音触达控制台")
    ap.add_argument("--limit", type=int, default=3, help="本轮最多处理几条")
    ap.add_argument("--only", action="append", default=[], help="只处理指定抖音号，可重复")
    ap.add_argument("--real", action="store_true", help="真发（默认是干跑：不发送、不改状态）")
    ap.add_argument("--enable", action="store_true",
                    help="本次允许真发（等价于在配置页打开「触达功能启用」）")
    args = ap.parse_args()

    cfg = get_config()
    o = cfg.outreach

    _banner("抖音触达控制台")
    print(f"  模式      : {'真发（会公开留言）' if args.real else '干跑（不发送、不改状态）'}")
    print(f"  待发上限  : 每日 {o.daily_limit} 条 / 本轮 {args.limit} 条")
    print(f"  限速      : 两条之间 {int(o.min_delay)}~{int(o.max_delay)} 秒")
    print(f"  窗口      : {'屏幕外（看不见）' if o.hide_window and not o.headless else '可见'}")
    print(f"  风控冷却  : {o.risk_cooldown_minutes} 分钟", flush=True)

    if args.real and not o.enabled:
        if not args.enable:
            print("\n  ⚠️ 触达总开关是关的。")
            print("     真发需要先打开（配置页 → 抖音触达 → 触达功能启用），")
            print("     或加 --enable 只对这一次生效。")
            return 2
        # 只对本次进程生效，不写回 config.yaml
        o.enabled = True
        print("  → 已临时打开触达总开关（仅本次进程，不改配置文件）", flush=True)

    if not args.real:
        print("\n  提示：干跑只验证「找得到人、挑得到作品、话术对不对」，不会发评论。",
              flush=True)

    from src.outreach.runner import get_outreach_runner

    runner = get_outreach_runner()

    left = runner.risk_cooldown_left()
    if left > 0:
        print(f"\n  🛑 账号疑似被限流（软风控），冷却中，还剩 {left} 分钟。", flush=True)
        print("     先别跑，等风控过去再试。", flush=True)
        return 3

    _banner("开始")
    result = asyncio.run(
        runner.run(limit=args.limit, dry_run=not args.real, only=args.only or None)
    )

    _banner("结果")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)

    print("\n  详细日志见上面；最近 200 条也可以在网页上翻到。", flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
