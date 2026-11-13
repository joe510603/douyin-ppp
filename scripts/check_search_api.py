"""诊断：抖音搜索为什么一个用户都搜不到

现象（2026-11-11 19:46）：干跑那轮 10 条，
每条都是「没搜到抖音号（搜索无用户结果）」，
但 17:21 用户自己那轮（发了 29 条）还是好的。

手工搜同一个号，页面返回的是**视频结果**（一堆视频卡片），
用户列表压根没出现。

要分清两种可能：
A. **接口层面**：搜索接口还在返回，但用户列表的块没了 / 形状变了
   → 解析要改
B. **页面层面**：抖音现在搜索默认落在「综合」tab，
   用户 tab 要手动点一下才请求用户接口
   → 操作流程要改（点用户 tab）

所以：把搜索过程的所有 XHR/fetch 都抓下来，
看有没有用户接口、响应里有没有 user_list / sec_uid。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402

DOUYIN = "https://www.douyin.com"
SEARCH_Q = "999999102"


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        seen: list[tuple[str, str]] = []

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                if "search" not in r.url.lower() and "discover" not in r.url.lower():
                    return
                body = ""
                try:
                    body = r.text()
                except Exception:
                    pass
                marks = []
                if "user_list" in body:
                    marks.append("user_list")
                if "sec_uid" in body:
                    marks.append("sec_uid")
                if "unique_id" in body:
                    marks.append("unique_id")
                seen.append((r.url, ",".join(marks) or "-", len(body)))
            except Exception:
                pass

        page.on("response", on_resp)
        page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)

        box = page.locator('input[placeholder*="搜索"]').first
        box.wait_for(state="visible", timeout=15000)
        box.click()
        page.wait_for_timeout(400)
        box.fill(SEARCH_Q)
        page.wait_for_timeout(600)
        page.keyboard.press("Enter")
        page.wait_for_timeout(8000)

        print(f"=== 按回车后抓到的搜索相关接口 ===")
        for url, marks, size in seen:
            print(f"  [{marks}] len={size}\n      {url[:160]}")
        if not seen:
            print("  （一个都没抓到！）")

        # 页面上有没有「用户」tab
        print("\n=== 页面上的 tab ===")
        tabs = page.evaluate(
            """() => {
              const out = [];
              for (const el of document.querySelectorAll('[class*="tab"], [role="tab"], [data-e2e*="tab"]')) {
                const t = (el.innerText||'').trim();
                if (t && t.length < 12) out.push({t, cls: el.className.slice(0,90)});
              }
              return out.slice(0, 25);
            }"""
        )
        for t in tabs:
            print(" ", t)

        # 点「用户」tab 再看一次
        print("\n=== 尝试点「用户」tab ===")
        clicked = page.evaluate(
            """() => {
              const cands = [...document.querySelectorAll('*')].filter(e => {
                const t = (e.innerText||'').trim();
                return (t === '用户' || t === '用户 tab') && e.children.length === 0;
              });
              if (!cands.length) return false;
              cands[0].click();
              return true;
            }"""
        )
        print("  点到了吗:", clicked)
        if clicked:
            page.wait_for_timeout(6000)
            print("  点完之后抓到的全部接口（含之前按回车的）：")
            for url, marks, size in seen:
                print(f"    [{marks}] len={size}\n        {url[:300]}")
            cards = page.evaluate(
                """() => {
                  const out = {};
                  for (const s of ['[data-e2e="search-user"]', '[class*="user-card"]',
                                   '[class*="UserInfo"]', 'li[role="listitem"]']) {
                    out[s] = document.querySelectorAll(s).length;
                  }
                  return out;
                }"""
            )
            print("  用户卡片数量:", cards)
            txt = page.inner_text("body")[:600]
            print("\n  页面文本前 600 字:\n", txt)

        shot = pathlib.Path("data/outreach_shots") / "diag_search_tab.png"
        page.screenshot(path=str(shot))
        print("\n截图:", shot)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
