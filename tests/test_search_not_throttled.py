"""「搜索没跑起来」≠「被限流」（2026-11-13 用户现场日志逼出来的修复）

现场日志
--------
    12:36:58  🔎 探测：搜「小米」也一个用户都没有 → 搜索接口被限流了
    12:38:31  搜索：999999108
    12:38:48  ✓ 定位到 测试昵称F（作品 0 条）      ← 同样的搜索，16.7 秒就成功了

抖音的限流窗口活不过 90 秒，却被当成了铁证 →
  · 白等 90~120 秒慢档
  · 线索退回 pending
  · 一轮下来少触达一批人

而且用户亲眼看到：「日志写着『搜索：测试昵称A』，浏览器上却根本没有搜索动作」。

所以「搜不到用户」必须**按证据**分三种：
  · 抓到了用户列表响应、里面是空的   → 真限流（throttled）
  · 压根没抓到用户列表响应           → 搜索动作没跑起来（not_run）→ **不是限流**
  · 抓到用户列表、里面有目标         → 搜到了

这些行为必须写成测试钉住，否则改一次就退一次。
"""
from __future__ import annotations

import time

from src.outreach.douyin_api import Douyin, SearchNotReady


def _douyin() -> Douyin:
    return Douyin(page=None, log=lambda m: None)


# ============================================================
# 三态：ok / throttled / not_run
# ============================================================

def test_默认状态是正常():
    d = _douyin()
    assert d._search_health_state == "ok"
    assert d._search_known_throttled() is False


def test_真限流才短路():
    d = _douyin()
    d._search_health_state = "throttled"
    d._search_health_at = time.time()
    assert d._search_known_throttled() is True


def test_搜索没跑起来不短路():
    """not_run 不能当成限流去短路 —— 实测 90 秒后同样的搜索就成功了"""
    d = _douyin()
    d._search_health_state = "not_run"
    d._search_health_at = time.time()
    assert d._search_known_throttled() is False


def test_限流结论过期之后不短路():
    d = _douyin()
    d._search_health_state = "throttled"
    d._search_health_at = time.time() - (d.SEARCH_UNHEALTHY_TTL + 5)
    assert d._search_known_throttled() is False


def test_正常结论留长有效期():
    d = _douyin()
    d._search_health_state = "ok"
    d._search_health_at = time.time()
    assert d._health_ttl() == d.SEARCH_HEALTH_TTL


def test_非正常结论用短有效期():
    """not_run / throttled 都要尽快重探 —— 抖音的限流是概率性的"""
    d = _douyin()
    d._search_health_state = "not_run"
    d._search_health_at = time.time()
    assert d._health_ttl() == d.SEARCH_UNHEALTHY_TTL


# ============================================================
# 探针按证据下结论
# ============================================================

class _FakeDouyin(Douyin):
    """把 ``_search`` 换掉，只测「探针拿到证据之后怎么下结论」"""

    def __init__(self, users, evidence):
        super().__init__(page=None, log=lambda m: None)
        self._users = users
        self._ev = evidence
        self._search_calls = 0

    def _search(self, q, expect_hit=True, want_ids=None, evidence=None):
        self._search_calls += 1
        if evidence is not None:
            evidence.update(self._ev)
        return [{"user_list": self._users}]


def test_探针搜到人就是正常():
    d = _FakeDouyin([{"sec_uid": "s1"}], {"user_hits": 1, "hits": 1})
    assert d._probe_search_health(force=True) is True
    assert d._search_health_state == "ok"


def test_探针拿到空用户列表是真限流():
    """抖音回话了、用户列表是空的 → 这才是限流"""
    d = _FakeDouyin([], {"user_hits": 1, "hits": 1})
    assert d._probe_search_health(force=True) is False
    assert d._search_health_state == "throttled"
    assert d._search_known_throttled() is True


def test_探针压根没搜起来不是限流():
    """一个用户列表响应都没抓到 → 是我们自己没做成，不是抖音限流"""
    d = _FakeDouyin([], {"user_hits": 0, "hits": 0})
    assert d._probe_search_health(force=True) is False
    assert d._search_health_state == "not_run"
    # 最关键：不能因此短路（否则后面每条线索都不搜了）
    assert d._search_known_throttled() is False


def test_探针没搜起来时不会把后续搜索全短路掉():
    d = _FakeDouyin([], {"user_hits": 0, "hits": 0})
    d._probe_search_health(force=True)
    d._search_health_at = time.time()
    assert d._search_known_throttled() is False


# ============================================================
# 「搜索动作没跑起来」是独立异常
# ============================================================

