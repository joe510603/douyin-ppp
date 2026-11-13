"""搜不到够多次就跳过（终态），留给人工验证

用户 2026-11-13 指示：「多次搜不到可以跳过，我后面来人工验证」

要钉住的：
  1. 第 1、2 次搜不到 → 还是 ``not_found``（**可重试**，排队列末尾）
  2. 搜够 N 次 → 变成 ``skipped``（**终态**，不再进队列）
  3. 跳过时「上次同步进飞书的状态」要对齐 —— 否则人工在飞书里把它
     改回「待触达」永远不生效，而「跳过 → 人工验证 → 放回」正是这个
     状态存在的意义
  4. **干跑不写计数**（干跑只是试一下，不该留下痕迹）
  5. 人工放回队列 → 计数清零，给一次全新机会
  6. 阈值可配置
  7. 连续 N 条全搜不到 → 照样停手（那是搜索坏了，不是这些人都失效）
"""

from __future__ import annotations

import sqlite3

from src.outreach.douyin_api import LeadNotFound
from src.outreach.store import OutreachStore
from src.outreach.worker import OutreachWorker


# ============================================================
# 环境
# ============================================================

def _建库(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    # 列跟真实的 leads 表对齐——pending_leads 会 SELECT 一堆列，
    # 少一列就 no such column（这正是老库补列那套机制要防的事）
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
        " store TEXT, last_hit_at TEXT, sec_uid TEXT)"
    )
    conn.commit()
    conn.close()
    return db


def _加线索(db, lead_id=1, display_id="u1", status="pending"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status, status) "
        "VALUES (?, ?, ?, ?, ?)",
        [lead_id, display_id, "某人", status, "new"],
    )
    conn.commit()
    conn.close()


def _读(db, lead_id=1):
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA table_info(leads)")
    row = conn.execute(
        "SELECT outreach_status, not_found_count, bitable_synced_status, outreach_note "
        "FROM leads WHERE id = ?",
        [lead_id],
    ).fetchone()
    conn.close()
    return row


class _假飞书:
    def __init__(self):
        self.pushed: list[tuple[str, dict]] = []

    def push(self, record_id, fields):
        self.pushed.append((record_id, dict(fields)))

    def flush(self, timeout: float = 30.0) -> bool:
        return True


def _worker(tmp_path, db, **cfg_over):
    from src.config import get_config

    cfg = get_config()
    cfg.outreach.dry_run = False
    cfg.outreach.max_per_run = 20
    cfg.outreach.daily_limit = 999
    cfg.outreach.fail_stop_threshold = 3
    cfg.outreach.not_found_stop_threshold = 99   # 「连续 N 条」那个阈值放很大
    cfg.outreach.not_found_skip_threshold = 3    # 「同一条累计 N 次」这个阈值
    cfg.outreach.min_delay = 0
    cfg.outreach.max_delay = 0
    cfg.outreach.dedupe_days = 0                 # 别让去重挡住重试
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    w = OutreachWorker(cfg, str(db), log=lambda m: None)
    w._feishu = _假飞书()  # type: ignore[assignment]
    return w


def _线索(n: int = 1, record_id: str = "") -> list[dict]:
    return [
        {"id": i, "lead_key": f"k{i}", "display_id": f"u{i}",
         "nickname": f"n{i}", "car_model": "", "intent_level": "medium",
         "intent_score": 30, "intent_reason": "", "evidence": "[]",
         "monitor_name": "", "bitable_record_id": record_id,
         "outreach_status": "pending",
         "status": "new"}
        for i in range(1, n + 1)
    ]


