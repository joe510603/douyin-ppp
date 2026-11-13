"""人工验证通过之后，触达必须**真的接着跑**（2026-11-13 用户反馈）

用户反馈了两件事
----------------
1. 「图片验证出来后，手动过了，但是没反应了」
2. 「触发了验证的抖音号，好像就直接跳过了？」

查日志查出来的真相
------------------
- 程序**其实恢复了**：09:54:32 日志「🔎 检测到验证码已经过了，自动接着跑」，
  接着「↩ 继续处理队列」。但恢复后**立刻撞上搜索接口限流**，
  一条也没发出去 → 用户肉眼看过去就是「什么都没发生」。
- 触发验证的那条线索被标回 pending 后就**跳到下一条**了。
  可验证码就是在**搜这个人**的时候弹的——人已经找到门口了，
  验证一过就该马上去敲他的门，跳到下一条等于这个人白排一次队。

所以这不单是「没反应」的问题，还藏着一个行为错误：
**验证过了却不回头重试触发验证的那条**。

这两个行为必须写成测试钉住，否则改一次就退一次。
"""
from __future__ import annotations

from src.outreach.douyin_api import (
    RiskControl,
    SearchUnavailable,
)
from src.outreach.worker import OutreachWorker


# ============================================================
# 假抖音客户端：按剧本在「搜索阶段」撞验证 / 撞限流
# ============================================================

class _FakeDy:
    """记录走了哪条路

    verify_until    前 N 次搜索抛「要验证」
    unavailable_until 前 N 次搜索抛「搜索接口被限流」
    """

    def __init__(self, verify_until: int = 0, unavailable_until: int = 0,
                 raise_on_search: Exception | None = None):
        self.verify_until = verify_until
        self.unavailable_until = unavailable_until
        self.raise_on_search = raise_on_search
        self.resolve_calls: list[str] = []
        self.cleared = 0
        self.last_search_users: list[dict] = []

    def check_risk(self):
        pass

    def cookies_ok(self):
        return True

    def clear_verify(self):
        self.cleared += 1

    def resolve(self, unique_id, nickname=None):
        self.resolve_calls.append(unique_id)
        if self.raise_on_search is not None:
            raise self.raise_on_search
        if len(self.resolve_calls) <= self.verify_until:
            raise RiskControl(
                "抖音要求先完成安全验证才能搜索（搜索结果为空是因为触发了验证）",
                kind="verify",
            )
        if len(self.resolve_calls) <= self.unavailable_until:
            raise SearchUnavailable("搜索接口返回空（疑似被抖音限流）")
        return {"sec_uid": "sec-new", "nickname": "昵称", "aweme_count": 3}

    def resolve_by_sec_uid(self, sec_uid):
        return {"sec_uid": sec_uid, "nickname": "昵称", "aweme_count": 3}

    def fetch_works(self, sec_uid, limit=20):
        return [{"aweme_id": "aw1", "desc": "作品"}]


def _线索(n: int) -> list[dict]:
    return [
        {"id": i, "lead_key": f"k{i}", "display_id": f"u{i}",
         "nickname": f"n{i}", "car_model": "", "intent_level": "medium",
         "intent_score": 30, "intent_reason": "", "evidence": "[]",
         "monitor_name": "", "bitable_record_id": "", "outreach_status": "pending",
         "sec_uid": ""}
        for i in range(1, n + 1)
    ]


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
    # 插队在测试里关掉：名单顺序要确定，才能断言「先重试哪条」
    cfg.outreach.auto_continuous = False
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
            dy.clear_verify()

        def resolve(self, unique_id, nickname=None):
            return dy.resolve(unique_id, nickname)

        def resolve_by_sec_uid(self, sec_uid):
            return dy.resolve_by_sec_uid(sec_uid)

        def fetch_works(self, sec_uid, limit=20):
            return dy.fetch_works(sec_uid, limit)

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


def _接上人工(w):
    """把「等人验证」这头接好：假装用户已经手动过了

    同时记录：
    · status_calls —— 每条线索被写成了什么状态
    · resumed     —— 恢复时有没有主动通知用户
    """
    status_calls: list[tuple[str, str]] = []
    resumed: list[dict] = []

    orig_set = w._set_status

    def _spy_set_status(lead, status, error=""):
        status_calls.append((lead["display_id"], status))
        return orig_set(lead, status, error)

    w._set_status = _spy_set_status
    w._bring_window_back = lambda: True
    w._notify_risk = lambda *a, **k: True
    w._wait_for_human = lambda reason: True
    w._notify_resumed = lambda lead=None: (resumed.append(lead or {}), True)[1]
    # 话术这一步不参与本文件的断言，固定成成功，省得受 LLM 影响
    w.cw.generate = lambda lead: {"ok": True, "text": "你好", "template_id": "t1"}
    return status_calls, resumed


