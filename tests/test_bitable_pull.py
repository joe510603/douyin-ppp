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

    ``touched`` 是「有过触达执行记录」的 lead_id 集合，
    对应 outreach_records 里出现过的 lead_id。
    """

    def __init__(self, rows, touched=()):
        self.updates: list[tuple[int, dict]] = []
        self._db = _FakeConn(rows, touched)

    async def update_lead(self, lead_id, **fields):
        self.updates.append((lead_id, fields))
        return True


class _FakeConn:
    def __init__(self, rows, touched=()):
        self._rows = rows
        self._touched = list(touched)

    async def execute(self, sql, params=None):
        # 有触达执行痕迹的 lead_id（SELECT DISTINCT lead_id FROM outreach_records）
        if "outreach_records" in sql:
            return _Cursor([(lid,) for lid in self._touched])
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
    state = {"map": {}, "err": None, "patched": []}

    class _Writer:
        is_configured = True

        async def fetch_status_map(self):
            if state["err"]:
                raise RuntimeError(state["err"])
            return state["map"]

        async def patch_fields(self, record_id, fields):
            state["patched"].append((record_id, fields))
            return True, ""

    monkeypatch.setattr("src.notify.bitable.get_bitable_writer", lambda config=None: _Writer())
    return state


def _row(lead_id, display_id, status="new", outreach_status="pending", synced="",
         record_id="rec1"):
    return {
        "id": lead_id,
        "display_id": display_id,
        "status": status,
        "outreach_status": outreach_status,
        "bitable_synced_status": synced,
        "bitable_record_id": record_id,
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


def test_pull_backfills_empty_feishu_status(fake_writer):
    """飞书端状态是空 → 把本地的值补写回去

    建记录时守卫跳过了状态字段，飞书表里那一格就是空白，
    销售看着会以为这条还没处理。
    """
    fake_writer["map"] = {"abc123": {"处理状态": "", "触达状态": ""}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["updated"] == 1
    assert len(fake_writer["patched"]) == 1
    rid, patch = fake_writer["patched"][0]
    assert rid == "rec1"
    assert patch["处理状态"] == "待处理"
    assert patch["触达状态"] == "待触达"


def test_pull_backfills_only_empty_field(fake_writer):
    """飞书端只有处理状态为空 → 只补那一个字段

    触达状态销售已经改了，不能覆盖。
    """
    fake_writer["map"] = {"abc123": {"处理状态": "", "触达状态": "已留言"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")])

    asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    _rid, patch = fake_writer["patched"][0]
    assert "处理状态" in patch
    assert "触达状态" not in patch


def test_pull_flags_suspect_when_no_touch_record(fake_writer):
    """飞书标了「已留言」但本地没有触达执行记录 → 要告警

    这种状态没有任何执行证据支撑：可能是销售手工跟进后标的（正常），
    也可能是调试残留写进去的脏数据（会让线索被误判为已触达而漏掉）。
    不自动推翻销售的标记，但必须计数并留日志。
    """
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "已留言"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")], touched=[])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["suspect"] == 1
    # 状态仍然照常回拉（不能因为可疑就丢掉销售的标记）
    assert result["updated"] == 1
    assert db.updates[0][1]["outreach_status"] == "commented"


def test_pull_no_suspect_when_touch_record_exists(fake_writer):
    """本地有触达执行记录 → 不是可疑状态，不告警"""
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "已留言"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")], touched=[1])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["suspect"] == 0


def test_pull_plain_status_is_not_suspect(fake_writer):
    """「已跳过」「失败」这类状态不参与可疑判定

    只有「已留言」「已定位」才代表"真的触达过"，才有执行痕迹可查。
    """
    fake_writer["map"] = {"abc123": {"处理状态": "待处理", "触达状态": "已跳过"}}
    db = _FakeDB([_row(1, "abc123", synced="待处理|待触达")], touched=[])

    result = asyncio.run(pull_bitable_status(db=db, config=_FakeConfig()))

    assert result["suspect"] == 0
