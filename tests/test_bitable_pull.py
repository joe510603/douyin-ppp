"""飞书表状态回拉测试 — 销售在飞书里改的状态能流回本地

覆盖 ``pull_bitable_status`` 的关键分支：
- 飞书改了、本地没改 → 采纳飞书的值
- 本地改了、飞书没改 → 保留本地的值，不被飞书冲掉
- 两边都没改 → 不动
- 从没同步过的老线索 → 采纳飞书的值
"""

import asyncio

import pytest

from src.intelligence.lead_service import pull_bitable_status


class _FakeDB:
    """最小可用的 DB 替身

    真实调用是 ``db._db.execute(...)``（aiosqlite 连接），
    所以这里让 ``self._db`` 指向一个带 execute 的对象。
    """

    def __init__(self, rows):
        self.updates: list[tuple[int, dict]] = []
        self._db = _FakeConn(rows)

    async def update_lead(self, lead_id, **fields):
        self.updates.append((lead_id, fields))
        return True


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=None):
        return _Cursor(self._rows)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _FakeConfig:
    class notify:
        bitable_enabled = True


@pytest.fixture
def fake_writer(monkeypatch):
    """伪造的写入器，返回预设的状态映射"""
    state = {"map": {}, "err": None}

    class _Writer:
        is_configured = True

        async def fetch_status_map(self):
            if state["err"]:
                raise RuntimeError(state["err"])
            return state["map"]

    monkeypatch.setattr("src.notify.bitable.get_bitable_writer", lambda config=None: _Writer())
    return state


def _row(lead_id, display_id, status="new", outreach_status="pending", synced=""):
    return {
        "id": lead_id,
        "display_id": display_id,
        "status": status,
        "outreach_status": outreach_status,
        "bitable_synced_status": synced,
    }


def test_pull_adopts_feishu_status(fake_writer):
    """销售在飞书标了「已跟进」→ 本地应该跟着变"""
    fake_writer["map"] = {"abc123": {"处理状态": "已跟进", "触达状态": "待触达"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 1
    assert db.updates[0][1]["status"] == "contacted"


def test_pull_adopts_feishu_outreach_status(fake_writer):
    """销售把触达状态改成「已跳过」→ 本地应该跟着变"""
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "已跳过"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 1
    assert db.updates[0][1]["outreach_status"] == "skipped"


def test_pull_does_not_override_local_change(fake_writer):
    """本地刚改成「已跟进」→ 不能被飞书端的旧值冲掉"""
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "待触达"}}
    # synced 是「待处理|待触达」，但本地当前是 contacted → 本地改过
    db = _FakeDB([_row(1, "abc123", status="contacted", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 0
    assert db.updates == []


def test_pull_skips_when_nothing_changed(fake_writer):
    """两边都是默认值 → 不用动"""
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "待触达"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 0
    assert db.updates == []


def test_pull_updates_synced_marker(fake_writer):
    """采纳飞书的值后，同步标记也要更新，否则下一轮会重复判断"""
    fake_writer["map"] = {"abc123": {"处理状态": "已跟进", "触达状态": "待触达"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert db.updates[0][1]["bitable_synced_status"] == "已跟进|待触达"


def test_pull_handles_never_synced_lead(fake_writer):
    """从没同步过的老线索（synced 为空）→ 采纳飞书的值"""
    fake_writer["map"] = {"abc123": {"处理状态": "已忽略", "触达状态": "待触达"}}
    db = _FakeDB([_row(1, "abc123", synced="")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 1
    assert db.updates[0][1]["status"] == "ignored"


def test_pull_ignores_unknown_display_id(fake_writer):
    """飞书里有本地没有的抖音号 → 跳过"""
    fake_writer["map"] = {"zzz999": {"处理状态": "已跟进", "触达状态": "待触达"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 0
    assert db.updates == []


def test_pull_reports_error(fake_writer):
    """写入器炸了要报错而不是抛异常"""
    fake_writer["err"] = "lark-cli 不存在"
    db = _FakeDB([])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["error"]
    assert result["updated"] == 0