# ============================================================
# 1. 验证通过后要**回头重试触发验证的那条**
# ============================================================

def test_验证通过后重试同一条线索(tmp_path, monkeypatch):
    """验证码是在搜 u1 时弹的 → 过了就该马上回头找 u1，而不是跳到 u2"""
    w = _worker(tmp_path)
    _status, _resumed = _接上人工(w)
    dy = _FakeDy(verify_until=1)

    _result, dy = _跑一轮(w, _线索(2), monkeypatch, dy=dy)

    assert dy.resolve_calls[:2] == ["u1", "u1"], (
        f"验证通过后必须回头重试 u1，实际调用顺序：{dy.resolve_calls}"
    )
    assert dy.resolve_calls.index("u1", 1) == 1, "重试必须紧跟在第一次之后"


def test_重试成功后才轮到下一条(tmp_path, monkeypatch):
    """重试是「插在原位」，不是把整份名单重跑一遍"""
    w = _worker(tmp_path)
    _接上人工(w)
    dy = _FakeDy(verify_until=1)

    _result, dy = _跑一轮(w, _线索(2), monkeypatch, dy=dy)

    assert dy.resolve_calls == ["u1", "u1", "u2"], dy.resolve_calls


# ============================================================
# 2. 重试要有上限，防止「验证→重试→又验证」变成死循环
# ============================================================

def test_一直撞验证不会无限重试(tmp_path, monkeypatch):
    w = _worker(tmp_path, verify_retry_max=2)
    _status, _resumed = _接上人工(w)
    dy = _FakeDy(verify_until=99)

    _result, dy = _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    # 首次 + 2 次重试 = 3 次，到顶就不再重来
    assert dy.resolve_calls == ["u1", "u1", "u1"], dy.resolve_calls


def test_重试到顶后线索留在队列里(tmp_path, monkeypatch):
    """不能因为反复撞验证就把线索标成失败（那等于永久踢出队列）"""
    w = _worker(tmp_path, verify_retry_max=1)
    status_calls, _resumed = _接上人工(w)
    dy = _FakeDy(verify_until=99)

    _result, dy = _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert ("u1", "failed") not in status_calls, status_calls
    assert status_calls[-1] == ("u1", "pending"), status_calls


# ============================================================
# 3. 恢复之后要做两件事：清验证状态 + 主动告诉用户
# ============================================================

def test_恢复后清掉验证状态(tmp_path, monkeypatch):
    """不清的话，下一条线索刚开跑就被自己的旧状态拦死
    （2026-11-11 在发送阶段踩过同一个坑，搜索阶段漏了）"""
    w = _worker(tmp_path)
    _接上人工(w)
    dy = _FakeDy(verify_until=1)

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert dy.cleared >= 1, "人工验证通过后必须调 clear_verify"


def test_恢复后主动通知用户(tmp_path, monkeypatch):
    """用户手动过完验证，回头一看像「没反应」——
    恢复必须说一声，不然人以为程序卡死了"""
    w = _worker(tmp_path)
    _status, resumed = _接上人工(w)
    dy = _FakeDy(verify_until=1)

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert resumed, "验证通过后必须通知用户「已经恢复，接着发」"
    assert resumed[0].get("display_id") == "u1", resumed


# ============================================================
# 4. 搜索被限流 ≠ 这个线索失效，不能把它永久踢出队列
# ============================================================

def test_搜索被限流线索保持待触达(tmp_path, monkeypatch):
    """待触达队列只挑 pending——标成 failed 就是永久出局，
    每撞一次限流就悄悄吃掉一批线索（静默的数据损失）"""
    w = _worker(tmp_path, fail_stop_threshold=99)
    status_calls, _resumed = _接上人工(w)
    dy = _FakeDy(unavailable_until=1)

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert ("u1", "failed") not in status_calls, (
        f"搜索被限流是平台侧故障，不能标 failed：{status_calls}"
    )
    assert status_calls[-1] == ("u1", "pending"), status_calls


