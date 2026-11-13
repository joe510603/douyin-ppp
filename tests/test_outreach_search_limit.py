"""搜索被限流 ≠ 搜不到人（2026-11-12）

用户反馈：
「G1808575996、987714317、53722928132 我抽查了几个，用手机都能搜到，你这边是什么情况？」

真相
----
手机**确实**能搜到——限流的是抖音 **web 端搜索接口**，而且它是**静默**的：
不报错、不弹提示、状态码还是 0，就是给你一个空的 user_list
（实测 status_code=0、cursor=10、user_list=[]、has_more=0）。

所以「搜某人返回 0 个用户」有两种完全不同的含义，从单次搜索里分不出来：

    a) 这个号真的不存在（改名/注销了）→ 正常业务结果，跳过，不算故障
    b) 搜索接口被限流了                  → 真故障，该停手

分不清的后果：
  · 被限流时，程序以为「这些人都失效了」，接着往下跑 → 纯空转
  · 真失效的号混在限流里，看不出到底是哪回事

解法：**金丝雀探针**。再搜一个一定搜得到的词（"小米"），
      探针也空 → 接口坏了（SearchUnavailable，停手）
      探针有 → 接口是好的，人不在结果里（LeadNotFound，跳过）

另外这一轮加了两件事：
  · ``SearchUnavailable`` 是**独立**的停手种类（search），
    **不触发账号限流冷却**——账号是好的，扫码也没用，方向完全错
  · 记住 sec_uid → 下次直接开主页，**跳过搜索**（搜索次数本身就是被限流的资源）

要盯住的：
1. 探针能区分「接口坏了」和「人不在」
2. 探针结果有缓存（探针自己也是一次搜索，不能每条都探）
3. search 停手 = 独立种类，不锁冷却、不发「扫码没用」的通知
4. 已知 sec_uid → 不走搜索
5. sec_uid 缓存写独立列，不冲掉 outreach_note（那是故障记录）
"""

from __future__ import annotations

import sqlite3

import pytest

from src.outreach.douyin_api import (
    Douyin,
    DouyinError,
    KIND_LIMIT,
    KIND_SEARCH,
    LeadNotFound,
    SearchUnavailable,
    normalize_kind,
)
from src.outreach.store import OutreachStore
from src.outreach.worker import OutreachWorker


# ============================================================
# 假抖音客户端
# ============================================================

class _FakePage:
    url = "https://www.douyin.com/"

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass

    def evaluate(self, *a, **k):
        return ""

    def wait_for_timeout(self, *a, **k):
        pass

    def locator(self, *a, **k):
        return _FakeLocator()

    @property
    def keyboard(self):
        return _FakeKeyboard()


class _FakeLocator:
    @property
    def first(self):
        return self

    def wait_for(self, *a, **k):
        pass

    def click(self, *a, **k):
        pass

    def fill(self, *a, **k):
        pass


class _FakeKeyboard:
    def press(self, *a, **k):
        pass


def _用户(unique_id: str, sec_uid: str = "") -> dict:
    return {
        "user_list": [{
            "user_info": {
                "unique_id": unique_id,
                "short_id": unique_id,
                "sec_uid": sec_uid or f"sec-{unique_id}",
                "nickname": f"昵称{unique_id}",
                "aweme_count": 5,
            }
        }]
    }


def _dy(search_impl, log=None) -> Douyin:
    """造一个不依赖浏览器的 Douyin（只测搜索判定逻辑）"""
    dy = Douyin(_FakePage(), log=log or (lambda m: None))
    dy.ensure_login = lambda: None
    dy.check_risk = lambda: None
    dy._body_text = lambda: ""

    def _search(q, expect_hit=True, want_ids=None, evidence=None):
        data = search_impl(q, expect_hit=expect_hit, want_ids=want_ids)
        if evidence is not None:
            # 假实现默认「抖音回话了，只是用户列表是空的」→ 真限流。
            # （「压根没搜起来」那种情况由 test_search_not_throttled.py 覆盖）
            evidence.update({"hits": 1, "user_hits": 1, "empty_bodies": 0,
                             "statuses": [200], "parsed": len(data),
                             "search_ran": True})
        return data

    dy._search = _search
    return dy


# ============================================================
# 1. 探针：分清「接口坏了」和「人不在」
# ============================================================

def test_探针也搜不到就判定接口被限流():
    """目标搜不到 + 探针也搜不到 → 接口坏了 → SearchUnavailable"""
    calls: list[str] = []

    def _search(q, expect_hit=True, want_ids=None):
        calls.append(q)
        return []

    dy = _dy(_search)
    with pytest.raises(SearchUnavailable):
        dy._resolve_once("987714317")
    assert Douyin.SEARCH_CANARY in calls, "判定前必须先探一次"


