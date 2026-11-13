"""撞了抖音拼图验证之后的「冷却」

背景（2026-11-13 实测）
------------------------
当天的时间线：

    17:41  撞上拼图验证 → 停下等人（等 15 分钟）
    18:00  没人管，超时，这一轮结束
    18:09  新的自动轮又开一轮
    18:10  **又撞一次验证**

也就是说「超时 → 20 分钟后重开」这个节奏，正好落在抖音的验证冷却期里，
**两轮全白跑**：一轮干等 15 分钟没人管，一轮 1 分钟就撞墙。

抖音的拼图验证不是「等一下就好」的东西——它要你过验证，
过了之后账号还有一段冷却。在这段冷却里硬冲，只是重复撞墙，
还会把冷却期越拖越长。

所以加了两样东西：

1. **验证冷却**（记在 DB，跨重启不丢）：撞了验证又没人管 →
   接下来 N 分钟**不发起任何搜索**，到点自动重试。
2. **手动可以硬闯**：人手动点「开始触达」=「我人在电脑前」，
   这时候应该把浏览器开起来让他过拼图，而不是把他挡在门外。

盯住的：
- 冷却状态存 DB（重启不丢）、到期自动失效
- 冷却期内**自动轮**不跑
- 冷却期内**手动跑**照样能跑（否则人没法自己过验证）
- 人工点「我验证好了，继续」能解除冷却
- 冷却到期时给一张绿卡片告诉用户（不然用户以为程序挂了）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from src.config import get_config
from src.outreach.runner import OutreachRunner
from src.outreach.store import OutreachStore
from src.outreach.worker import OutreachWorker


# ============================================================
# 存储层：冷却状态
# ============================================================

def test_设了冷却就是激活状态(tmp_path):
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "撞了拼图验证")
        state = st.speed_state()
        assert state["verify_active"] is True
        assert state["verify_until"], "冷却截止时间要写进去"
        assert state["verify_reason"] == "撞了拼图验证"
    finally:
        st.close()


def test_解除冷却后不激活(tmp_path):
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "x")
        st.clear_verify_cooldown()
        state = st.speed_state()
        assert state["verify_active"] is False
        assert state["verify_until"] == ""
    finally:
        st.close()


def test_过期的冷却自动失效(tmp_path):
    """到期之后就不再挡路了——但「为什么进过冷却」要留着

    留着是为了让 runner 在冷却结束的那一刻能发一张「恢复了」的绿卡片，
    发完就清掉（clear_verify_cooldown 会把 reason 一起擦掉）。
    """
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "x")
        # 手动把截止时间改成过去
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        st._conn.execute(
            "UPDATE outreach_speed SET verify_until=? WHERE id=1", [past]
        )
        st._conn.commit()
        state = st.speed_state()
        assert state["verify_active"] is False, "过期了就不该再挡"
        assert state["verify_reason"] == "x", "进过冷却的原因要留着（用来通知恢复）"
    finally:
        st.close()


def test_冷却状态跨进程重启还在(tmp_path):
    """状态记在 DB 里，不是记在内存里

    内存状态一重启就没了 —— 2026-11-13 那次就是重启之后
    忘了「还在冷却期」，18:09 直接又开一轮，18:10 又撞验证。
    """
    db = tmp_path / "t.db"
    st = OutreachStore(db)
    try:
        st.set_verify_cooldown(45, "撞了拼图")
    finally:
        st.close()

    # 相当于服务重启：重新 new 一个 store（内存全丢）
    st2 = OutreachStore(db)
    try:
        assert st2.speed_state()["verify_active"] is True
    finally:
        st2.close()


def test_老库没有这一列也能自己补上(tmp_path):
    """老库的 outreach_speed 表没有 verify_until 列 → 自己 ALTER 补上"""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE outreach_speed (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "slow_until TEXT DEFAULT '', reason TEXT DEFAULT '', updated_at TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    try:
        st.set_verify_cooldown(30, "x")
        assert st.speed_state()["verify_active"] is True
    finally:
        st.close()


# ============================================================
# Runner：冷却期内挡自动轮、放手动轮
# ============================================================

def _runner(tmp_path, monkeypatch) -> OutreachRunner:
    cfg = get_config()
    cfg.outreach.enabled = True
    runner = OutreachRunner()
    # 把 DB 指到测试库
    monkeypatch.setattr(runner, "_db_path", lambda: str(tmp_path / "t.db"))
    return runner


def test_冷却剩余分钟数(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "x")
    finally:
        st.close()
    left = runner.verify_cooldown_left()
    assert 40 <= left <= 45, f"应该还剩 40~45 分钟，实际 {left}"


def test_没有冷却时剩余0(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    assert runner.verify_cooldown_left() == 0


def test_冷却期内开轮会被挡下来(tmp_path, monkeypatch):
    """自动轮在冷却期里必须停——这正是要修的那个 bug"""
    runner = _runner(tmp_path, monkeypatch)
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "撞了拼图")
    finally:
        st.close()

    called = []

    def _fake_run(*a, **k):
        called.append(1)
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(runner, "_run_in_thread", _fake_run)

    result = asyncio.run(runner.run(limit=1, dry_run=True))
    assert result["ok"] is False
    assert "冷却" in result["error"], result["error"]
    assert not called, "冷却期内不该真的跑一轮（更不该起浏览器）"


def test_手动跑可以绕过冷却(tmp_path, monkeypatch):
    """人手动点「开始触达」= 我人在电脑前 → 允许硬闯

    不这样的话，冷却期里用户想自己把拼图过了都做不到：
    浏览器根本没开，验证没地方过。
    """
    runner = _runner(tmp_path, monkeypatch)
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "撞了拼图")
    finally:
        st.close()

    called = []

    def _fake_run(*a, **k):
        called.append(1)
        return {"ok": True, "sent": 1, "dry": 1}

    monkeypatch.setattr(runner, "_run_in_thread", _fake_run)

    result = asyncio.run(runner.run(limit=1, dry_run=True,
                                     bypass_verify_cooldown=True))
    assert result["ok"] is True, result
    assert called, "手动跑应该真的开了一轮"


def test_人工点继续能解除冷却(tmp_path, monkeypatch):
    """用户手动过了拼图 → 冷却必须立刻解掉

    冷却是因为「撞了验证又没人管」才记上的；
    人都过了还冷却着，就是白等。
    """
    runner = _runner(tmp_path, monkeypatch)
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "撞了拼图")
    finally:
        st.close()

    assert runner.verify_cooldown_left() > 0
    runner.resume()          # 网页上「我验证好了，继续」
    assert runner.verify_cooldown_left() == 0, "点了继续就该解除冷却"


def test_冷却结束会通知一次(tmp_path, monkeypatch):
    """冷却到点 → 发一张绿卡片告诉用户「恢复了」

    冷却期间系统一声不吭（故意的：不搜就不该有动静）。
    用户回头一看「怎么没动静了」会以为程序挂了。
    """
    runner = _runner(tmp_path, monkeypatch)
    st = OutreachStore(tmp_path / "t.db")
    try:
        st.set_verify_cooldown(45, "撞了拼图")
        past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        st._conn.execute(
            "UPDATE outreach_speed SET verify_until=? WHERE id=1", [past]
        )
        st._conn.commit()
    finally:
        st.close()

    sent = []

    class _Notifier:
        def __init__(self, *a, **k):
            pass

        async def send_info_card(self, title="", lines="", at_open_id=""):
            sent.append((title, lines))
            return True, ""

    import src.notify.feishu as feishu
    monkeypatch.setattr(feishu, "FeishuNotifier", _Notifier)
    cfg = get_config()
    cfg.notify.risk_webhook_enabled = True
    cfg.notify.risk_webhook_url = "https://example.invalid/hook"

    asyncio.run(runner._expire_verify_cooldown())
    assert sent, "冷却结束应该发一张绿卡片"
    assert "恢复" in sent[0][0] or "继续" in sent[0][0], sent[0][0]

    # 再调一次不该重复发（冷却记录已经清了）
    sent.clear()
    asyncio.run(runner._expire_verify_cooldown())
    assert not sent, "冷却记录清了之后不该再发第二次"


# ============================================================
# Worker：验证没人管 → 进冷却
# ============================================================

def test_验证超时会进冷却(tmp_path):
    """撞了验证、这一轮没人管 → 记一段冷却"""
    cfg = get_config()
    cfg.outreach.captcha_wait_minutes = 0.01     # 0.6 秒
    cfg.outreach.verify_cooldown_minutes = 45
    logs = []
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=logs.append)
    try:
        w._enter_verify_cooldown("抖音要求先完成安全验证才能搜索")
    finally:
        w.st.close()

    st = OutreachStore(tmp_path / "t.db")
    try:
        state = st.speed_state()
        assert state["verify_active"] is True
        assert "冷却" in " ".join(logs), logs
    finally:
        st.close()


def test_冷却分钟数为0时不进冷却(tmp_path):
    """配成 0 = 关掉这个功能，不该拦路"""
    cfg = get_config()
    cfg.outreach.verify_cooldown_minutes = 0
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None)
    try:
        w._enter_verify_cooldown("x")
    finally:
        w.st.close()

    st = OutreachStore(tmp_path / "t.db")
    try:
        assert st.speed_state()["verify_active"] is False
    finally:
        st.close()


def test_默认等待时长是30分钟():
    """15 分钟太短了——撞验证时人往往不在电脑前

    15 分钟一到整轮作废，等人回来又得从头来一轮
    （重新起浏览器、重新挑 30 条、重新搜）。

    ⚠ 断言的是**类里的默认值**，不是全局单例：
    别的用例会把全局 config 改成 0.01/0.2 用来加速测试，
    读单例会互相干扰。
    """
    from src.config import OutreachConfig

    assert OutreachConfig.model_fields["captcha_wait_minutes"].default == 30
    assert OutreachConfig.model_fields["verify_cooldown_minutes"].default == 45