def _跑一轮(w, leads, monkeypatch, dry_run=False):
    """喂一堆线索 + 一串「搜不到」的剧本，跑一轮 worker.run

    直接把 worker 模块里的 Douyin 类换掉——浏览器整套不参与，
    测的只是「搜不到怎么计数、什么时候变终态」。
    """
    import src.outreach.worker as worker_mod

    class _FakeDouyin:
        def __init__(self, *a, **k):
            pass

        def cookies_ok(self):
            return True

        def check_risk(self):
            pass

        def resolve(self, unique_id, nickname=None):
            raise LeadNotFound("没搜到这个人")

        def clear_verify(self):
            pass

    monkeypatch.setattr(worker_mod, "Douyin", _FakeDouyin)

    class _Ctx:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    w._page_ctx = _Ctx
    w.st.pending_leads = lambda **kw: list(leads)
    w._sleep = lambda s: None
    return w.run(limit=len(leads), dry_run=dry_run)


# ============================================================
# 存储层
# ============================================================

def test_bump_not_found累加(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    assert st.bump_not_found(1) == 1
    assert st.bump_not_found(1) == 2
    assert st.bump_not_found(1) == 3


def test_reset_not_found_count清零(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    st.bump_not_found(1)
    st.bump_not_found(1)
    st.reset_not_found_count(1)
    assert _读(db)[1] == 0


def test_set_outreach_status能对齐同步标记(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    st.set_outreach_status(1, "skipped", "跳过了", synced_status="待处理|已跳过")
    status, _cnt, synced, _note = _读(db)
    assert status == "skipped"
    assert synced == "待处理|已跳过"


# ============================================================
# worker：搜不够次数 → 还能重试
# ============================================================

def test_第一次搜不到还是可重试(tmp_path, monkeypatch):
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db)
    _跑一轮(w, _线索(), monkeypatch)
    status, cnt, _synced, _note = _读(db)
    assert status == "not_found", "一次搜不到不能判死"
    assert cnt == 1


def test_第二次搜不到还是可重试(tmp_path, monkeypatch):
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db)
    for _ in range(2):
        _跑一轮(w, _线索(), monkeypatch)
    status, cnt, _synced, _note = _读(db)
    assert status == "not_found"
    assert cnt == 2


# ============================================================
# worker：搜够次数 → 跳过（终态）
# ============================================================

def test_搜够三次变终态(tmp_path, monkeypatch):
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=3)
    for _ in range(3):
        _跑一轮(w, _线索(), monkeypatch)
    status, cnt, _synced, note = _读(db)
    assert status == "skipped", "搜够 3 次还没搜到就该放弃"
    assert cnt == 3
    assert "人工验证" in note, note


def test_跳过之后不再进待触达队列(tmp_path, monkeypatch):
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=2)
    for _ in range(2):
        _跑一轮(w, _线索(), monkeypatch)
    assert _读(db)[0] == "skipped"

    # 真实的 pending_leads 查询（不 mock）——skipped 是终态，不该再被捞出来
    st = OutreachStore(db)
    ids = [r["id"] for r in st.pending_leads()]
    assert 1 not in ids, "跳过的线索不该再进队列"


def test_跳过时同步标记对齐人工能放回(tmp_path, monkeypatch):
    """回拉靠「上次同步进飞书的状态」判断本地改没改过

    不对齐 → 回拉永远认为「本地改过」→ 人工在飞书里把它改回「待触达」
    永远不生效。这个状态的意义就是「留给人工验证」，必须能放回去。
    """
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=1)
    _跑一轮(w, _线索(), monkeypatch)
    _status, _cnt, synced, _note = _读(db)
    assert synced == "待处理|已跳过", synced


def test_跳过会写飞书触达状态(tmp_path, monkeypatch):
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=1)
    _跑一轮(w, _线索(record_id="rec1"), monkeypatch)
    assert w._feishu.pushed, "跳过也要写回飞书，销售那边看得到"
    assert w._feishu.pushed[-1][1].get("触达状态") == "已跳过"


# ============================================================
# 干跑不写痕迹
# ============================================================