def test_探针能搜到就说明人是真的不在():
    """目标搜不到，但探针搜得到 → 接口是好的 → 不是故障，返回 None"""
    def _search(q, expect_hit=True, want_ids=None):
        if q == Douyin.SEARCH_CANARY:
            return [_用户("小米汽车")]
        return []

    dy = _dy(_search)
    # 不抛 SearchUnavailable，说明它当成「正常搜不到」
    assert dy._resolve_once("987714317") is None


def test_搜到目标就完全不探针():
    """正常情况不许多花一次搜索"""
    calls: list[str] = []

    def _search(q, expect_hit=True, want_ids=None):
        calls.append(q)
        return [_用户("987714317")]

    dy = _dy(_search)
    info = dy._resolve_once("987714317")
    assert info and info["sec_uid"] == "sec-987714317"
    assert calls == ["987714317"], f"不该多探：{calls}"


def test_探针结果有缓存不重复探():
    """探针自己也是一次搜索，每条都探等于搜索次数翻倍"""
    calls: list[str] = []

    def _search(q, expect_hit=True, want_ids=None):
        calls.append(q)
        return []

    dy = _dy(_search)
    for _ in range(4):
        with pytest.raises(SearchUnavailable):
            dy._resolve_once("u1")
    canary_calls = [c for c in calls if c == Douyin.SEARCH_CANARY]
    assert len(canary_calls) == 1, f"探针不该重复跑：{canary_calls}"


def test_接口正常时SearchUnavailable不会被误抛():
    """接口正常时，SearchUnavailable 一次都不该出现"""
    def _search(q, expect_hit=True, want_ids=None):
        return [_用户("x")] if q == Douyin.SEARCH_CANARY else []

    dy = _dy(_search)
    # 目标搜不到、接口正常 → 返回 None（不是异常）
    assert dy._resolve_once("不存在的号") is None


def test_SearchUnavailable不是LeadNotFound():
    """两者必须能区分开——这是整个改动的核心"""
    assert not isinstance(SearchUnavailable("x"), LeadNotFound)
    assert not isinstance(LeadNotFound("x"), SearchUnavailable)


# ============================================================
# 1b. 平台自己的裁决：search_nil_type = "verify_check"
# ============================================================

def test_读得出search_nil_info():
    """抖音在搜索响应里明说「为什么搜不到」"""
    data = [{"search_nil_info": {"search_nil_type": "verify_check"}}]
    assert Douyin._search_nil_type(data) == "verify_check"
    assert Douyin._search_nil_type([{"user_list": []}]) == ""
    assert Douyin._search_nil_type([]) == ""


def test_搜索要求验证时抛验证异常而不是搜不到():
    """根因修复（2026-11-12）

    抖音要求先过拼图验证才能搜人时，接口回的是
        user_list=[] + search_nil_info.search_nil_type="verify_check"
    以前没人读这个字段 → 程序当成「这些人全失效了」→ 一路空转。

    现在必须抛 RiskControl(kind=verify)，
    让「叫人过验证码 → 验证完自动接着跑」那条路生效。

    这个测试跑的是**真实的 `_search()`**（只把底层抓响应的 `_capture` 换掉），
    免得把要验的那段代码自己也 mock 掉了。
    """
    from src.outreach.douyin_api import KIND_VERIFY, RiskControl

    # ⚠️ 这里**不能**用 _dy()：它会把 _search 整个换掉，
    # 而要验的判定就在 _search 里 —— 等于把被测代码也 mock 掉了。
    dy = Douyin(_FakePage(), log=lambda m: None)
    dy.ensure_login = lambda: None
    dy.check_risk = lambda: None
    dy._body_text = lambda: ""
    dy._goto = lambda url: None
    dy._click_user_tab = lambda *a, **k: True
    dy._capture = lambda *a, **k: [
        {"user_list": [], "search_nil_info": {"search_nil_type": "verify_check"}}
    ]

    with pytest.raises(RiskControl) as ei:
        dy._resolve_once("987714317")
    assert normalize_kind(getattr(ei.value, "kind", "")) == KIND_VERIFY


def test_普通空结果不误报成验证():
    """没有 search_nil_info 的空结果，不能当成要验证"""
    def _search(q, expect_hit=True, want_ids=None):
        return [_用户("x")] if q == Douyin.SEARCH_CANARY else [{"user_list": []}]

    dy = _dy(_search)
    # 返回 None（真搜不到），不是抛验证异常
    assert dy._resolve_once("不存在的号") is None


