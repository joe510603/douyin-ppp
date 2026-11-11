"""端到端验证：撞上「要本人验证」时，触达该**停下叫人**，而不是误判成限流

这是 2026-11-11 那个 bug 的回归验证。

之前：
    抖音弹「接收短信验证码」→ 我们没认出来 → 继续跑
    → 回查找不到 → 误判「账号被限流」→ 停手 + 冷却 120 分钟 + 通知「扫码没用」

现在应该：
    抖音弹「接收短信验证码」→ 立刻认出 account_flow=verify
    → 抛 RiskControl(kind="verify") → 触发飞书通知 + 停下等人工验证

而且**不能**触发「软风控」计数（那是误判）。

    .venv/bin/python scripts/check_verify_stop.py
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402
from src.outreach.worker import OutreachWorker  # noqa: E402

AWEME_ID = "7693153770800486054"
TEXT = "老板，你在问小米汽车多久能提车对吧？我这边能帮你查下现在的交付节奏～"


def main() -> int:
    cfg = get_config()
    events: list[dict] = []
    notified: list[str] = []

    # 拦住飞书通知，别真发消息出去
    import src.notify.lark_im as lark_im

    orig = lark_im.get_im_notifier

    def _fake_notifier(*a, **k):
        class N:
            is_configured = True

            def send_text(self, t):
                notified.append(t)
                return True, ""

        return N()

    lark_im.get_im_notifier = _fake_notifier

    cfg.outreach.captcha_wait_minutes = 0.05      # 等 3 秒就超时
    w = OutreachWorker(cfg, "data/db/douyin_ppp.db", log=lambda m: print(m, flush=True),
                       on_event=lambda e: events.append(e))
    w._notify_risk = lambda kind, reason, lead=None: (
        notified.append(f"[{kind}] {reason}") or True)

    from src.outreach.browser import Browser

    with Browser(pathlib.Path("data/outreach_profile"), headless=False,
                 window_mode="background", block_media=True) as (ctx, page):
        dy = Douyin(page, shot_dir=pathlib.Path("data/outreach_shots"),
                    log=lambda m: print(m, flush=True))
        try:
            res = dy.post_comment(AWEME_ID, TEXT)
            print(f"\n▶ post_comment 返回: {res}")
            kind = "none"
        except Exception as e:
            kind = getattr(e, "kind", "")
            print(f"\n▶ post_comment 抛出: {type(e).__name__}: {e}  (kind={kind})")

        print(f"\n▶ 平台裁决 account_flow = {dy.verify_flow!r}")
        print(f"▶ 可用验证方式 = {dy.verify_ways}")

    lark_im.get_im_notifier = orig

    print("\n" + "=" * 66)
    ok_kind = kind == "verify"
    ok_flow = dy.verify_flow == "verify"
    print(f"被识别为「要人工验证」而不是限流: {'✅' if ok_kind else '❌'}  kind={kind}")
    print(f"平台裁决读到了 verify:              {'✅' if ok_flow else '❌'}  {dy.verify_flow}")
    print(f"验证方式里包含扫码:                  "
          f"{'✅' if 'qr_code_verify' in dy.verify_ways else '❌'}  {dy.verify_ways}")
    return 0 if (ok_kind and ok_flow) else 1


if __name__ == "__main__":
    raise SystemExit(main())