def test_干跑不写计数也不改状态(tmp_path, monkeypatch):
    """干跑是「试一下」，不该留下痕迹

    干跑写了计数 → 正式跑的时候第一次搜不到就立刻被跳过，
    等于干跑把线索给废了。
    """
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=1)
    for _ in range(3):
        _跑一轮(w, _线索(), monkeypatch, dry_run=True)
    status, cnt, _synced, _note = _读(db)
    assert status == "pending", "干跑不该改状态"
    assert cnt == 0, "干跑不该累加「搜不到」次数"


# ============================================================
# 人工放回队列 = 给一次全新机会
# ============================================================

def test_人工放回队列计数清零(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    st.bump_not_found(1)
    st.bump_not_found(1)
    st.reset_not_found_count(1)
    assert _读(db)[1] == 0
    # 放回队列后又能被捞出来
    ids = [r["id"] for r in st.pending_leads()]
    assert 1 in ids


# ============================================================
# 阈值可配置
# ============================================================

def test_阈值可配置(tmp_path, monkeypatch):
    """阈值设 5 → 第 5 次才跳过，前 4 次都还能重试"""
    db = _建库(tmp_path)
    _加线索(db)
    w = _worker(tmp_path, db, not_found_skip_threshold=5)
    for _ in range(4):
        _跑一轮(w, _线索(), monkeypatch)
    assert _读(db)[0] == "not_found"
    _跑一轮(w, _线索(), monkeypatch)
    assert _读(db)[0] == "skipped"


# ============================================================
# 人工在飞书里放回队列 → 回拉要能采纳
# ============================================================

class _假DB:
    def __init__(self, rows):
        self.updates: list[tuple[int, dict]] = []
        self._db = _假连接(rows)

    async def update_lead(self, lead_id, **fields):
        self.updates.append((lead_id, fields))
        return True


class _假连接:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, sql, params=None):
        return _假游标(self._rows)


class _假游标:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _假配置:
    class notify:
        bitable_enabled = True


def _回拉(monkeypatch, rows, 远端状态):
    from src.intelligence.lead_service import pull_bitable_status

    class _Writer:
        is_configured = True

        async def fetch_status_map(self):
            return {"u1": 远端状态}

        async def patch_fields(self, record_id, fields):
            return True, ""

    monkeypatch.setattr(
        "src.notify.bitable.get_bitable_writer", lambda config=None: _Writer()
    )
    db = _假DB(rows)
    import asyncio

    result = asyncio.run(pull_bitable_status(db=db, config=_假配置()))
    return result, db


def test_人工改回待触达会被采纳且计数清零(monkeypatch):
    """人工验证完，想把这条放回队列 → 回拉必须能采纳

    前提：跳过时把「上次同步进飞书的状态」对齐成了「待处理|已跳过」，
    所以回拉认为「本地没改过」→ 采纳飞书的值。
    """
    rows = [{
        "id": 1, "display_id": "u1", "status": "new",
        "outreach_status": "skipped",
        "bitable_synced_status": "待处理|已跳过",
        "bitable_record_id": "rec1",
    }]
    result, db = _回拉(monkeypatch, rows, {"处理状态": "待处理", "触达状态": "待触达"})
    assert result["updated"] == 1, result
    fields = db.updates[0][1]
    assert fields["outreach_status"] == "pending", fields
    assert fields["not_found_count"] == 0, "放回队列要清零，给一次全新机会"


def test_没对齐同步标记时人工改不动(monkeypatch):
    """反面对照：同步标记停在旧值 → 回拉认为本地改过 → 人工的改动不生效

    这就是「跳过时必须对齐同步标记」的原因。
    """
    rows = [{
        "id": 1, "display_id": "u1", "status": "new",
        "outreach_status": "skipped",
        "bitable_synced_status": "待处理|搜不到",   # 旧值：没对齐
        "bitable_record_id": "rec1",
    }]
    result, db = _回拉(monkeypatch, rows, {"处理状态": "待处理", "触达状态": "待触达"})
    assert result["updated"] == 0, result
    assert not db.updates
