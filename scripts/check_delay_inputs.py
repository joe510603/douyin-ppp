"""实测：触达页上的「间隔」输入框是不是真的在页面上

用户反馈「每次发完要等将近2分钟才又去发，太慢了，改一下频率，让我自己能设置嘛」，
所以这两个框必须在页面上当场能填（不用跳去配置页）。

顺带检查：
1. 「最小间隔(秒)」「最大间隔(秒)」两个输入框存在
2. 默认是**空的**（留空 = 用配置里的值）
3. 填进去的值能被读到（说明没被 disabled / readonly）
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

URL = "http://127.0.0.1:9527/outreach"


def main() -> int:
    ok = True
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        labels = page.eval_on_selector_all(
            ".q-field__label, label",
            "els => els.map(e => e.innerText.trim()).filter(t => t.length)",
        )
        print(f"页面上的输入框标签: {labels}")

        for want in ("最小间隔(秒)", "最大间隔(秒)"):
            hit = any(want in l for l in labels)
            print(f"  {want:<14} {'✅' if hit else '❌'}")
            ok = ok and hit

        # 取这两个框的实际 DOM 值，确认是空的可填输入框
        info = page.evaluate(
            """() => {
              const out = [];
              for (const el of document.querySelectorAll('.q-field')) {
                const lab = el.querySelector('.q-field__label');
                const inp = el.querySelector('input');
                if (!lab || !inp) continue;
                const t = (lab.innerText || '').trim();
                if (t.includes('间隔')) {
                  out.push({label: t, value: inp.value, disabled: inp.disabled,
                            readonly: inp.readOnly});
                }
              }
              return out;
            }"""
        )
        print(f"间隔输入框: {info}")
        for i in info:
            if i["disabled"] or i["readonly"]:
                print(f"  ❌ {i['label']} 不能编辑")
                ok = False
            if i["value"]:
                print(f"  ⚠️ {i['label']} 默认不是空的（应该留空=用配置）")
                ok = False

        # 试填一下，确认真的能填进去
        filled = page.evaluate(
            """() => {
              for (const el of document.querySelectorAll('.q-field')) {
                const lab = el.querySelector('.q-field__label');
                const inp = el.querySelector('input');
                if (!lab || !inp) continue;
                if ((lab.innerText || '').includes('最小间隔')) {
                  inp.focus();
                  inp.value = '35';
                  inp.dispatchEvent(new Event('input', {bubbles: true}));
                  return inp.value;
                }
              }
              return null;
            }"""
        )
        print(f"  填入 '35' 后读回: {filled}")
        ok = ok and filled == "35"

        browser.close()

    print("\n全部通过 ✅" if ok else "\n有问题 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