def test_有独立的异常类型():
    """worker 要能把它跟「真限流」分开处理（不切慢档）"""
    from src.outreach.douyin_api import DouyinError, SearchUnavailable

    assert issubclass(SearchNotReady, DouyinError)
    # 两者不是同一个类 —— worker 里是两套处理逻辑
    assert not issubclass(SearchNotReady, SearchUnavailable)
    assert not issubclass(SearchUnavailable, SearchNotReady)


def test_搜索代码里必须有证据收集():
    """防止以后有人把「重试 + 证据」这两样删掉"""
    import inspect
    from src.outreach import douyin_api

    src = inspect.getsource(douyin_api.Douyin._search)
    assert "user_hits" in src, "搜索必须记录用户列表响应数（区分限流和没搜成）"
    assert "search_ran" in src, "搜索必须记录动作有没有真的执行"
    assert "abort_if" in src, "动作没跑起来时不能白等满超时"
    assert "重做一次搜索" in src, "没拿到用户列表要重试一次"


def test_搜索框必须挑可见的那个():
    """搜索结果页上有两个匹配的 input，一个可见一个隐藏（0×0）

    用 ``.first`` 有可能挑中隐藏的那个 → 白等满超时 → 这次搜索直接废掉。
    （2026-11-13 生产日志实测到过：「这页的搜索框用不了：Timeout 8000ms」）
    """
    import inspect
    from src.outreach import douyin_api

    src = inspect.getsource(douyin_api.Douyin._type_in_search_box)
    assert ":visible" in src, "必须只挑可见的搜索框"
    assert ".first" in src, "可见的也要挑第一个"


# ============================================================
# worker 层：搜索没跑起来 ≠ 限流
# ============================================================

def _worker(tmp_path, **cfg_over):
    from src.config import get_config
    from src.outreach.worker import OutreachWorker

    # 建一个最小可用的 leads 表（触达线程只写 outreach_status 那一列）
    import sqlite3
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS leads ("
        " id INTEGER PRIMARY KEY, display_id TEXT, nickname TEXT, "
        " outreach_status TEXT DEFAULT 'pending', outreach_note TEXT DEFAULT '', "
        " outreach_at TEXT DEFAULT '', contact_type TEXT DEFAULT '', "
        " contact_value TEXT DEFAULT '', sec_uid TEXT DEFAULT '')"
    )
    conn.commit()
    conn.close()

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
    return OutreachWorker(cfg, str(db_path), log=lambda m: None)


def _线索(n: int) -> list[dict]:
    return [
        {"id": i, "lead_key": f"k{i}", "display_id": f"u{i}",
         "nickname": f"n{i}", "car_model": "", "intent_level": "medium",
         "intent_score": 30, "intent_reason": "", "evidence": "[]",
         "monitor_name": "", "bitable_record_id": "", "outreach_status": "pending",
         "sec_uid": ""}
        for i in range(1, n + 1)
    ]


def _跑一轮(w, leads, monkeypatch, resolve_impl):
    import src.outreach.worker as worker_mod

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
            return resolve_impl(unique_id, nickname)

        def resolve_by_sec_uid(self, sec_uid):
            return resolve_impl(sec_uid, None)

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
    return w.run(limit=len(leads), dry_run=True)


def test_搜索没跑起来不切慢档(tmp_path, monkeypatch):
    """退避对「我们自己没做成」一点用没有 —— 只能白白拖慢一轮触达"""
    w = _worker(tmp_path, fail_stop_threshold=99)

    def _boom(unique_id, nickname=None):
        raise SearchNotReady("搜索动作没跑起来（页面没发起搜索请求）")

    leads = _线索(3)
    result = _跑一轮(w, leads, monkeypatch, _boom)
    # 慢档表里不该有记录
    row = w.st._conn.execute(
        "SELECT slow_until FROM outreach_speed WHERE id = 1").fetchone()
    assert (not row) or not row["slow_until"], "搜索没跑起来不该切慢档"
    # 线索退回 pending，不能判死（判死 = 永远不再触达）
    for lead_id in (1, 2, 3):
        got = w.st.get_lead(lead_id)
        if got:
            assert got["outreach_status"] == "pending", got["outreach_status"]
    assert not result.get("stopped"), "不该停手"


def test_搜索没跑起来连续多次才停手(tmp_path, monkeypatch):
    """连续多次都做不出搜索 → 说明浏览器真有问题，该停下来等人看一眼"""
    w = _worker(tmp_path, fail_stop_threshold=3)

    def _boom(unique_id, nickname=None):
        raise SearchNotReady("搜索动作没跑起来")

    leads = _线索(10)
    result = _跑一轮(w, leads, monkeypatch, _boom)
    assert result.get("stopped"), "连续做不出搜索该停手"
