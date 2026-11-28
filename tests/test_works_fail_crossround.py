"""作品通道熔断必须**跨轮**累计 —— 否则即时触达永远触发不了告警

事故现场（2026-11-27 晚）
--------------------------
线上日志：17:58、18:00 连续两条线索都是

    「⏭️ 作品列表抓不到（抖音把作品接口降级了：HTTP 200 但响应体是空的）
      —— 这条退回队列等通道恢复」

抖音把「主页作品列表」接口降级了（17:55 之后就在降级，20:33 还在降），
但**飞书一张卡片都没发**，用户完全不知情。

根因：熔断计数器 ``consecutive_works_fail`` 是 ``run()`` 里的**局部变量**，
每轮开头清零。而「即时触达」每轮只处理 **1 条**新线索 →
每轮最多失败 1 次 → 永远到不了阈值 3 → 熔断从来没触发过。

而且新线索是**持续进来**的（直播间每分钟都有人问价），
所以每来一条新线索就重试一次、每次都静默失败 —— 故障被完全掩盖。

修法：计数存 DB（outreach_speed 表），跨轮累计；
抓取成功就清零；超过 30 分钟的旧账不算数（别把「现在第一次失败」
误报成「连续 N 次」）。

盯住的：
1. 连续 3 次（**跨轮**）抓不到 → 停手 + 飞书告警
2. 抓取成功 → 计数清零
3. 超过 30 分钟的失败不计入（陈旧计数不误报）
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import KIND_WORKS  # noqa: E402
from src.outreach.worker import OutreachWorker  # noqa: E402


class _FakeDouyin:
    """假装是个 Douyin：作品抓取按指定方式失败

    ``hits``/``empties`` 是抓取证据，``diag`` 是**结论**：
      · "session"  → 对照账号也打不开 → 整个会话被降级
      · "endpoint" → 会话正常，但取作品这条路没响应
    """

    def __init__(self, works_status: str = "failed", hits: int = 0,
                 empties: int = 0, diag: str = "endpoint"):
        self.works_status = works_status
        self.last_works_fetch = {
            "status": works_status, "detail": "",
            "hits": hits, "empties": empties, "diag": diag,
        }
        self.sent_dm: list[tuple[str, str]] = []

    def fetch_works(self, sec_uid: str, limit: int = 20) -> list:
        return []

    def resolve_by_sec_uid(self, sec_uid: str) -> dict:
        return {"sec_uid": sec_uid, "nickname": "昵称1", "aweme_count": 0,
                "signature": ""}

    def resolve(self, unique_id: str, nickname: str | None = None) -> dict:
        return self.resolve_by_sec_uid("MS4wLjABAAAAtest1")

    def send_dm(self, sec_uid: str, text: str) -> dict:
        self.sent_dm.append((sec_uid, text))
        return {"ok": True, "msg": "已发送", "is_private": False}

    def detect_private_account(self) -> bool:
        return False

    def _body_text(self) -> str:
        return ""

    def check_risk(self) -> None:
        return None

    def cookies_ok(self) -> bool:
        return True

    def set_control_account(self, sec_uid: str) -> None:
        """对照账号（2026-11-27）：真实 Douyin 用它分清「他没了」和「我们瞎了」。

        假对象不用真做对照，记下来就行（省得每个测试都去开一个对照页）。
        """
        self.control_sec_uid = sec_uid


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
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
            " outreach_status, status, no_works, last_hit_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 'new', 0, ?)",
            (i, f"dy_{i}", f"昵称{i}", f"MS4wLjABAAAAtest{i}", "测试门店",
             f"2026-11-27 10:0{i}:00"),
        )
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, n: int = 3, **cfg_over):
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.dm_fallback = True
    cfg.outreach.dm_daily_limit = 10
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 1
    cfg.outreach.max_delay = 1
    cfg.outreach.works_fail_stop_threshold = 3
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(_建库(tmp_path, n)), log=logs.append)
    w._dry = False
    return w, logs


def _跑一轮(w, dy, limit: int = 1, monkeypatch=None):
    @contextmanager
    def _fake_page_ctx(profile_dir, headless, session):
        yield object()

    if monkeypatch is not None:
        monkeypatch.setattr(w, "_page_ctx", _fake_page_ctx)
        monkeypatch.setattr("src.outreach.worker.Douyin",
                            lambda page, shot_dir=None, log=print: dy)
    return w.run(limit=limit)


def _lead(**kw) -> dict:
    lead = {
        "id": 1, "display_id": "dy_1", "nickname": "昵称1",
        "sec_uid": "MS4wLjABAAAAtest1", "store": "测试门店",
        "car_model": "小米SU7", "contact_type": "", "contact_value": "",
        "evidence": "[]", "monitor_name": "测试直播间",
        "bitable_record_id": "", "intent_level": "high", "intent_score": 90,
        "no_works": 0,
    }
    lead.update(kw)
    return lead


# ============================================================
# 1. 跨轮累计
# ============================================================

def test_每轮一条也累计得到阈值(tmp_path, monkeypatch):
    """核心回归：每轮只处理 1 条（即时触达就是这样），也要能累计到 3

    以前计数是 run() 里的局部变量 → 每轮清零 → 永远触发不了熔断
    → 作品接口降级了半小时，飞书一张卡片都没发。
    """
    w, logs = _worker(tmp_path, n=3)
    notified = []
    w._notify_risk = lambda kind, reason, lead=None, diag="": (
        notified.append((kind, reason, diag)) or True)
    try:
        dy = _FakeDouyin(works_status="failed", diag="session")
        # 第 1 轮：1 条失败 → 还没到阈值
        r1 = _跑一轮(w, dy, limit=1, monkeypatch=monkeypatch)
        assert r1.get("stop_kind") != KIND_WORKS, f"第 1 轮不该停手：{r1}"
        assert not notified, "第 1 轮不该告警"

        # 第 2 轮：又 1 条失败 → 累计 2
        r2 = _跑一轮(w, dy, limit=1, monkeypatch=monkeypatch)
        assert r2.get("stop_kind") != KIND_WORKS, f"第 2 轮不该停手：{r2}"
        assert not notified, "第 2 轮不该告警"

        # 第 3 轮：累计 3 → 停手 + 告警
        r3 = _跑一轮(w, dy, limit=1, monkeypatch=monkeypatch)
        assert r3.get("stop_kind") == KIND_WORKS, f"第 3 轮该停手：{r3}"
        assert notified, "该发飞书卡片告诉用户"
        kind, reason, diag = notified[0]
        assert kind == KIND_WORKS, kind
        assert "取不到作品" in reason, reason
        # 诊断要分对：主页压根没渲染 → 会话被降级（不是作品接口这一条路）
        assert diag == "session", diag
    finally:
        w.st.close()


def test_计数是存在DB里的跨轮状态(tmp_path):
    """计数不能只活在内存里 —— 服务重启也得记得"""
    w, _ = _worker(tmp_path)
    try:
        assert w.st.works_fail_count() == 0
        assert w.st.bump_works_fail() == 1
        assert w.st.bump_works_fail() == 2
        assert w.st.works_fail_count() == 2, "计数要留在 DB 里"
    finally:
        w.st.close()


# ============================================================
# 2. 抓取成功 → 清零
# ============================================================

def test_通道恢复后计数清零(tmp_path):
    """抓到作品了 → 通道是通的 → 旧账清掉

    不清零的话，通道恢复后第一次失败会被误报成「连续 N 次」，
    白白停手 + 白白发一张红卡片。
    """
    w, _ = _worker(tmp_path)
    try:
        w.st.bump_works_fail()
        w.st.bump_works_fail()
        assert w.st.works_fail_count() == 2
        w.st.clear_works_fail()
        assert w.st.works_fail_count() == 0
    finally:
        w.st.close()


def test_抓取成功会把计数清掉(tmp_path, monkeypatch):
    """走完整链路：先失败两次，再成功一次 → 计数归零"""
    w, _ = _worker(tmp_path, n=3)
    try:
        _跑一轮(w, _FakeDouyin(works_status="failed"), limit=1,
                monkeypatch=monkeypatch)
        _跑一轮(w, _FakeDouyin(works_status="failed"), limit=1,
                monkeypatch=monkeypatch)
        assert w.st.works_fail_count() == 2

        # 第三次：这次作品抓到了（ok）
        class _Ok(_FakeDouyin):
            def fetch_works(self, sec_uid: str, limit: int = 20) -> list:
                self.last_works_fetch = {"status": "ok", "detail": ""}
                return [{"aweme_id": "7703927024881327795", "desc": "作品",
                         "create_time": 1780000000, "is_top": False}]

        _跑一轮(w, _Ok("ok"), limit=1, monkeypatch=monkeypatch)
        assert w.st.works_fail_count() == 0, "通道通了，旧账就该清掉"
    finally:
        w.st.close()


# ============================================================
# 3. 陈旧计数不误报
# ============================================================

def test_超过半小时的旧账不算数(tmp_path):
    """一小时前失败过一次，现在第一次失败 —— 不能报成「连续 2 次」

    那么久以前失败过，说明通道大概率已经恢复过；
    拿它凑数会误停手 + 误发告警。
    """
    w, _ = _worker(tmp_path)
    try:
        w.st.bump_works_fail()
        # 把时间改成 40 分钟前
        w.st._conn.execute(
            "UPDATE outreach_speed SET works_fail_at = datetime('now','localtime','-40 minutes') "
            "WHERE id=1")
        w.st._conn.commit()
        assert w.st.works_fail_count() == 0, "过期的失败不该计入"
        assert w.st.bump_works_fail() == 1, "新一轮失败从 1 开始算"
    finally:
        w.st.close()


def test_半小时内的账算数(tmp_path):
    w, _ = _worker(tmp_path)
    try:
        w.st.bump_works_fail()
        w.st._conn.execute(
            "UPDATE outreach_speed SET works_fail_at = datetime('now','localtime','-10 minutes') "
            "WHERE id=1")
        w.st._conn.commit()
        assert w.st.works_fail_count() == 1, "半小时内的失败要算数"
    finally:
        w.st.close()


# ============================================================
# 4. 降级期间一条私信都不能发
# ============================================================

def test_降级期间绝不发私信(tmp_path, monkeypatch):
    """抓不到作品时发私信 = 把有作品的客户私信消耗掉（543 条误判的由来）"""
    w, _ = _worker(tmp_path, n=3)
    try:
        dy = _FakeDouyin(works_status="failed")
        _跑一轮(w, dy, limit=3, monkeypatch=monkeypatch)
        assert dy.sent_dm == [], f"绝不能发私信，实际 {dy.sent_dm}"
    finally:
        w.st.close()


# ============================================================
# 5. 诊断要按证据分两种，不能一律说「作品接口降级」
# ============================================================

def test_主页没渲染算会话降级(tmp_path, monkeypatch):
    """接口压根没响应 → 是**整个会话**被降级，不是作品接口这一条路

    实测（2026-11-27 20:48）：作品抓取证据是「接口命中 0 次、空响应体 0 次」，
    跟检测器报的「正文533字/用户不存在」是同一件事 —— 主页根本没渲染。
    这时还跟用户说「作品接口被限了」就是误导。
    """
    w, logs = _worker(tmp_path, n=3)
    try:
        dy = _FakeDouyin(works_status="failed", hits=0, empties=0, diag="session")
        _跑一轮(w, dy, limit=1, monkeypatch=monkeypatch)
        joined = " ".join(logs)
        assert "整个会话" in joined, joined
    finally:
        w.st.close()


def test_空响应体算接口降级(tmp_path, monkeypatch):
    """接口回了 200 但响应体是空 → 这一条路被降级"""
    w, logs = _worker(tmp_path, n=3)
    try:
        dy = _FakeDouyin(works_status="failed", hits=2, empties=2, diag="endpoint")
        _跑一轮(w, dy, limit=1, monkeypatch=monkeypatch)
        joined = " ".join(logs)
        assert "作品列表" in joined, joined
        assert "整个会话" not in joined, "会话正常时别说是会话降级"
    finally:
        w.st.close()


def test_诊断分类写进异常(tmp_path, monkeypatch):
    """diag 是结构化的（外层拼文案不许解析 str(e)）"""
    from src.outreach.douyin_api import WorksUnreachable

    w, _ = _worker(tmp_path, n=1)
    try:
        # hits=0 → session
        with pytest.raises(WorksUnreachable) as ei:
            w._handle_one(_FakeDouyin("failed", hits=0, empties=0,
                                      diag="session"),
                          _lead(), {"dry": 0})
        assert ei.value.diag == "session", ei.value.diag
        # empties>0 → endpoint
        with pytest.raises(WorksUnreachable) as ei2:
            w._handle_one(_FakeDouyin("failed", hits=1, empties=1,
                                      diag="endpoint"),
                          _lead(), {"dry": 0})
        assert ei2.value.diag == "endpoint", ei2.value.diag
    finally:
        w.st.close()


def test_会话正常时别把作品路没响应说成会话降级(tmp_path, monkeypatch):
    """2026-11-28 实测纠正：hits=0 **不等于**会话降级

    实测：对照账号（自家门店号）渲染得好好的、目标主页昵称也拿到了，
    但作品 XHR 一次都没发出来 —— hits 也是 0。
    这时候说「整个会话被降级了」就是误导（处置方式都不一样）。
    判据只有一个：**对照账号能不能渲染**。
    """
    from src.outreach.douyin_api import WorksUnreachable

    w, _ = _worker(tmp_path, n=1)
    try:
        dy = _FakeDouyin("failed", hits=0, empties=0)
        # 会话是好的（对照账号正常）
        dy.last_works_fetch = {"status": "failed", "detail": "接口命中 0 次",
                               "hits": 0, "empties": 0, "diag": "endpoint"}
        with pytest.raises(WorksUnreachable) as ei:
            w._handle_one(dy, _lead(), {"dry": 0})
        assert ei.value.diag == "endpoint", ei.value.diag
        assert "整个会话" not in str(ei.value), str(ei.value)
    finally:
        w.st.close()
