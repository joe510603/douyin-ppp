"""触达时从主页简介补联系方式（用户 2026-11-13 要求）

需求原话：「当搜索用户主页的时候，检查下他的简介内容，
有些客户会把联系方式写到简介里」

要钉住的行为：
  1. 简介里有电话/微信 → 写进线索的联系方式
  2. 写完同步飞书「联系方式」列
  3. **弹幕里已经有的同类联系方式不被简介覆盖**（弹幕是客户当场说的）
  4. 简介里的**不同类**（弹幕有电话、简介有微信）→ 追加，两个都留
  5. 简介里没联系方式 → 什么都不做（不写库、不写飞书）
  6. 简介是广告引流 → 照样记（那个微信号是他本人的，是真是同行让销售自己看）
"""
from __future__ import annotations

import sqlite3

from src.outreach.store import OutreachStore
from src.outreach.worker import OutreachWorker


# ============================================================
# 环境
# ============================================================

def _建库(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leads ("
        " id INTEGER PRIMARY KEY, display_id TEXT, nickname TEXT, "
        " outreach_status TEXT DEFAULT 'pending', outreach_note TEXT DEFAULT '', "
        " outreach_at TEXT DEFAULT '', contact_type TEXT DEFAULT '', "
        " contact_value TEXT DEFAULT '', sec_uid TEXT DEFAULT '', "
        " bitable_record_id TEXT DEFAULT '', created_at TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()
    return db


def _加线索(db, lead_id=1, display_id="u1", contact_type="", contact_value="",
            record_id=""):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, contact_type, contact_value,"
        " bitable_record_id) VALUES (?, ?, ?, ?, ?, ?)",
        [lead_id, display_id, "某人", contact_type, contact_value, record_id],
    )
    conn.commit()
    conn.close()


def _读(db, lead_id=1):
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT contact_type, contact_value FROM leads WHERE id = ?", [lead_id]
    ).fetchone()
    conn.close()
    return row


class _假飞书:
    def __init__(self):
        self.pushed: list[tuple[str, dict]] = []

    def push(self, record_id, fields):
        self.pushed.append((record_id, dict(fields)))


def _worker(tmp_path, db):
    from src.config import get_config

    cfg = get_config()
    w = OutreachWorker(cfg, str(db), log=lambda m: None)
    w._feishu = _假飞书()  # type: ignore[assignment]
    return w


def _线索(db_id=1):
    return {"id": db_id, "display_id": "u1", "nickname": "某人",
            "bitable_record_id": "rec1"}


# ------------------------------------------------------------ 存储层

def test_set_contact写入简介里的电话(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    changed, t, v = st.set_contact(1, "phone", "电话 13281975716")
    assert changed is True
    assert t == "phone"
    assert "13281975716" in v
    assert _读(db) == (t, v)


def test_set_contact同类不覆盖(tmp_path):
    """弹幕里已经有的电话，简介里的同类不覆盖它"""
    db = _建库(tmp_path)
    _加线索(db, contact_type="phone", contact_value="电话 13812345678")
    st = OutreachStore(db)
    changed, t, v = st.set_contact(1, "phone", "电话 13999998888")
    assert changed is False
    assert "13812345678" in _读(db)[1]


def test_set_contact异类追加(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, contact_type="phone", contact_value="电话 13812345678")
    st = OutreachStore(db)
    changed, t, v = st.set_contact(1, "wechat", "微信 abc123")
    assert changed is True
    assert t == "phone+wechat"
    assert "13812345678" in v and "abc123" in v


def test_set_contact空值不动(tmp_path):
    db = _建库(tmp_path)
    _加线索(db)
    st = OutreachStore(db)
    changed, _t, _v = st.set_contact(1, "", "")
    assert changed is False


# ------------------------------------------------------------ worker

def test_简介里的电话会写库并同步飞书(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact(
        {"signature": "隆广顺大量招收学员，加盟电话13281975716"}, _线索())
    t, v = _读(db)
    assert t == "phone"
    assert "13281975716" in v
    assert w._feishu.pushed == [("rec1", {"联系方式": v})]


def test_简介里的微信会写库(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact({"signature": "加V：xiao_ming_2024"}, _线索())
    t, v = _读(db)
    assert t == "wechat"
    assert "xiao_ming_2024" in v


def test_简介没有联系方式就不动(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact({"signature": "愿你走出半生，归来仍是少年"}, _线索())
    assert _读(db) == ("", "")
    assert w._feishu.pushed == []


def test_没有简介就不动(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact({"signature": ""}, _线索())
    assert _读(db) == ("", "")
    assert w._feishu.pushed == []


def test_弹幕已有的电话不被简介覆盖(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, contact_type="phone", contact_value="电话 13812345678",
            record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact({"signature": "加盟电话13999998888"}, _线索())
    t, v = _读(db)
    assert "13812345678" in v
    assert "13999998888" not in v
    # 没变 → 不该白跑一次飞书写入
    assert w._feishu.pushed == []


def test_弹幕有电话简介有微信两个都留(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, contact_type="phone", contact_value="电话 13812345678",
            record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact({"signature": "微信 abc123"}, _线索())
    t, v = _读(db)
    assert t == "phone+wechat"
    assert "13812345678" in v and "abc123" in v
    assert w._feishu.pushed[0][1] == {"联系方式": v}


def test_引流的简介也照记(tmp_path):
    """那个微信号是他本人的 —— 是真是同行让销售自己看内容判断"""
    db = _建库(tmp_path)
    _加线索(db, record_id="rec1")
    w = _worker(tmp_path, db)
    w._harvest_profile_contact(
        {"signature": "加微信了解详情，微信号 abc12345"}, _线索())
    t, v = _读(db)
    assert t == "wechat"
    assert "abc12345" in v


def test_没有飞书record_id也不报错(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, record_id="")
    w = _worker(tmp_path, db)
    lead = _线索()
    lead["bitable_record_id"] = ""
    w._harvest_profile_contact({"signature": "加盟电话13281975716"}, lead)
    assert _读(db)[0] == "phone"
    assert w._feishu.pushed == []
