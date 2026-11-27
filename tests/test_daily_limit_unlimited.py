"""「自动触达不要设限制，可以一直触达」（2026-11-27 用户要求）

背景
----
触达有每日上限 ``daily_limit``（配置里是 100）。发满了就：
    · worker 直接收工（``daily_limit_reached``）
    · 调度器暂停到**次日 00:05**
而「网页上手动解除暂停」也没用 —— 解除完立刻又判「已发满」→ 又停。

实测（2026-11-27）：00:05 自动恢复 → 02:24 两小时发满 100 条 → 停手；
10:14 手动解除 → 10:14:17 立刻又判「今天已发 100 条」→ 再停。
用户要求：**不要设限制，可以一直触达**。

改法：``daily_limit = 0`` 表示**不限**。

盯住的：
1. ``daily_limit = 0`` → 不再收工、不再暂停，预算按单轮上限走
2. ``daily_limit = 0`` 时**不能**被 ``or 默认值`` 吞成 30
   （0 是 falsy → ``int(x or 30)`` 会把 0 变成 30）
3. 即时触达的每日额度判据也要认 0（否则 ``sent_today >= 0`` 永远成立
   → 即时触达一条都发不出去）
4. ``daily_limit > 0`` 时的老行为**不能变**（有上限仍然收工）
5. 私信额度仍然**单独**限额（陌生人私信被限会连留言一起废）
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.worker import OutreachWorker  # noqa: E402


def _建库(tmp_path, n: int = 3):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leads ("
        " id INTEGER PRIMARY KEY, lead_key TEXT, user_id TEXT, display_id TEXT, "
        " nickname TEXT, avatar TEXT, room_id TEXT, monitor_name TEXT, "
        " live_room TEXT, intent_level TEXT, intent_score INTEGER, "
        " intent_reason TEXT, car_model TEXT, contact_type TEXT, "
        " contact_value TEXT, evidence TEXT, evidence_count INTEGER, "
        " comment_count INTEGER, status TEXT DEFAULT 'new', pushed_at TEXT, "
        " outreach_status TEXT DEFAULT 'pending', outreach_at TEXT, "
        " outreach_note TEXT, created_at TEXT DEFAULT '', updated_at TEXT, "
        " source TEXT, bitable_record_id TEXT, bitable_synced_status TEXT, "
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, "
        " not_found_count INTEGER DEFAULT 0, no_works INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE outreach_records ("
        " id TEXT PRIMARY KEY, lead_id INTEGER, record_id TEXT, display_id TEXT, "
        " nickname TEXT, message TEXT, target_url TEXT, sec_uid TEXT, "
        " aweme_id TEXT, aweme_desc TEXT, "
        " comment_text TEXT, template_id TEXT, status TEXT, channel TEXT, "
        " error TEXT, verified INTEGER DEFAULT 0, "
        " created_at TEXT DEFAULT (datetime('now','localtime')), "
        " updated_at TEXT DEFAULT '')"
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
            " outreach_status, status) VALUES (?, ?, ?, ?, ?, 'pending', 'new')",
            (i, f"dy_{i}", f"昵称{i}", f"MS4wLjABAAAAtest{i}", "测试门店"))
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, **cfg_over):
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 1
    cfg.outreach.max_delay = 1
    cfg.outreach.auto_dry_run = False
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(_建库(tmp_path)), log=logs.append)
    w._dry = False
    return w, logs


def _不真开浏览器(w, monkeypatch):
    """run() 会去开浏览器 —— 测试里必须换掉（profile 也会被占用）"""
    from contextlib import contextmanager

    @contextmanager
    def _fake_page_ctx(profile_dir, headless, session):
        yield object()

    monkeypatch.setattr(w, "_page_ctx", _fake_page_ctx)


def test_零表示不限_不会被当成30(tmp_path, monkeypatch):
    """⚠️ `int(x or 30)` 会把 0 吞成 30 —— 这是最容易踩的坑"""
    w, logs = _worker(tmp_path, daily_limit=0)
    _不真开浏览器(w, monkeypatch)
    try:
        # 伪造「今天已经发了好几条」→ 不限的话照样要开跑
        for i in range(5):
            w.st.add_touch(lead_id=1, display_id=f"x{i}", status="sent",
                           channel="comment")
        assert w.st.sent_today() >= 0
        # 直接看 run() 的前置判断：不限时不该返回 daily_limit_reached
        res = w.run(limit=1, dry_run=True)
        assert not res.get("daily_limit_reached"), res
    finally:
        w.st.close()


def test_零表示不限_预算不按剩余额度算(tmp_path, monkeypatch):
    """不限时，单轮该挑多少挑多少（不受「今天还剩几条」限制）"""
    w, logs = _worker(tmp_path, daily_limit=0, max_per_run=3)
    _不真开浏览器(w, monkeypatch)
    try:
        res = w.run(limit=3, dry_run=True)
        # 干跑不会真的发，但名单应该被取出来
        assert res.get("total") == 3, res
        assert any("今日已发" in s and "/不限" in s for s in logs), \
            f"日志里应显示「不限」：{[s for s in logs if '今日已发' in s]}"
    finally:
        w.st.close()


def test_有上限时老行为不变(tmp_path, monkeypatch):
    """daily_limit > 0 仍然要收工（防风控的老逻辑不能被改坏）"""
    w, logs = _worker(tmp_path, daily_limit=1)
    _不真开浏览器(w, monkeypatch)
    try:
        w.st.add_touch(lead_id=1, display_id="already", status="sent",
                       channel="comment")
        assert w.st.sent_today() == 1
        res = w.run(limit=1, dry_run=False)
        assert res.get("daily_limit_reached") is True, res
        assert "每日上限" in str(res.get("stopped") or ""), res
    finally:
        w.st.close()


def test_即时触达的额度判据认零(tmp_path):
    """⚠️ 原来写 `sent_today >= int(o.daily_limit or 0)`：
    daily_limit=0 时算出 0，而 `sent_today >= 0` 永远成立
    → 即时触达会**一条都发不出去**。必须改成「有上限才拦」。
    """
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "outreach" / "instant_trigger.py").read_text(encoding="utf-8")
    assert "daily_limit > 0 and sent_today >= daily_limit" in src, \
        "即时触达的额度判据必须先判 daily_limit > 0"


def test_私信额度仍然单独限额():
    """私信是另一个通道，限流会连留言一起废 → 保留上限"""
    cfg = get_config()
    assert int(cfg.outreach.dm_daily_limit) > 0, "私信不能完全放开"


def test_网页上的每日上限能填零():
    """网页上那个输入框 min=1 的话，用户根本没法在界面上选「不限」"""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "src" / "web" / "config_page.py").read_text(encoding="utf-8")
    assert "每日留言上限（0=不限）" in src, "网页上要说明 0 = 不限"
    assert "outreach_daily.value if outreach_daily.value is not None else 0" in src, \
        "保存时不能用 `or 30`（0 会被吞成 30）"
