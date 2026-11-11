"""停手种类要结构化落库，服务重启后横幅不能丢

背景（2026-11-11 实测踩到）：

停手原因一直只存在两个地方——
1. 内存里的 ``runner._risk_stop``（进程一重启就没了）
2. ``outreach_runs.note`` 里的一段中文文案

第 2 种靠关键词去猜「这次是限流 / 要验证 / 登录失效」，但实际写进去的
文案有好几种，关键词根本对不全：

| 实际情况 | note 文案 | 含「验证码」？ | 含「限流」？ |
|---|---|---|---|
| 要本人验证 | 触发风控：抖音要求验证本人操作才能发评论（…） | ✗ | ✗ |
| 要本人验证 | 触发风控：跳转到安全验证页 | ✗ | ✗ |
| 账号限流 | 触发风控：页面提示：操作频繁 | ✗ | ✗ |
| 账号限流 | 触发风控：连续 2 条评论发出去但页面看不到，判定账号被限流（软风控） | ✗ | ✓ |

结果就是：**服务一重启、页面一刷新，最常见的两种停手横幅直接消失**——
用户不记得十分钟前跑过一次，只对着空气猜。

所以：kind 单独存一列，不再靠猜。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.outreach.runner import OutreachRunner
from src.outreach.store import OutreachStore


# ============================================================
# store 层：kind 要能写进去、能读出来
# ============================================================

def test_停手种类会落库(tmp_path):
    st = OutreachStore(tmp_path / "t.db")
    try:
        rid = st.start_run(dry_run=False)
        st.finish_run(rid, note="触发风控：抖音要求验证本人操作才能发评论", kind="verify")
        runs = st.recent_runs(limit=1)
        assert runs[0]["kind"] == "verify"
        assert runs[0]["note"].startswith("触发风控")
    finally:
        st.close()


def test_没停手时kind是空的(tmp_path):
    """正常跑完的一轮不该被当成停手"""
    st = OutreachStore(tmp_path / "t.db")
    try:
        rid = st.start_run(dry_run=True)
        st.finish_run(rid, note="干跑", kind="")
        runs = st.recent_runs(limit=1)
        assert runs[0]["kind"] == ""
    finally:
        st.close()


# ============================================================
# runner 层：重启后横幅要能恢复
# ============================================================

def _runner_with_db(db_path) -> OutreachRunner:
    r = OutreachRunner()
    r._db_path = lambda: str(db_path)
    return r


def _写一轮(db_path, note: str, kind: str = "") -> None:
    st = OutreachStore(db_path)
    try:
        rid = st.start_run(dry_run=False)
        st.finish_run(rid, note=note, kind=kind)
    finally:
        st.close()


def test_重启后能恢复停手横幅(tmp_path):
    """新记录：有 kind 列，直接用"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：抖音要求验证本人操作才能发评论", kind="verify")
    r = _runner_with_db(db)
    stop = r.last_risk_stop()
    assert stop.get("kind") == "verify", stop


def test_老记录靠文案回退能认出要验证(tmp_path):
    """老记录没有 kind 列，note 里也**不含**「验证码」三个字"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：抖音要求验证本人操作才能发评论（可用的验证方式：qr_code_verify）")
    r = _runner_with_db(db)
    stop = r.last_risk_stop()
    assert stop.get("kind") == "verify", stop


def test_老记录靠文案回退能认出限流(tmp_path):
    """老记录没有 kind 列，note 里也**不含**「限流」两个字"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：页面提示：操作频繁")
    r = _runner_with_db(db)
    stop = r.last_risk_stop()
    assert stop.get("kind") == "limit", stop


def test_老记录靠文案回退能认出登录失效(tmp_path):
    db = tmp_path / "t.db"
    _写一轮(db, "未登录：浏览器 profile 里没有 sessionid，先扫码登录")
    r = _runner_with_db(db)
    stop = r.last_risk_stop()
    assert stop.get("kind") == "login", stop


def test_正常跑完不显示横幅(tmp_path):
    db = tmp_path / "t.db"
    _写一轮(db, "干跑", kind="")
    r = _runner_with_db(db)
    assert r.last_risk_stop() == {}