# ============================================================
# 2. search 是一种独立停手种类（不锁冷却、不发错通知）
# ============================================================

def test_search是独立种类():
    assert normalize_kind("search") == KIND_SEARCH
    assert normalize_kind("搜索接口") == KIND_SEARCH
    assert KIND_SEARCH != KIND_LIMIT


def test_网页横幅认得search():
    """前后端词表必须一致（以前漂移过一次，网页横幅压根不显示）"""
    from src.web.outreach_page import STOP_KINDS

    assert KIND_SEARCH in STOP_KINDS
    assert set(STOP_KINDS) == {"verify", "limit", "login", "search"}


def test_搜索限流的停手文案不会被误判成账号限流():
    """搜索限流的文案里带了「限流」两个字——顺序反了就会误判成账号限流，
    然后锁 120 分钟冷却 + 发一条「扫码没用」的通知，方向完全错。"""
    from src.outreach.runner import OutreachRunner

    note = "搜索接口连续 3 次返回空（疑似被抖音限流），主动停手"
    assert OutreachRunner._kind_from_note(note) == KIND_SEARCH


def test_搜索限流不触发账号冷却():
    """账号是好的，凭什么锁人 120 分钟"""
    from src.outreach.runner import OutreachRunner

    r = OutreachRunner.__new__(OutreachRunner)
    r.config = _cfg_with_cooldown(120)
    r._risk_stop = None
    r._db_path = lambda: ":memory:"
    # 直接看 kind 的判定：search != limit → 不冷却
    assert normalize_kind(KIND_SEARCH) != KIND_LIMIT


def _cfg_with_cooldown(minutes: int):
    from src.config import get_config

    cfg = get_config()
    cfg.outreach.risk_cooldown_minutes = minutes
    return cfg


# ============================================================
# 3. sec_uid 缓存
# ============================================================

def _建库(tmp_path) -> str:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT NOT NULL DEFAULT '',
            display_id TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            car_model TEXT DEFAULT '',
            monitor_name TEXT DEFAULT '',
            store TEXT DEFAULT '',
            intent_level TEXT DEFAULT 'none',
            intent_score INTEGER DEFAULT 0,
            intent_reason TEXT DEFAULT '',
            evidence TEXT DEFAULT '[]',
            last_hit_at TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            outreach_status TEXT DEFAULT 'pending',
            outreach_at TEXT DEFAULT '',
            outreach_note TEXT DEFAULT '',
            source TEXT DEFAULT 'rule',
            bitable_record_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    conn.close()
    return str(path)


def test_set_sec_uid写独立列不冲掉故障记录(tmp_path):
    """outreach_note 存的是「失败原因」，冲掉等于把故障记录弄没了"""
    db = _建库(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO leads (display_id, nickname, outreach_note) VALUES (?, ?, ?)",
        ("u1", "昵称", "上次失败：网络断了"),
    )
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    st.set_sec_uid(1, "sec-abc")
    st.close()

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT sec_uid, outreach_note FROM leads WHERE id=1").fetchone()
    conn.close()
    assert row[0] == "sec-abc", row
    assert row[1] == "上次失败：网络断了", f"故障记录被冲掉了：{row}"


def test_cache_sec_uid_map批量缓存(tmp_path):
    """一次搜索返回一页用户，顺手把线索池里同号的都存上"""
    db = _建库(tmp_path)
    conn = sqlite3.connect(db)
    for did in ("u1", "u2", "u3"):
        conn.execute("INSERT INTO leads (display_id, nickname) VALUES (?, ?)", (did, did))
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    n = st.cache_sec_uid_map({"u1": "sec-1", "u2": "sec-2", "u999": "sec-9"})
    st.close()

    conn = sqlite3.connect(db)
    got = dict(conn.execute("SELECT display_id, sec_uid FROM leads").fetchall())
    conn.close()
    assert n == 2, n
    assert got["u1"] == "sec-1"
    assert got["u2"] == "sec-2"
    assert got["u3"] == "", "没匹配上的不能乱写"


def test_pending_leads带出sec_uid(tmp_path):
    db = _建库(tmp_path)
    # 老库没有 sec_uid 列——触达线程自己补（不能干等主进程迁移）
    st = OutreachStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO leads (display_id, nickname, outreach_status, sec_uid) "
        "VALUES ('u1', 'n1', 'pending', 'sec-1')"
    )
    conn.commit()
    conn.close()

    rows = st.pending_leads()
    st.close()
    assert rows and rows[0]["sec_uid"] == "sec-1", rows


