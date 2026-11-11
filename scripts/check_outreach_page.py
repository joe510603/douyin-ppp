"""实测：触达控制台页面上的元素是否都在

要验的：
1. 「打开浏览器窗口」按钮存在（撞验证码时自己把窗口叫出来）
2. 「我验证好了，继续」按钮存在
3. 等待条在空闲时是**隐藏**的（不该一进页面就顶着个橙色条）
4. 话术预览表能加载出数据

用真实浏览器跑，断言的是 DOM 而不是肉眼。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

URL = "http://127.0.0.1:9527/outreach"


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # 1 & 2：按钮在不在
        btns = page.eval_on_selector_all(
            "button",
            "els => els.map(e => e.innerText.trim()).filter(t => t.length)",
        )
        print(f"页面上的按钮: {btns}")
        has_show = any("打开浏览器窗口" in b for b in btns)
        has_resume = any("我验证好了，继续" in b for b in btns)

        # 3：等待条空闲时是隐藏的
        box_hidden = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('div'));
                const box = els.find(e => e.innerText &&
                    e.innerText.includes('打开浏览器窗口') &&
                    e.style && e.style.background &&
                    e.style.background.includes('255, 244, 229'));
                if (!box) return 'not-found';
                const r = box.getBoundingClientRect();
                const st = getComputedStyle(box);
                return {visible: r.width > 0 && r.height > 0,
                        display: st.display, visibility: st.visibility};
            }"""
        )
        print(f"等待条状态: {box_hidden}")

        # 4：话术预览有没有数据
        page.wait_for_timeout(2000)
        rows = page.eval_on_selector_all(
            "tbody tr", "els => els.length"
        )
        print(f"话术预览行数: {rows}")

        browser.close()

    hidden_ok = isinstance(box_hidden, dict) and not box_hidden.get("visible")
    print()
    print(f"「打开浏览器窗口」按钮:   {'✅' if has_show else '❌'}")
    print(f"「我验证好了，继续」按钮: {'✅' if has_resume else '❌'}")
    print(f"空闲时等待条隐藏:         {'✅' if hidden_ok else '❌'}  {box_hidden}")
    print(f"话术预览有数据:           {'✅' if rows > 0 else '❌'}  {rows} 行")
    return 0 if (has_show and has_resume and hidden_ok and rows > 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
