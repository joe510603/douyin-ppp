"""诊断：为什么搜索「一个都搜不到」

现象（2026-11-11 19:46 实测）：干跑那一轮 10 条，
每条都是「⚠️ 没搜到抖音号 XXX（搜索无用户结果）」。
但同一天 17:21 用户自己跑的那轮发了 29 条 —— 搜索当时是好的。

要分清三种可能：
1. **登录态没了** → 页面会写「登录后即可搜索」
2. **搜索被限流** → 页面不报错，但结果为空 / 提示太频繁
3. **搜索逻辑坏了** → 页面有结果但我们没解析出来

所以直接把页面摊开看：页面文本、用户卡片数量、有没有限流/登录提示。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402

DOUYIN = "https://www.douyin.com"


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    print(f"profile: {profile}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        # 登录态
        cookies = page.context.cookies(DOUYIN)
        names = {c["name"] for c in cookies}
        print(f"cookie 数 {len(cookies)}　有 sessionid: "
              f"{'sessionid' in names or 'sessionid_ss' in names}")

        # 搜一个号
        q = "987714317"
        box = page.locator('input[placeholder*="搜索"]').first
        box.wait_for(state="visible", timeout=15000)
        box.click()
        page.wait_for_timeout(400)
        box.fill(q)
        page.wait_for_timeout(600)
        page.keyboard.press("Enter")
        page.wait_for_timeout(6000)

        print(f"\nURL: {page.url}")
        text = page.inner_text("body")[:1200]
        print("\n--- 页面文本（前 1200 字）---")
        print(text)

        for kw in ("登录后即可搜索", "操作频繁", "访问频繁", "请稍后再试",
                   "安全验证", "验证码", "没有找到", "搜索结果"):
            if kw in text:
                print(f"\n⚠️ 页面出现关键词：{kw}")

        # 用户卡片
        cards = page.evaluate(
            """() => {
              const sels = ['[data-e2e="search-user"]', '[class*="user-card"]',
                            '[class*="UserInfo"]', 'li[role="listitem"]'];
              const out = {};
              for (const s of sels) out[s] = document.querySelectorAll(s).length;
              return out;
            }"""
        )
        print(f"\n用户卡片数量: {cards}")

        shot = pathlib.Path("data/outreach_shots") / "diag_search_empty.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot), full_page=False)
        print(f"截图: {shot}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
