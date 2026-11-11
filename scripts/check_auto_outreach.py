"""端到端验证：自动触达开关能不能真的跑起来

用**干跑**模式验证 —— 干跑不发送任何评论、不回写任何状态，
所以对生产是安全的（而且这也是自动模式的默认模式）。

验证链路：
1. 页面上点开「开启」→ 配置要真的存进 config.yaml
2. 调度器要真的跑一轮（干跑）→ 以 **outreach_runs 表里多了一条 dry_run 记录**为准
3. 再点掉「开启」→ 配置恢复成关闭

为什么不看页面上的文字：一轮干跑 10 条要 5 分钟以上（每条都要搜人、
开主页、找作品、生成话术），页面文字要等跑完才更新，
等页面文字既慢又容易超时。直接看数据库更可靠。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:9527/outreach"
CFG = ROOT / "config.yaml"
DB = ROOT / "data" / "db" / "douyin_ppp.db"

# 一轮干跑 10 条，实测要 5-8 分钟
WAIT_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 900


def _cfg() -> dict:
    return yaml.safe_load(CFG.read_text()) or {}


def _last_run_id() -> int:
    conn = sqlite3.connect(str(DB))
    try:
        row = conn.execute(
            "SELECT id FROM outreach_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _run_info(run_id: int) -> dict:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, dry_run, sent, failed, skipped, note, started_at, finished_at "
            "FROM outreach_runs WHERE id = ?", [run_id]
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _toggle(page, text: str):
    page.evaluate(
        """(t) => {
          for (const el of document.querySelectorAll('.q-toggle')) {
            if ((el.innerText||'').trim().startsWith(t)) { el.click(); return true; }
          }
          return false;
        }""",
        text,
    )
    time.sleep(1.5)


def _status_text(page) -> str:
    return page.evaluate(
        """() => {
          const els = [...document.querySelectorAll('.text-caption')];
          const hit = els.find(e => (e.innerText||'').includes('自动触达'));
          return hit ? hit.innerText.trim() : '';
        }"""
    ) or ""


def main() -> int:
    ok = True
    before_id = _last_run_id()
    print(f"0) 验证前最新 run id = {before_id}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)

        print(f"1) 验证前 config.yaml 的 auto_mode = {_cfg().get('outreach', {}).get('auto_mode')}")

        # 把「每轮最多」调成 3 —— 验证要快，10 条要十几分钟
        page.evaluate("""() => {
          for (const el of document.querySelectorAll('.q-field')) {
            const lab = el.querySelector('.q-field__label');
            const inp = el.querySelector('input');
            if (!lab || !inp) continue;
            if ((lab.innerText||'').includes('每轮最多')) {
              inp.focus(); inp.value = '3';
              inp.dispatchEvent(new Event('input', {bubbles: true}));
              inp.dispatchEvent(new Event('change', {bubbles: true}));
              return true;
            }
          }
          return false;
        }""")
        time.sleep(1.5)
        print(f"   （每轮最多改成 3：{_cfg().get('outreach', {}).get('auto_max_per_run')}）")

        _toggle(page, "开启")
        saved = _cfg().get("outreach", {}).get("auto_mode")
        print(f"2) 点开「开启」后 auto_mode = {saved}")
        if saved is not True:
            print("   ❌ 开关没存进配置")
            ok = False
        else:
            print("   ✅ 开关已保存")
        print(f"   页面状态：{_status_text(page)}")

        print(f"3) 等调度器跑一轮干跑（最多等 {WAIT_SECONDS} 秒）…")
        new_id = 0
        for i in range(WAIT_SECONDS // 5):
            time.sleep(5)
            cur = _last_run_id()
            if cur > before_id:
                info = _run_info(cur)
                if info.get("dry_run") == 1:
                    new_id = cur
                    print(f"   ✅ 第 {(i+1)*5} 秒：新 run #{cur}（干跑）已开跑")
                    break
        if not new_id:
            print("   ❌ 没等到自动触达开跑")
            ok = False
        else:
            # 等它跑完
            for i in range(WAIT_SECONDS // 5):
                time.sleep(5)
                info = _run_info(new_id)
                if info.get("finished_at"):
                    print(f"   ✅ 跑完了：{info}")
                    break
            else:
                print(f"   （还没跑完：{_run_info(new_id)}）")

        _toggle(page, "开启")
        after = _cfg().get("outreach", {}).get("auto_mode")
        print(f"4) 点掉「开启」后 auto_mode = {after}")
        if after is not False:
            print("   ❌ 没恢复成关闭")
            ok = False
        else:
            print("   ✅ 已恢复成关闭")

        browser.close()

    print("\n全部通过 ✅" if ok else "\n有问题 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