def test_内存里的状态优先于数据库(tmp_path):
    """正在跑的时候以内存为准（数据库还没写）"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：页面提示：操作频繁", kind="limit")
    r = _runner_with_db(db)
    r._risk_stop = {"kind": "verify", "reason": "刚刚撞上验证"}
    assert r.last_risk_stop()["kind"] == "verify"


# ============================================================
# 冷却：只有真限流才锁人
# ============================================================

def test_要本人验证不该触发冷却(tmp_path):
    """验证 ≠ 限流，不该把人锁在冷却里干等两小时

    2026-11-11 实测踩到：#10 那轮要的是「本人操作验证」，
    但冷却逻辑只看 note 里有没有「风控」「限流」两个字 →
    「触发风控：抖音要求验证本人操作才能发评论…」也被当成限流，
    凭空卡了 120 分钟（用户 13:10 想跑，提示「还剩 25 分钟」，
    11:35 + 120 = 13:35，数字精确吻合）。
    """
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：抖音要求验证本人操作才能发评论"
                "（可用的验证方式：mobile_sms_verify、qr_code_verify）",
            kind="verify")
    r = _runner_with_db(db)
    assert r.risk_cooldown_left() == 0, "要验证不是限流，不该冷却"


def test_老记录里要验证的也不该冷却(tmp_path):
    """老记录没有 kind 列，靠文案判断也不能误判成限流"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：抖音要求验证本人操作才能发评论"
                "（可用的验证方式：qr_code_verify）")
    r = _runner_with_db(db)
    assert r.risk_cooldown_left() == 0


def test_真的限流还是要冷却(tmp_path):
    """限流保护不能丢——真被限流了还是要拦住"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：连续 2 条评论发出去但页面看不到，"
                "判定账号被限流（软风控），已停手。冷却 120 分钟后再试",
            kind="limit")
    r = _runner_with_db(db)
    left = r.risk_cooldown_left()
    assert left > 0, "真限流必须冷却"
    assert left <= 120


def test_老记录里真限流的还是要冷却(tmp_path):
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：页面提示：操作频繁")
    r = _runner_with_db(db)
    assert r.risk_cooldown_left() > 0


def test_干跑记录不触发冷却(tmp_path):
    db = tmp_path / "t.db"
    _写一轮(db, "干跑", kind="")
    r = _runner_with_db(db)
    assert r.risk_cooldown_left() == 0


# ============================================================
# 前后端词汇必须一致
# ============================================================

def test_网页横幅认得三种停手():
    """网页上的 kind 集合要跟后端发出来的一致

    出过一次这样的事：后端发 "verify"，网页写的是 "captcha"，
    结果撞验证时横幅**完全不显示**——用户只看到一行日志。
    """
    from src.outreach.douyin_api import KIND_LIMIT, KIND_LOGIN, KIND_VERIFY
    from src.web.outreach_page import STOP_KINDS

    assert set(STOP_KINDS) == {KIND_VERIFY, KIND_LIMIT, KIND_LOGIN}, (
        f"网页横幅的 kind 集合跟后端对不上：{STOP_KINDS}"
    )


def test_网页不再用旧的captcha词汇():
    """防止有人手滑把 "captcha" 写回去"""
    from src.web.outreach_page import STOP_KINDS

    assert "captcha" not in STOP_KINDS
    assert all(k in ("verify", "limit", "login") for k in STOP_KINDS)


# ============================================================
# 老库的迁移：线上库要先加列，不然 store 写入会报错
# ============================================================

def test_老库没有kind列时要能自动迁移(tmp_path):
    """模拟一个 v2.0.22 之前建的库（没有 kind 列）

    不迁移的话，finish_run 写 kind 会直接报 no such column ——
    线上库已经有一堆历史记录，不能让升级把触达搞挂。
    """
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE outreach_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dry_run INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            finished_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    try:
        # store 自己就把列补上了
        cols = {r[1] for r in st._conn.execute("PRAGMA table_info(outreach_runs)")}
        assert "kind" in cols, "老库要自动补上 kind 列"

        # 补完之后读写都正常
        rid = st.start_run(dry_run=False)
        st.finish_run(rid, note="触发风控：页面提示：操作频繁", kind="limit")
        assert st.recent_runs(limit=1)[0]["kind"] == "limit"
    finally:
        st.close()
