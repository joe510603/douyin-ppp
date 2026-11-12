"""挖宝：搜索接口被限流时，综合 tab 的响应里还有没有用户信息？

背景（2026-11-12）：web 端搜索接口被静默限流——
    /aweme/v1/web/discover/search/?search_channel=aweme_user_web
    返回 status_code=0、cursor=10、user_list=[]

但按回车时抖音还会发另一个请求：
    /aweme/v1/web/general/search/single?search_channel=aweme_general
这个响应实测有 214KB——里面装的是**视频结果**。
视频结果里必然带作者（author），作者信息里就有 sec_uid！

如果能从这里把用户捞出来，就能**绕开被限流的用户 tab**。

这个脚本把搜索相关的所有响应都抓下来，
递归找出所有带 sec_uid 的字典，看能捞到几个、在哪个响应里。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from src.config import get_config  # noqa: E402

DOUYIN = "https://www.douyin.com"
Q = "987714317"


def 找用户(obj, out: dict, path: str = "") -> None:
    """递归找出所有带 sec_uid 的字典"""
    if isinstance(obj, dict):
        if obj.get("sec_uid"):
            uid = str(obj.get("unique_id") or obj.get("short_id") or "")
            out.setdefault(obj["sec_uid"], (uid, path))
        for k, v in obj.items():
            找用户(v, out, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            找用户(v, out, f"{path}[{i}]")


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            profile, headless=False, args=["--window-position=-3200,-3200"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        captured: list[tuple[str, str, str]] = []

        def on_resp(r):
            try:
                u = r.url or ""
                if "search" not in u:
                    return
                try:
                    body = r.text()
                except Exception:
                    body = ""
                captured.append((u, body, str(r.status)))
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            box = page.locator('input[placeholder*="搜索"]').first
            box.wait_for(state="visible", timeout=15000)
            box.click()
            page.wait_for_timeout(400)
            box.fill(Q)
            page.wait_for_timeout(600)
            page.keyboard.press("Enter")
            page.wait_for_timeout(6000)
            # 也点一下「用户」tab，看看被限流的那个接口
            page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('div,span,a,li')) {
                    const t=(el.innerText||'').trim();
                    if (t!=='用户' || el.children.length!==0) continue;
                    const r=el.getBoundingClientRect();
                    if (r.width<1||r.height<1) continue;
                    el.click(); return true;
                  }
                  return false;
                }"""
            )
            page.wait_for_timeout(8000)
        except Exception as e:
            print(f"操作出错: {str(e)[:120]}")

        print(f"\n共抓到 {len(captured)} 个跟 search 有关的响应\n")
        总用户: dict = {}
        for u, body, st in captured:
            短 = u.split("?")[0].replace("https://www.douyin.com", "")
            print(f"--- {短}  [status={st}, len={len(body)}]")
            if not body:
                print("    （响应体为空）")
                continue
            try:
                j = json.loads(body)
            except Exception as e:
                print(f"    不是 JSON: {str(e)[:60]}")
                continue
            if isinstance(j, dict):
                print(f"    顶层字段: {sorted(j.keys())[:14]}")
            found: dict = {}
            找用户(j, found)
            print(f"    带 sec_uid 的对象: {len(found)} 个")
            for sec, (uid, path) in list(found.items())[:8]:
                print(f"      uid={uid:<18} sec={sec[:22]:<22} 位置={path[:70]}")
                总用户.setdefault(sec, (uid, path, 短))
            if not found and isinstance(j, dict):
                d = j.get("data")
                if isinstance(d, list) and d:
                    first = d[0]
                    if isinstance(first, dict):
                        print(f"    data[0] 的字段: {sorted(first.keys())[:14]}")
                        aw = first.get("aweme_info")
                        if isinstance(aw, dict):
                            au = aw.get("author")
                            print(f"    aweme_info.author: "
                                  f"{sorted(au.keys())[:12] if isinstance(au, dict) else au}")

        print(f"\n{'=' * 60}")
        print(f"全部响应里一共捞到 {len(总用户)} 个带 sec_uid 的用户")
        for sec, (uid, path, src) in 总用户.items():
            print(f"  uid={uid:<18} sec={sec[:24]:<24} 来自 {src[:50]}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
