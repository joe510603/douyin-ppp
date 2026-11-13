"""诊断：搜索接口「返回空」到底是被限流，还是**搜索动作根本没执行**

背景（2026-11-13）
------------------
用户反馈：日志写着「搜索：测试昵称A」，但**浏览器页面上根本没有搜索动作**。
随后探针连「小米」这种一定搜得到人的词也返回空 → 程序判「被限流了」。

但「搜不到用户」至少有三种原因，代码却一律当成限流：
  A. 真被限流（抖音就是不给）
  B. **搜索框没找到 / 回车没触发** → 页面压根没发起搜索 → 一个响应都没有
  C. 「用户」tab 没点上 → 只有综合 tab 的响应 → 没有 user_list

这个脚本把三种情况**分开**：记录有没有抓到 discover/search 响应、
搜索后 URL 变成什么、页面正文有没有「限流/验证」的提示。

用法：
    .venv/bin/python scripts/diag_search_why.py 小米 999999106
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import DOUYIN, Douyin  # noqa: E402

SEARCH_URLS = ("discover/search", "general/search")


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    targets = sys.argv[1:] or ["小米"]
    print(f"profile: {profile}\n")

    seen: list[dict] = []

    with Browser(profile, headless=False, slow_mo=0, window_mode="background") as (_ctx, page):
        def _on_resp(r):
            try:
                url = r.url or ""
                if any(k in url for k in SEARCH_URLS):
                    body = ""
                    try:
                        body = r.text()
                    except Exception:
                        pass
                    seen.append({"url": url.split("?")[0], "status": r.status,
                                 "len": len(body), "body": body[:4000]})
            except Exception:
                pass

        page.on("response", _on_resp)
        dy = Douyin(page, log=lambda m: print("   ", m))

        for q in targets:
            seen.clear()
            print(f"═════ 搜「{q}」═════")
            t0 = time.time()

            # ---- 1. 页面到底有没有真的执行搜索 ----
            try:
                page.goto(DOUYIN + "/", wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"   首页打不开: {str(e)[:100]}")
                continue
            page.wait_for_timeout(1500)
            print(f"   首页 URL: {page.url}")
            box = page.locator('input[placeholder*="搜索"]').first
            try:
                print(f"   搜索框可见: {box.is_visible()}  placeholder="
                      f"{box.get_attribute('placeholder')!r}")
            except Exception as e:
                print(f"   搜索框定位失败: {str(e)[:100]}")

            # ---- 2. 走生产代码的搜索（带证据）----
            ev: dict = {}
            try:
                data = dy._search(q, expect_hit=False, evidence=ev)
                print(f"   _search 返回 {len(data)} 个响应，耗时 {time.time()-t0:.1f}s")
            except Exception as e:
                print(f"   _search 抛异常 {type(e).__name__}: {str(e)[:140]}")
                print(f"   耗时 {time.time()-t0:.1f}s")

            print(f"   搜索后 URL: {page.url}")
            print(f"   **证据**: {ev}")
            print(f"   抓到 {len(seen)} 个搜索相关响应:")
            for s in seen:
                info = ""
                try:
                    j = json.loads(s["body"])
                    ul = j.get("user_list")
                    nil = j.get("search_nil_info") or {}
                    info = (f"status_code={j.get('status_code')} "
                            f"cursor={j.get('cursor')} has_more={j.get('has_more')} "
                            f"user_list={len(ul) if isinstance(ul, list) else ul} "
                            f"nil_type={nil.get('search_nil_type')!r} "
                            f"顶层键={sorted(j.keys())[:12]}")
                except Exception:
                    info = f"body 前 120 字: {s['body'][:120]!r}"
                print(f"     - {s['url']} (HTTP {s['status']}, {s['len']} bytes)")
                print(f"       {info}")

            # ---- 3. 页面正文有没有限流/验证提示 ----
            body_txt = ""
            try:
                body_txt = page.evaluate(
                    "document.body ? document.body.innerText : ''") or ""
            except Exception:
                pass
            for kw in ("限流", "频繁", "验证", "滑块", "验证码", "稍后再试",
                       "操作频繁", "访问过于频繁"):
                if kw in body_txt:
                    i = body_txt.find(kw)
                    print(f"   ⚠️ 页面正文出现「{kw}」: …{body_txt[max(0,i-60):i+80]!r}")
            print(f"   正文前 200 字: {body_txt[:200]!r}")
            try:
                page.screenshot(path=f"/tmp/why_search_{q}.png")
                print(f"   截图: /tmp/why_search_{q}.png")
            except Exception:
                pass
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
