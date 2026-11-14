#!/usr/bin/env python3
"""探针：抖音 web 端到底能做什么

要回答三件事（用户 2026-11-14 要求）：
1. **没有作品**的人，主页上有没有「私信」按钮？（→ 能不能私信兜底）
2. **私密账号**长什么样？能不能判定？
3. web 端主页上有没有「加好友 / 添加朋友」按钮？
   （抖音的好友功能主要在 App，web 端大概率没有——要实地确认）

做法：拿几个真实线索的主页，dump 出
  · 页面上的「私密」相关提示
  · 所有**可见**按钮的文字
  · 私信按钮能不能点开、输入框能不能渲染出来

**只看不发**：不点发送、不留言、不加好友。
用法：
    .venv/bin/python scripts/probe_dm_friend.py [条数]
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402

DOUYIN = "https://www.douyin.com"

# 页面里出现这些字，就说明是「私密/不可见」类的账号
PRIVATE_HINTS = ("私密", "仅自己可见", "好友可见", "不可见", "你还未关注",
                  "关注后可见", "该用户已设置")


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    conn = sqlite3.connect("data/db/douyin_ppp.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, display_id, nickname, sec_uid FROM leads "
        "WHERE sec_uid != '' ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    print(f"profile: {profile}\n样本 {len(rows)} 条\n")

    from src.outreach.browser import Browser

    with Browser(profile, headless=False, slow_mo=0, window_mode="background") as (_ctx, page):
        page.goto(f"{DOUYIN}/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)

        for r in rows:
            print(f"\n===== #{r['id']} {r['display_id']} {r['nickname'] or ''} =====")
            try:
                page.goto(f"{DOUYIN}/user/{r['sec_uid']}",
                          wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"  打开失败：{str(e)[:80]}")
                continue
            page.wait_for_timeout(3500)

            body = ""
            try:
                body = page.inner_text("body") or ""
            except Exception:
                pass

            hits = [w for w in PRIVATE_HINTS if w in body]
            print(f"  私密相关提示：{hits or '无'}")
            if hits:
                # 把含这些词的行打出来 —— 「私密账号」和「私密作品」是两回事
                for line in body.splitlines():
                    line = line.strip()
                    if line and any(w in line for w in PRIVATE_HINTS):
                        print(f"      → {line[:70]}")

            # 所有可见按钮的文字
            try:
                btns = page.evaluate(
                    """() => Array.from(document.querySelectorAll('button, [role="button"], a.button'))
                         .filter(e => { const s = getComputedStyle(e);
                                        const r = e.getBoundingClientRect();
                                        return s.visibility !== 'hidden' && s.display !== 'none'
                                            && r.width > 2 && r.height > 2; })
                         .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
                         .filter(Boolean)"""
                )
            except Exception:
                btns = []
            uniq = []
            for b in btns:
                b = re.sub(r"\s+", " ", b)[:30]
                if b and b not in uniq:
                    uniq.append(b)
            print(f"  可见按钮：{uniq[:14]}")

            has_friend = any(("好友" in b) or ("朋友" in b) for b in uniq)
            print(f"  有没有「加好友/添加朋友」按钮：{'✅ 有' if has_friend else '❌ 没有'}")

            # 私信按钮能不能点开
            try:
                loc = page.locator('button:has-text("私信")')
                vis = None
                for i in range(min(loc.count(), 6)):
                    if loc.nth(i).is_visible():
                        vis = loc.nth(i)
                        break
                if vis is None:
                    print("  私信按钮：❌ 没有可见的")
                else:
                    vis.click(timeout=5000)
                    page.wait_for_timeout(3000)
                    has_input = page.evaluate(
                        """() => !!(document.querySelector('.messageMsgInputcontainer [contenteditable="true"]')
                                    || document.querySelector('[data-placeholder="发送消息"]'))"""
                    )
                    # 面板里抖音自己说的话 —— 「为什么发不了」都在这
                    panel = ""
                    try:
                        panel = page.evaluate(
                            """() => {
                                const ed = document.querySelector('.messageMsgInputcontainer [contenteditable="true"]')
                                       || document.querySelector('[data-placeholder="发送消息"]');
                                let p = ed;
                                for (let i = 0; i < 10 && p; i++) {
                                    const c = (p.className || '').toString();
                                    if (c.includes('componentsRightPanel') || c.includes('popupContainer')) break;
                                    p = p.parentElement;
                                }
                                return (p && p.innerText) ? p.innerText.slice(0, 300) : '';
                            }"""
                        ) or ""
                    except Exception:
                        pass
                    print(f"  私信面板：输入框={'有' if has_input else '❌ 没渲染出来'}")
                    if not has_input:
                        # 把面板/弹窗上的提示文字抓出来
                        try:
                            tip = page.evaluate(
                                """() => {
                                    const sels = ['.componentsRightPanel', '.popupContainer',
                                                  '[class*="popup"]', '[class*="modal"]'];
                                    for (const s of sels) {
                                        const e = document.querySelector(s);
                                        if (e && e.innerText && e.innerText.trim()) return e.innerText.trim().slice(0, 300);
                                    }
                                    return '';
                                }"""
                            ) or ""
                        except Exception:
                            tip = ""
                        print(f"      面板文字：{panel.strip()[:160]!r}")
                        print(f"      弹窗文字：{tip.strip()[:160]!r}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(800)
            except Exception as e:
                print(f"  私信按钮探测出错：{str(e)[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