# ============================================================
# 4. worker：已知 sec_uid 就跳过搜索
# ============================================================

class _FakeDy:
    """记录到底走了哪条路"""

    def __init__(self):
        self.resolve_calls: list[str] = []
        self.by_sec_calls: list[str] = []
        self.last_search_users: list[dict] = []

    def check_risk(self):
        pass

    def cookies_ok(self):
        return True

    def clear_verify(self):
        pass

    def resolve(self, unique_id, nickname=None):
        self.resolve_calls.append(unique_id)
        return {"sec_uid": "sec-new", "nickname": "昵称", "aweme_count": 3}

    def resolve_by_sec_uid(self, sec_uid):
        self.by_sec_calls.append(sec_uid)
        return {"sec_uid": sec_uid, "nickname": "昵称", "aweme_count": 3}


def _worker(tmp_path, **cfg_over):
    from src.config import get_config

    cfg = get_config()
    cfg.outreach.dry_run = True
    cfg.outreach.max_per_run = 20
    cfg.outreach.daily_limit = 999
    cfg.outreach.fail_stop_threshold = 3
    cfg.outreach.not_found_stop_threshold = 99
    cfg.outreach.min_delay = 0
    cfg.outreach.max_delay = 0
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    return OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None)


def _跑一轮(w, leads, monkeypatch, dy=None):
    import src.outreach.worker as worker_mod

    dy = dy or _FakeDy()

    class _FakeDouyin:
        def __init__(self, *a, **k):
            pass

        def cookies_ok(self):
            return True

        def check_risk(self):
            pass

        def clear_verify(self):
            pass

        def resolve(self, unique_id, nickname=None):
            return dy.resolve(unique_id, nickname)

        def resolve_by_sec_uid(self, sec_uid):
            return dy.resolve_by_sec_uid(sec_uid)

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
    result = w.run(limit=len(leads), dry_run=True)
    return result, dy


def _线索(n: int, sec_uid: str = "") -> list[dict]:
    return [
        {"id": i, "lead_key": f"k{i}", "display_id": f"u{i}",
         "nickname": f"n{i}", "car_model": "", "intent_level": "medium",
         "intent_score": 30, "intent_reason": "", "evidence": "[]",
         "monitor_name": "", "bitable_record_id": "", "outreach_status": "pending",
         "sec_uid": sec_uid}
        for i in range(1, n + 1)
    ]


def test_已知sec_uid就跳过搜索(tmp_path, monkeypatch):
    """搜索次数本身就是被限流的资源，能省就省"""
    w = _worker(tmp_path)
    leads = _线索(1, sec_uid="sec-known")
    _result, dy = _跑一轮(w, leads, monkeypatch)
    assert dy.by_sec_calls == ["sec-known"], dy.by_sec_calls
    assert dy.resolve_calls == [], f"不该再搜索：{dy.resolve_calls}"


def test_没有sec_uid才搜索(tmp_path, monkeypatch):
    w = _worker(tmp_path)
    leads = _线索(1)
    _result, dy = _跑一轮(w, leads, monkeypatch)
    assert dy.resolve_calls == ["u1"], dy.resolve_calls
    assert dy.by_sec_calls == []


def test_搜索被限流按真异常停手(tmp_path, monkeypatch):
    """接口坏了必须停手——别再拿搜索次数去撞抖音的风控"""
    w = _worker(tmp_path, fail_stop_threshold=3)
    leads = _线索(10)
    dy = _FakeDy()

    def _boom(unique_id, nickname=None):
        raise SearchUnavailable("搜索接口返回空（疑似被抖音限流）")

    dy.resolve = _boom
    result, _ = _跑一轮(w, leads, monkeypatch, dy=dy)
    assert result.get("stopped"), "接口坏了该停手"
    assert result["failed"] >= 3
    # 停手种类必须是 search，不能是 limit（limit 会锁 120 分钟冷却）
    assert w._stop_kind == KIND_SEARCH, w._stop_kind


def test_搜索被限流不计入not_found(tmp_path, monkeypatch):
    """接口坏了不是「这些人都失效了」，不能走 not_found 那条路"""
    w = _worker(tmp_path, fail_stop_threshold=99, not_found_stop_threshold=2)
    leads = _线索(3)
    dy = _FakeDy()

    def _boom(unique_id, nickname=None):
        raise SearchUnavailable("搜索接口返回空")

    dy.resolve = _boom
    result, _ = _跑一轮(w, leads, monkeypatch, dy=dy)
    assert result.get("not_found", 0) == 0, result
    assert result["failed"] == 3, result
    assert not result.get("stopped"), "阈值 99 不该停"
