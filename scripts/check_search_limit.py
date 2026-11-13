"""端到端诊断：抖音 web 端搜索接口是不是被限流了

2026-11-12 用户反馈：
「999999101、999999102、999999103 我抽查了几个，用手机都能搜到，你这边是什么情况？」

真相
----
手机**确实**能搜到——限流的是 **web 端搜索接口**，而且是**静默**的：
不报错、不弹提示、状态码还是 0，就是给你一个空的 user_list
（实测 status_code=0、cursor=10、user_list=[]、has_more=0）。

所以光看「搜某人返回 0 个用户」分不清两种情况：
    a) 这个号真的不存在
    b) 搜索接口被限流了

这个脚本用**金丝雀探针**分清：
先搜一个一定搜得到的词（"小米"），再搜用户抽查的那三个号。

跑法：
    .venv/bin/python scripts/check_search_limit.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    Douyin, RiskControl, SearchUnavailable, normalize_kind,
)

# 用户抽查的三个号
TEST_IDS = ["999999101", "999999102", "999999103"]


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        dy = Douyin(page, log=lambda m: print(m))

        print(f"登录态: {'✅ 有 sessionid' if dy.cookies_ok() else '❌ 没登录态'}")
        if not dy.cookies_ok():
            print("❌ 触达浏览器没有登录态，先扫码登录")
            browser.close()
            return 1

        # ---- 1. 金丝雀探针：搜索接口还活着吗 ----
        print(f"\n{'=' * 60}\n=== 探针：搜「{Douyin.SEARCH_CANARY}」 ===")
        try:
            healthy = dy._probe_search_health(force=True)
        except RiskControl as e:
            print(f"  🔍 搜索触发了验证：{e}")
            healthy = False
        print(f"  → 搜索接口 {'✅ 正常' if healthy else '❌ 出不来结果'}")

        # ---- 2. 抽查的三个号 ----
        found = 0
        need_verify = False
        for q in TEST_IDS:
            print(f"\n{'=' * 60}\n=== {q} ===")
            try:
                info = dy.resolve(q)
            except RiskControl as e:
                kind = normalize_kind(getattr(e, "kind", ""))
                if kind == "verify":
                    need_verify = True
                    print(f"  🔐 抖音要求先过验证才能搜索：{e}")
                    print("     → 这是拼图滑块，不是限流、也不是号失效")
                    print("     → 网页「抖音触达」点「打开浏览器窗口」把拼图拖过去，"
                          "过完自动接着跑")
                else:
                    print(f"  ⚠️ 风控（{kind}）：{e}")
                continue
            except SearchUnavailable as e:
                print(f"  🔍 搜索接口被限流：{e}")
                print("     → 不是这个号不存在，是 web 端搜索接口出不来结果")
                print("     → 手机能搜到是因为 App 走的是另一套接口")
                continue
            except Exception as e:
                print(f"  ❌ 异常: {type(e).__name__}: {str(e)[:120]}")
                continue
            if info:
                found += 1
                print(f"  ✅ 搜到了：nickname={info.get('nickname')} "
                      f"unique_id={info.get('unique_id')} "
                      f"aweme_count={info.get('aweme_count')}")
            else:
                print(f"  ⚠️ 搜不到（搜索接口是通的 → 这个号可能真的不存在）")

        browser.close()

    print("\n" + "=" * 60)
    if need_verify:
        print("结论：抖音要求**先完成安全验证（拼图滑块）才能搜索**。")
        print("      程序已经能认出来并停下等你——以前它被当成「这些人都失效了」，")
        print("      一路空转下去。")
        return 3
    if not healthy:
        print("结论：抖音 **web 端搜索接口被限流**（账号是好的，手机也能搜）。")
        print("      系统会自动识别这种情况并停手，不会把限流误当成「人都失效了」。")
        return 2
    print(f"结论：搜索接口正常，{found}/{len(TEST_IDS)} 个号搜到了。")
    return 0 if found == len(TEST_IDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
