"""搜索要**翻页**——目标常常不在第一页（2026-11-13）

现象
----
用户抽查被标「搜不到这个人」的线索，发现**全都搜得到**。
实测（走生产代码路径）：

    搜「999999111」  7.5s  ✅ 找到（昵称 测试昵称G）
    搜「test_user_e」 7.1s  ✅ 找到
    搜「999999107」     9.6s  ✅ 找到（测试昵称B）
    搜「999999109」  8.3s  ❌ 第一页里没有

根因
----
抖音一次搜索只给 **6~10 个用户**（响应里有 cursor / has_more），
目标常常在**第 2、3 页**——999999111 就是在 cursor=30 那页才出现的。

而旧代码等第一页的响应回来就下结论「这个人搜不到」，
于是线索被永久跳过（skipped 是终态）。

修法
----
首屏之后**继续往下滚**，逼抖音把下一页吐出来，
直到找到目标 / 抖音说没有下一页 / 翻页预算用完。
"""
from __future__ import annotations

import json

from src.outreach.douyin_api import Douyin


def _user(uid: str) -> dict:
    return {"user_info": {"sec_uid": f"sec-{uid}", "unique_id": uid,
                           "nickname": f"昵称{uid}", "aweme_count": 5}}


def _page(users: list[dict], cursor: int = 10, has_more: bool = True) -> str:
    return json.dumps({
        "status_code": 0, "cursor": cursor, "has_more": has_more,
        "user_list": users,
    })


class _FakeResp:
    def __init__(self, url: str, body: str):
        self.url = url
        self._body = body

    def text(self) -> str:
        return self._body


class _FakeMouse:
    def __init__(self, page):
        self._page = page

    def wheel(self, dx=0, dy=0):
        # 滚动 → 抖音加载下一页（实测就是这个行为）
        self._page.scrolls += 1
        self._page._emit_next()


class _FakePage:
    """假页面：首屏给一页，滚动一次给下一页"""

    def __init__(self, pages: list[str]):
        self.pages = pages
        self.handlers: list = []
        self.scrolls = 0
        self._emitted = 0

    def _emit_next(self):
        if self._emitted < len(self.pages):
            r = _FakeResp(
                "https://www.douyin.com/aweme/v1/web/discover/search/"
                "?search_channel=aweme_user_web",
                self.pages[self._emitted],
            )
            self._emitted += 1
            for h in list(self.handlers):
                h(r)

    def on(self, ev, h):
        if ev == "response":
            self.handlers.append(h)

    def remove_listener(self, ev, h):
        if h in self.handlers:
            self.handlers.remove(h)

    def wait_for_timeout(self, ms):
        if self._emitted == 0:
            self._emit_next()

    @property
    def mouse(self):
        return _FakeMouse(self)

    @property
    def url(self):
        return "https://www.douyin.com/jingxuan/search/x"


def _douyin(pages: list[str]) -> Douyin:
    return Douyin(_FakePage(pages))


# ============================================================
# 翻页
# ============================================================

def test_目标在第二页也能找到():
    pages = [
        _page([_user("u1"), _user("u2")], cursor=10, has_more=True),
        _page([_user("u3"), _user("target")], cursor=20, has_more=False),
    ]
    dy = _douyin(pages)

    got = dy._capture(
        ("discover/search",),
        lambda: None,
        timeout=3000,
        expect=lambda j: "user_list" in j,
        pager=dy._scroll_more_users,
        found=lambda parsed: any(
            u.get("unique_id") == "target"
            for u in dy._extract_users(parsed)
        ),
        page_budget=8000,
    )

    users = dy._extract_users(got)
    assert any(u.get("unique_id") == "target" for u in users), \
        f"翻页后应该能拿到目标，实际拿到：{[u.get('unique_id') for u in users]}"
    assert dy.page.scrolls >= 1, "应该往下滚过"


def test_目标在第一页就不用翻():
    pages = [_page([_user("target")], cursor=10, has_more=False)]
    dy = _douyin(pages)

    got = dy._capture(
        ("discover/search",),
        lambda: None,
        timeout=3000,
        expect=lambda j: "user_list" in j,
        pager=dy._scroll_more_users,
        found=lambda parsed: any(
            u.get("unique_id") == "target"
            for u in dy._extract_users(parsed)
        ),
        page_budget=8000,
    )
    users = dy._extract_users(got)
    assert any(u.get("unique_id") == "target" for u in users)
    assert dy.page.scrolls == 0, "第一页就命中，不该多滚"


def test_抖音说没有下一页就停():
    pages = [_page([_user("u1")], cursor=10, has_more=False)]
    dy = _douyin(pages)

    got = dy._capture(
        ("discover/search",),
        lambda: None,
        timeout=3000,
        expect=lambda j: "user_list" in j,
        pager=dy._scroll_more_users,
        found=lambda parsed: False,
        page_budget=8000,
    )
    assert dy._no_more_pages(got), "has_more=0 应该判定为没有下一页"
    assert dy.page.scrolls == 0, "没有下一页就不该再滚"


# ============================================================
# 「搜不到」不是终态
# ============================================================

def _建库(tmp_path) -> str:
    """建一个只够 OutreachStore 用的最小 leads 表"""
    import sqlite3

    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT NOT NULL DEFAULT '',
            display_id TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            car_model TEXT DEFAULT '',
            monitor_name TEXT DEFAULT '',
            store TEXT DEFAULT '',
            intent_level TEXT DEFAULT 'none',
            intent_score INTEGER DEFAULT 0,
            intent_reason TEXT DEFAULT '',
            evidence TEXT DEFAULT '[]',
            last_hit_at TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            outreach_status TEXT DEFAULT 'pending',
            outreach_at TEXT DEFAULT '',
            outreach_note TEXT DEFAULT '',
            source TEXT DEFAULT 'rule',
            bitable_record_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    conn.close()
    return path


def test_搜不到的人还留在队列里(tmp_path):
    """这次没搜到 ≠ 这个人没了 → 留在队列里，排在一堆正常线索后面"""
    from src.outreach.store import (
        STATUS_NOT_FOUND, STATUS_PENDING, OutreachStore,
    )

    db = _建库(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db)
    for i in (1, 2, 3):
        conn.execute(
            "INSERT INTO leads (id, display_id, nickname, outreach_status) "
            "VALUES (?, ?, ?, 'pending')", (i, f"u{i}", f"n{i}"),
        )
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    try:
        for i, stt in ((1, STATUS_PENDING), (2, STATUS_NOT_FOUND),
                       (3, STATUS_PENDING)):
            st.set_outreach_status(i, stt, "")
        rows = st.pending_leads()
    finally:
        st.close()

    ids = [r["id"] for r in rows]
    assert 2 in ids, "搜不到的人必须还能被选中（可重试）"
    # 而且要排在正常线索后面
    assert ids.index(2) == len(ids) - 1, f"not_found 应该排最后：{ids}"


def test_已留资的不在队列里(tmp_path):
    """拿到电话/微信的人别再留言了"""
    from src.outreach.store import STATUS_CONVERTED, OutreachStore

    db = _建库(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'u1', 'n1', 'pending')"
    )
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    try:
        st.set_outreach_status(1, STATUS_CONVERTED, "")
        rows = st.pending_leads()
    finally:
        st.close()

    assert not any(r["id"] == 1 for r in rows), "已留资的不该出现在触达队列"
