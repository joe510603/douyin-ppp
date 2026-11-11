"""对比两种修法：URL 带参数 vs 点「用户」tab

已知事实（2026-11-11 实测）：
- 按回车 → 请求 general/search/stream（空）+ general/search/single（视频结果）
- 点「用户」tab → 请求 discover/search/?search_channel=aweme_user_web
  → 响应里**才有** user_list

要选一个稳的：
A. 直接访问 https://www.douyin.com/search/{q}?type=user
B. 搜索框 + Enter + 点「用户」tab

哪个更稳、DOM 依赖更少，用哪个。
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402

DOUYIN = "https://www.douyin.com"
Q = "987714317"


def _抓用户数(page, hits, marker):
    """返回 (接口命中数, 解析出的用户数)"""
    users = []
    for url, body in hits:
        if "discover/search" not in url:
            continue
        try:
            import json
            j = json.loads(body)
        except Exception:
            continue
        for it in (j.get("data") or []):
            if isinstance(it, dict):
                for u in (it.get("user_list") or []):
                    info = u.get("user_info") if isinstance(u.get("user_info"), dict) else u
                    if isinstance(info, dict) and info.get("sec_uid"):
                        users.append(info)
    return len(users)


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        hits: list[tuple[str, str]] = []

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                if "discover/search" not in r.url:
                    return
                try:
                    hits.append((r.url, r.text()))
                except Exception:
                    pass
            except Exception:
                pass

        page.on("response", on_resp)

        # ---------- 方案 A：URL 带 type=user ----------
        print("=== 方案 A：直接访问 ?type=user ===")
        hits.clear()
        page.goto(f"{DOUYIN}/search/{Q}?type=user",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(7000)
        n = _抓用户数(page, hits, "A")
        print(f"  接口命中 {len(hits)} 次，解析出用户 {n} 个")
        txt = page.inner_text("body")
        print(f"  页面里有「抖音号: 987714317」: {'987714317' in txt}")
        print(f"  页面文本前 200: {txt[:200]!r}")
        if n:
            print("  ✅ 方案 A 可行")

        # ---------- 方案 B：搜索框 + Enter + 点用户 tab ----------
        print("\n=== 方案 B：搜索框 + Enter + 点「用户」tab ===")
        hits.clear()
        page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        box = page.locator('input[placeholder*="搜索"]').first
        box.wait_for(state="visible", timeout=15000)
        box.click()
        page.wait_for_timeout(400)
        box.fill(Q)
        page.wait_for_timeout(600)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        # 找「用户」tab：精确文本 + 可见
        info = page.evaluate(
            """() => {
              const cands = [];
              for (const el of document.querySelectorAll('div,span,a,li')) {
                const t = (el.innerText||'').trim();
                if (t !== '用户') continue;
                if (el.children.length !== 0) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) continue;
                cands.push({
                  cls: el.className,
                  parentCls: el.parentElement ? el.parentElement.className : '',
                  tag: el.tagName,
                  w: Math.round(r.width), h: Math.round(r.height),
                });
              }
              return cands;
            }"""
        )
        print(f"  候选「用户」tab 元素: {info}")
        clicked = page.evaluate(
            """() => {
              for (const el of document.querySelectorAll('div,span,a,li')) {
                const t = (el.innerText||'').trim();
                if (t !== '用户' || el.children.length !== 0) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) continue;
                el.click();
                return true;
              }
              return false;
            }"""
        )
        print(f"  点到了吗: {clicked}")
        if clicked:
            page.wait_for_timeout(6000)
            n = _抓用户数(page, hits, "B")
            print(f"  接口命中 {len(hits)} 次，解析出用户 {n} 个")
            if n:
                print("  ✅ 方案 B 可行")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
