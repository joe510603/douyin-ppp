"""端到端验证：搜抖音号能不能搜到人（修「用户 tab」之后）

2026-11-11 实测的 bug：
干跑那轮 10 条线索全部「搜索无用户结果」，
根因是**抖音搜索默认落在「综合」tab**，用户列表要手动点「用户」tab 才请求
    /aweme/v1/web/discover/search/?search_channel=aweme_user_web
才返回 user_list。

这个脚本用**真实的 Douyin.resolve()** 跑一遍，
验证的是代码路径本身（不是手写的复现逻辑）。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402

# 真实存在的抖音号（从待触达线索里挑）
TEST_IDS = ["999999102", "999999104", "999999105"]


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    ok = True

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        dy = Douyin(page, log=lambda m: print(m))

        print(f"登录态: {'✅ 有 sessionid' if dy.cookies_ok() else '❌ 没登录态'}")
        if not dy.cookies_ok():
            print("❌ 触达浏览器没有登录态，先扫码登录")
            return 1

        for q in TEST_IDS:
            print(f"\n=== 搜索 {q} ===")
            try:
                info = dy.resolve(q)
            except Exception as e:
                print(f"  ❌ 异常: {type(e).__name__}: {str(e)[:120]}")
                ok = False
                continue
            if info:
                print(f"  ✅ 搜到了：nickname={info.get('nickname')} "
                      f"unique_id={info.get('unique_id')} "
                      f"aweme_count={info.get('aweme_count')}")
            else:
                print("  ❌ 没搜到")
                ok = False

        browser.close()

    print("\n全部搜到 ✅" if ok else "\n有搜不到的 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
