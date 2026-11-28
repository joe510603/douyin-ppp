"""停手横幅必须会过期，飞书和网页必须说同一句话（2026-11-28）

实测经过
--------
2026-11-27 20:58 触发过一次 works 停手（``outreach_runs`` #263，kind=works）。
到 11-28 11:00 通道早就恢复了（#267 成功发出 1 条留言），
但用户 12:11 和 14:39 打开网页看到的还是那条横幅：

    🧱 抖音**作品列表接口**被限了，已停手。
    原因：连续 4 条都抓不到作品列表（主页压根没渲染出来……）

于是用户问「怎么还是有这个」。

两条根因
--------
1. **横幅不会过期**（主因）
   ``runner.last_risk_stop()`` 原来是「从最近 5 轮里找**第一条**带 kind 的」，
   完全不看此后有没有正常跑完的轮次 →
   昨晚的停手记录可以一直挂着，误导成「系统没修好」。

2. **网页文案和飞书不是一份**
   11-27 改 works 文案时只改了 ``worker.py``（飞书卡片），
   ``outreach_page.py``（网页横幅）还是旧版，
   于是同一件事两套互相矛盾的说法。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.outreach.runner import OutreachRunner
from src.outreach.stop_text import works_stop_text
from src.outreach.store import OutreachStore

REPO = Path(__file__).resolve().parents[1]


# ============================================================
# 工具
# ============================================================

def _runner_with_db(db_path) -> OutreachRunner:
    r = OutreachRunner()
    r._db_path = lambda: str(db_path)
    return r


def _写一轮(db_path, note: str, kind: str = "", diag: str = "",
           sent: int = 0) -> None:
    st = OutreachStore(db_path)
    try:
        rid = st.start_run(dry_run=False)
        st.finish_run(rid, sent=sent, note=note, kind=kind, diag=diag)
    finally:
        st.close()


_WORKS_NOTE = ("连续 4 条都抓不到作品列表（主页压根没渲染出来），已停手 —— "
               "这不是验证码、也不是登录问题；线索一条没丢（都退回队列了）")


# ============================================================
# 1. 横幅会过期
# ============================================================

def test_停手之后跑通一轮横幅就该消失(tmp_path):
    """核心回归：停手 → 通道恢复 → 横幅必须撤掉

    以前这里永远返回那条旧记录，于是用户以为「系统没修好」。
    """
    db = tmp_path / "t.db"
    _写一轮(db, _WORKS_NOTE, kind="works")
    # 通道恢复了：后来这一轮正常跑完，还发出去了 1 条
    _写一轮(db, "", kind="", sent=1)

    stop = _runner_with_db(db).last_risk_stop()
    assert stop == {}, stop


def test_最近一轮就是停手时横幅要显示(tmp_path):
    """别把「会过期」修成「永远不显示」"""
    db = tmp_path / "t.db"
    _写一轮(db, _WORKS_NOTE, kind="works")

    stop = _runner_with_db(db).last_risk_stop()
    assert stop.get("kind") == "works", stop
    assert "连续 4 条" in stop.get("reason", ""), stop


def test_干跑一轮也算通道正常(tmp_path):
    """干跑没撞停手 = 那会儿通道是通的 → 旧横幅过期"""
    db = tmp_path / "t.db"
    _写一轮(db, _WORKS_NOTE, kind="works")
    _写一轮(db, "干跑", kind="")

    assert _runner_with_db(db).last_risk_stop() == {}


def test_私信暂停不算整体停手(tmp_path):
    """dm = 只停私信通道，评论照发 → 不该弹「触达已停手」横幅"""
    db = tmp_path / "t.db"
    _写一轮(db, "🚫 抖音私信被限了（评论照发）", kind="dm")

    assert _runner_with_db(db).last_risk_stop() == {}


def test_老记录没kind列仍按文案猜(tmp_path):
    """老库没 kind 列的记录不能因此丢掉横幅（老数据兼容）"""
    db = tmp_path / "t.db"
    _写一轮(db, "触发风控：抖音要求验证本人操作才能发评论", kind="")
    # 模拟「老库没有 kind 列」：直接把 kind 清空
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE outreach_runs SET kind = ''")
        conn.commit()
    finally:
        conn.close()

    stop = _runner_with_db(db).last_risk_stop()
    assert stop.get("kind") == "verify", stop


# ============================================================
# 2. 诊断要落库（网页横幅靠它选文案）
# ============================================================

def test_诊断会落库(tmp_path):
    st = OutreachStore(tmp_path / "t.db")
    try:
        rid = st.start_run(dry_run=False)
        st.finish_run(rid, note=_WORKS_NOTE, kind="works", diag="session")
        runs = st.recent_runs(limit=1)
        assert runs[0]["diag"] == "session", runs[0]
    finally:
        st.close()


def test_诊断能一路带到横幅(tmp_path):
    db = tmp_path / "t.db"
    _写一轮(db, _WORKS_NOTE, kind="works", diag="session")

    stop = _runner_with_db(db).last_risk_stop()
    assert stop.get("diag") == "session", stop


def test_老库自动补diag列(tmp_path):
    """老库没有 diag 列时，_ensure_schema 要自己补上"""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE outreach_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dry_run INTEGER DEFAULT 0, total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, "
            "failed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, note TEXT DEFAULT '', "
            "started_at TEXT DEFAULT '', finished_at TEXT DEFAULT '', kind TEXT DEFAULT '')"
        )
        conn.commit()
    finally:
        conn.close()

    st = OutreachStore(db)
    try:
        rid = st.start_run(dry_run=True)
        st.finish_run(rid, note="干跑", kind="")
        assert st.recent_runs(limit=1)[0]["diag"] == ""
    finally:
        st.close()


# ============================================================
# 3. 飞书和网页必须用同一份文案
# ============================================================

def test_两种诊断给的是不同文案():
    t1, b1 = works_stop_text("session")
    t2, b2 = works_stop_text("endpoint")
    assert t1 != t2
    assert "主页没渲染" in t1
    assert "作品列表" in t2
    # 证据不足时（空 diag）要说保守的那种，别断言「整个会话降级」
    t3, _ = works_stop_text("")
    assert t3 == t2


def test_两处都引用同一份文案():
    """worker.py（飞书）和 outreach_page.py（网页）必须都从 stop_text 拿文案"""
    worker = (REPO / "src/outreach/worker.py").read_text(encoding="utf-8")
    page = (REPO / "src/web/outreach_page.py").read_text(encoding="utf-8")
    assert "works_stop_text" in worker
    assert "works_stop_text" in page
    # 旧文案不许再留在源码里（它就是用户看到的那段）
    assert "HTTP 200 但响应体是空的" not in worker
    assert "HTTP 200 但响应体是空的" not in page