def test_搜索被限流的线索下一轮还能被选中(tmp_path, monkeypatch):
    """真正要保住的是「下一轮还在队列里」——直接查 DB"""
    import sqlite3

    from src.outreach.store import OutreachStore

    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, "
        "lead_key TEXT, display_id TEXT, nickname TEXT, car_model TEXT, "
        "store TEXT, intent_level TEXT, intent_score INTEGER, "
        "intent_reason TEXT, evidence TEXT, monitor_name TEXT, "
        "bitable_record_id TEXT, outreach_status TEXT, outreach_note TEXT, "
        "outreach_at TEXT, created_at TEXT, last_hit_at TEXT, sec_uid TEXT)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'u1', 'n1', 'pending')"
    )
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    st.set_outreach_status(1, "pending", "搜索接口被限流，稍后重试")
    rows = st.pending_leads()
    st.close()

    assert any(r["display_id"] == "u1" for r in rows), (
        "被限流打断的线索必须还能被下一轮选中"
    )


# ============================================================
# 5. 偶发的故障（超时/断连）也不该把线索判死刑
# ============================================================

def test_页面超时线索保持待触达(tmp_path, monkeypatch):
    """一次网络抖动就把线索踢出队列，等于慢性自杀"""
    w = _worker(tmp_path, fail_stop_threshold=99)
    status_calls, _resumed = _接上人工(w)
    dy = _FakeDy(raise_on_search=TimeoutError("Page.goto: Timeout 45000ms exceeded."))

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert ("u1", "failed") not in status_calls, status_calls
    assert status_calls[-1] == ("u1", "pending"), status_calls


def test_断连也算偶发(tmp_path, monkeypatch):
    w = _worker(tmp_path, fail_stop_threshold=99)
    status_calls, _resumed = _接上人工(w)
    dy = _FakeDy(raise_on_search=RuntimeError(
        "打不开作品页：Page.goto: net::ERR_CONNECTION_CLOSED"))

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert ("u1", "failed") not in status_calls, status_calls


def test_真失败仍然标失败(tmp_path, monkeypatch):
    """不是所有错误都值得重试——真失败还是得标出来"""
    w = _worker(tmp_path, fail_stop_threshold=99)
    status_calls, _resumed = _接上人工(w)
    dy = _FakeDy(raise_on_search=ValueError("这个号根本不是个人"))

    _跑一轮(w, _线索(1), monkeypatch, dy=dy)

    assert ("u1", "failed") in status_calls, status_calls


# ============================================================
# 6. 轮次异常结束时，手上那条线索不能卡在「搜索中」
# ============================================================

def test_中断时手上的线索退回待触达(tmp_path, monkeypatch):
    """「搜索中 / 已定位」不是终态，待触达队列只挑 pending——
    卡在这里的线索就永远没人管了（实测卡了 3 条出不来）"""
    import sqlite3

    from src.outreach.store import OutreachStore

    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, "
        "lead_key TEXT, display_id TEXT, nickname TEXT, car_model TEXT, "
        "store TEXT, intent_level TEXT, intent_score INTEGER, "
        "intent_reason TEXT, evidence TEXT, monitor_name TEXT, "
        "bitable_record_id TEXT, outreach_status TEXT, outreach_note TEXT, "
        "outreach_at TEXT, created_at TEXT, last_hit_at TEXT, sec_uid TEXT)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'u1', 'n1', 'searching')"
    )
    conn.commit()
    conn.close()

    w = _worker(tmp_path, dry_run=False)
    _接上人工(w)
    # _dry 是在 run() 里按参数设的，这里没跑 run()，得自己设对
    w._dry = False
    w._current_lead = {"id": 1, "display_id": "u1"}

    w._release_current_lead()

    st = OutreachStore(db)
    row = st.get_lead(1)
    st.close()
    assert row["outreach_status"] == "pending", row


def test_已经留言的不受影响(tmp_path, monkeypatch):
    """正常跑完的轮次，那条线索是终态，不能被误改回 pending"""
    import sqlite3

    from src.outreach.store import OutreachStore

    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, "
        "lead_key TEXT, display_id TEXT, nickname TEXT, car_model TEXT, "
        "store TEXT, intent_level TEXT, intent_score INTEGER, "
        "intent_reason TEXT, evidence TEXT, monitor_name TEXT, "
        "bitable_record_id TEXT, outreach_status TEXT, outreach_note TEXT, "
        "outreach_at TEXT, created_at TEXT, last_hit_at TEXT, sec_uid TEXT)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'u1', 'n1', 'commented')"
    )
    conn.commit()
    conn.close()

    w = _worker(tmp_path)
    _接上人工(w)
    w._current_lead = {"id": 1, "display_id": "u1"}

    w._release_current_lead()

    st = OutreachStore(db)
    row = st.get_lead(1)
    st.close()
    assert row["outreach_status"] == "commented", row
