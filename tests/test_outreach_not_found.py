"""搜不到人 ≠ 异常，不该把整轮掐断

用户反馈（2026-11-12）：
「连续没搜到人，这个不应该作为连续 3 次异常，主动停手（防止把号跑废）吧？
 不要随便停下，效率太低了」

要盯住的：
1. 连续 N 条「搜不到人」**不触发**「连续 N 次异常」停手
2. 「搜不到人」记成 skipped（不是 failed），且能单独计数
3. 但如果**连续**很多条全都搜不到 → 该停（那多半是搜索坏了，在空转）
4. 真异常（网络断了之类的）照样按 fail_stop_threshold 停手
"""

from __future__ import annotations

import pytest

from src.outreach.douyin_api import DouyinError, LeadNotFound, NoWorks
from src.outreach.worker import OutreachWorker


class _FakeDy:
    """假的抖音客户端：按脚本抛异常或正常返回"""

    def __init__(self, script: list):
        self.script = list(script)
        self.i = 0

    def check_risk(self):
        pass

    def resolve(self, unique_id, nickname=None):
        item = self.script[self.i] if self.i < len(self.script) else self.script[-1]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item


def _worker(tmp_path, **cfg_over):
    from src.config import get_config

    cfg = get_config()
    cfg.outreach.dry_run = True
    cfg.outreach.max_per_run = 20
    cfg.outreach.daily_limit = 999
    cfg.outreach.fail_stop_threshold = 3
    cfg.outreach.not_found_stop_threshold = 5
    cfg.outreach.min_delay = 0
    cfg.outreach.max_delay = 0
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None)
    return w


def _线索(n: int) -> list[dict]:
    return [
        {"id": i, "lead_key": f"k{i}", "display_id": f"u{i}",
         "nickname": f"n{i}", "car_model": "", "intent_level": "medium",
         "intent_score": 30, "intent_reason": "", "evidence": "[]",
         "monitor_name": "", "bitable_record_id": "", "outreach_status": "pending"}
        for i in range(1, n + 1)
    ]


def _跑一轮(w, leads, script, monkeypatch):
    """喂一堆线索 + 一串异常剧本，跑 worker.run

    直接把 worker 模块里的 Douyin 类换掉——浏览器整套不参与，
    测的只是「异常怎么分类、怎么计数、什么时候停手」。
    """
    import src.outreach.worker as worker_mod

    dy = _FakeDy(script)

    class _FakeDouyin:
        def __init__(self, *a, **k):
            pass

        def cookies_ok(self):
            return True

        def check_risk(self):
            pass

        def resolve(self, unique_id, nickname=None):
            return dy.resolve(unique_id, nickname)

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
    return w.run(limit=len(leads), dry_run=True)


# ============================================================
# 搜不到人
# ============================================================

def test_搜不到人抛的是专用异常():
    """不是普通 DouyinError —— 那样会被当成「异常」"""
    assert issubclass(LeadNotFound, DouyinError)
    assert not isinstance(LeadNotFound("x"), NoWorks)


def test_连续搜不到人不触发异常停手(tmp_path, monkeypatch):
    """核心：连续 8 条都搜不到人，也不该按「异常」停手

    fail_stop_threshold=3 —— 如果搜不到人还算异常，第 3 条就停了。
    not_found_stop_threshold 设很大 —— 只测「不计入异常」这一件事。
    """
    w = _worker(tmp_path, fail_stop_threshold=3, not_found_stop_threshold=99)
    leads = _线索(8)
    script = [LeadNotFound("没搜到这个人（抖音号可能已改名/注销/被平台隐藏）")] * 8

    result = _跑一轮(w, leads, script, monkeypatch)

    assert not result.get("stopped"), f"不该停手：{result.get('stopped')}"
    assert result["skipped"] == 8, result
    assert result.get("not_found") == 8, result


def test_搜不到人记成skipped不是failed(tmp_path, monkeypatch):
    w = _worker(tmp_path)
    leads = _线索(2)
    script = [LeadNotFound("没搜到这个人")] * 2
    result = _跑一轮(w, leads, script, monkeypatch)
    assert result["failed"] == 0, "搜不到人不是失败"
    assert result["skipped"] == 2


def test_连续全搜不到也不停手(tmp_path, monkeypatch):
    """搜不到就该跳过继续，别掐断整轮

    用户 2026-11-13 纠正：「是不是转牛角尖了，一直在搜索搜不到的人，
    然后一直触发风控？……搜不到应该就是真的搜不到了，继续去触达呀」

    老逻辑是「连续 N 条都搜不到 → 判定搜索坏了 → 停手 + 切慢档」。
    实测 2026-11-13 下午：连着两轮「发 0　跳过 5」，13 分钟一条没发出去，
    而同一批里真正能触达的人排在第 30 位，一轮都轮不到。

    现在「搜索坏了」有专门的信号（限流 → SearchUnavailable、
    没跑起来 → SearchNotReady），不需要靠连续数去猜。
    """
    w = _worker(tmp_path, not_found_stop_threshold=4)
    leads = _线索(10)
    script = [LeadNotFound("没搜到这个人")] * 10
    result = _跑一轮(w, leads, script, monkeypatch)
    assert not result.get("stopped"), f"搜不到不该停手：{result.get('stopped')}"
    assert result.get("not_found") == 10, result


def test_搜到一个之后计数清零(tmp_path, monkeypatch):
    """中间搜到一个 → 连续计数清零 → 后面再连续 2 条也不算数

    阈值 3。如果中间那个成功没清零，
    前面 2 条 + 后面 1 条 = 连续 3 条 → 会停。
    """
    w = _worker(tmp_path, not_found_stop_threshold=3)
    leads = _线索(5)
    script = (
        [LeadNotFound("没搜到")] * 2
        + [{"sec_uid": "s1", "nickname": "找到了", "aweme_count": 3}]
        + [LeadNotFound("没搜到")] * 2
    )
    result = _跑一轮(w, leads, script, monkeypatch)
    assert not result.get("stopped"), result.get("stopped")


# ============================================================
# 真异常照样停
# ============================================================

def test_真异常照样按阈值停手(tmp_path, monkeypatch):
    """网络断了这种真故障还是要停——别把号跑废"""
    w = _worker(tmp_path, fail_stop_threshold=3)
    leads = _线索(10)
    script = [DouyinError("网络断了")] * 10
    result = _跑一轮(w, leads, script, monkeypatch)
    assert result.get("stopped"), "真异常必须停手"
    assert "异常" in result["stopped"]


def test_搜不到人不影响真异常的计数(tmp_path, monkeypatch):
    """交替出现时，真异常累计到 3 次照样停"""
    w = _worker(tmp_path, fail_stop_threshold=3, not_found_stop_threshold=99)
    leads = _线索(6)
    script = [
        LeadNotFound("没搜到"), DouyinError("炸了"),
        LeadNotFound("没搜到"), DouyinError("炸了"),
        LeadNotFound("没搜到"), DouyinError("炸了"),
    ]
    result = _跑一轮(w, leads, script, monkeypatch)
    assert result.get("stopped"), "真异常累计 3 次就该停"
