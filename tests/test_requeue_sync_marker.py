"""放回队列的线索不许被「飞书回拉」冲掉

背景（2026-11-14 实测）
------------------------
把一批「搜不到 / 没作品」的线索放回待触达队列，5 分钟内 30 条里有 26 条
又变回了 skipped —— 放回白做了。

根因
----
飞书回拉的判据（``lead_service.pull_bitable_status``）是：

    「本地状态 == 上次同步进飞书的状态」→ 认为本地没改过 → 采纳飞书的值

我们放回时改了本地的 ``outreach_status``（skipped → pending），
但**同步标记 ``bitable_synced_status`` 还是空**（从没同步过的那批）。
空标记会走「从没同步过 → 本地是默认值 → 采纳飞书」的分支，
而飞书上还写着「已跳过」→ 本地被改回去。

修法
----
放回时把同步标记一起设成**放回前的状态**（待处理|已跳过）：

- 标记 ≠ 当前值 → 回拉认为「本地改过」→ 保护本地 ✓
- 语义也对：飞书上确实还写着「已跳过」
- 下次这条线索被触达、写回飞书成功后，标记会重新对齐，
  销售在飞书里手动改状态的通道随之恢复 ✓

盯住的：
1. 放回后 pending + 标记是「待处理|已跳过」
2. 回拉的判据算出来是「本地改过」→ 不覆盖
3. 同时 ``status_changed`` 为 True → 该写回飞书（飞书和本地最终一致）
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.notify.bitable import OUTREACH_LABELS, STATUS_LABELS, status_changed  # noqa: E402

REQUEUE = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "requeue_skipped_leads.py"


def _建库(tmp_path):
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
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, not_found_count INTEGER DEFAULT 0, "
        " no_works INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
        " outreach_status, outreach_note, status, bitable_synced_status) "
        "VALUES (1, 'dy_x_1', '某人', 'MS4wLjABAAAAtest', '测试门店', "
        " 'skipped', '该用户没有公开作品（主页作品数为 0）', 'new', '')"
    )
    conn.commit()
    conn.close()
    return db


def _跑脚本(db, *argv):
    """跑放回脚本（改 sys.argv）"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("requeue_mod", REQUEUE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    old = sys.argv
    sys.argv = ["requeue_skipped_leads.py", "--db", str(db), "--apply", *argv]
    try:
        mod.main()
    finally:
        sys.argv = old


def _读(db, lead_id=1):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM leads WHERE id = ?", [lead_id]
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def test_放回后要带上同步标记(tmp_path):
    db = _建库(tmp_path)
    _跑脚本(db, "--reason", "作品")
    lead = _读(db)
    assert lead["outreach_status"] == "pending", lead
    assert lead["bitable_synced_status"], "同步标记必须一起设，否则会被回拉冲掉"


def test_放回的作品批次会打no_works标记(tmp_path):
    """标记「确认没作品」→ 下次进队列直接转私信，不搜索也不拉作品列表"""
    db = _建库(tmp_path)
    _跑脚本(db, "--reason", "作品")
    lead = _读(db)
    assert lead["no_works"] == 1, lead


def test_放回后回拉不会判成本地没改过(tmp_path):
    """回拉判据的核心：本地值 == 同步标记 → 才会被飞书覆盖

    这里本地是「待触达」、标记是「已跳过」→ 不相等 → 回拉**不覆盖**。
    """
    db = _建库(tmp_path)
    _跑脚本(db, "--reason", "作品")
    lead = _读(db)

    local_st = STATUS_LABELS.get(lead.get("status") or "", "待处理")
    local_os = OUTREACH_LABELS.get(lead.get("outreach_status") or "", "待触达")
    synced_st, _, synced_os = (lead.get("bitable_synced_status") or "").partition("|")

    assert synced_os, "同步标记不能为空（空 → 走「从没同步过」分支 → 被飞书覆盖）"
    local_unchanged = (local_st == synced_st.strip()) and (local_os == synced_os.strip())
    assert local_unchanged is False, (
        "回拉会认为「本地没改过」→ 采纳飞书上还写着「已跳过」→ 放回白做"
    )


def test_放回后本地确实变了要写回飞书(tmp_path):
    """保护本地的同时，飞书那边也要跟上（否则两边状态不一致）"""
    db = _建库(tmp_path)
    _跑脚本(db, "--reason", "作品")
    lead = _读(db)
    assert status_changed(lead) is True, "本地状态变了 → 下次同步应该写回飞书"
