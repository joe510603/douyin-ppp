"""触达提速：自适应间隔 + 飞书状态异步回写

背景（2026-11-12 实测）
------------------------
一轮 10 条实测每条 137 秒 = 页面操作 57~105 秒 + 条间等待 30~62 秒。
要让整轮时间砍掉一半，光优化页面操作不够，间隔必须一起动；
但间隔又是**风控旋钮**（2026-11-11 有过 3~8 秒间隔导致 10 条全部搜不到的教训）。

所以拆成两档：
  · 快档（平时）20~30 秒
  · 慢档（撞过验证码/软风控后）90~120 秒，而且**记住**一段时间

另外：飞书状态回写走 lark-cli 子进程，实测一次 1.4 秒，每条线索要写 3 次
→ 每条白等 4.2 秒。改成后台线程排队写，不能再阻塞触达。
"""
from __future__ import annotations

import sqlite3
import time

from src.config import get_config
from src.outreach.worker import OutreachWorker, _FeishuStatusPusher


LEADS_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    outreach_status TEXT DEFAULT 'pending',
    outreach_note TEXT DEFAULT '',
    outreach_at TEXT DEFAULT '',
    intent_level TEXT DEFAULT 'none',
    intent_score INTEGER DEFAULT 0,
    evidence TEXT DEFAULT '[]',
    car_model TEXT DEFAULT '',
    store TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    bitable_record_id TEXT DEFAULT ''
)
"""


def _make_db(tmp_path):
    db = tmp_path / "delay_test.db"
    conn = sqlite3.connect(db)
    conn.execute(LEADS_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'dy001', '小明', 'pending')"
    )
    conn.commit()
    conn.close()
    return str(db)


def _worker(tmp_path, **cfg_over):
    cfg = get_config()
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    w = OutreachWorker(cfg, _make_db(tmp_path), log=lambda m: None)
    return w


# ============================================================
# 自适应间隔
# ============================================================

def test_默认走快档(tmp_path):
    """没撞过风控就用快档（20~30 秒）"""
    w = _worker(tmp_path)
    lo, hi, why = w._pick_delay()
    assert (lo, hi) == (20, 30), f"默认应该是快档 20-30，实际 {lo}-{hi}"
    assert "快档" in why


def test_撞过风控后走慢档(tmp_path):
    """撞过验证码/软风控 → 自动退到慢档（90~120 秒）"""
    w = _worker(tmp_path)
    w.st.set_slow_delay(60, "测试")
    lo, hi, why = w._pick_delay()
    assert (lo, hi) == (90, 120), f"应该退到慢档 90-120，实际 {lo}-{hi}"
    assert "慢档" in why


def test_慢档会跨轮次记住(tmp_path):
    """慢档状态存在库里，不是内存里的临时变量 —— 服务重启也记得"""
    w = _worker(tmp_path)
    w.st.set_slow_delay(60, "撞了验证码")
    w2 = _worker(tmp_path)          # 模拟下一轮（新的 worker 对象）
    assert w2._is_slow_mode() is True


def test_慢档到期自动回快档(tmp_path):
    """过了退避时间就自动回快档，不用手动清"""
    w = _worker(tmp_path)
    w.st.set_slow_delay(0, "马上过期")   # 0 分钟 → 至少 1 分钟，见实现
    # 直接改库把过期时间拨到过去，模拟「到期」
    w.st._conn.execute(
        "UPDATE outreach_speed SET slow_until = '2000-01-01 00:00:00' WHERE id = 1"
    )
    w.st._conn.commit()
    assert w._is_slow_mode() is False
    lo, hi, _ = w._pick_delay()
    assert (lo, hi) == (20, 30)


def test_网页指定的间隔优先(tmp_path):
    """用户在页面上当场调了间隔 → 用他的，不走自适应"""
    w = _worker(tmp_path)
    w._delay_min = 5.0
    w._delay_max = 8.0
    lo, hi, why = w._pick_delay()
    assert (lo, hi) == (5, 8)
    assert "网页" in why


def test_网页调快了也逃不出慢档兜底(tmp_path):
    """撞过风控之后，网页上填的快间隔不能低于慢档下限

    用户显式指定优先，但风控兜底优先级更高 —— 账号比速度重要。
    """
    w = _worker(tmp_path)
    w.st.set_slow_delay(60, "撞了验证码")
    w._delay_min = 5.0
    w._delay_max = 8.0
    lo, hi, _ = w._pick_delay()
    assert lo >= 90, f"慢档时下限不能低于 90，实际 {lo}"
    assert hi >= 120, f"慢档时上限不能低于 120，实际 {hi}"


def test_关掉自适应就回到老配置(tmp_path):
    """adaptive_delay=False → 用 min_delay/max_delay"""
    w = _worker(tmp_path, adaptive_delay=False, min_delay=45.0, max_delay=120.0)
    lo, hi, why = w._pick_delay()
    assert (lo, hi) == (45, 120)
    assert "配置" in why


def test_连续干净后撤掉慢档(tmp_path):
    """连续 5 条都干净了 → 慢档撤掉，回到快档"""
    w = _worker(tmp_path, slow_delay_clear_after=5)
    w.st.set_slow_delay(60, "撞了验证码")
    w._clean_streak = 4
    w._maybe_exit_slow_mode()
    assert w._is_slow_mode() is True, "才 4 条不该撤"
    w._clean_streak = 5
    w._maybe_exit_slow_mode()
    assert w._is_slow_mode() is False, "满 5 条应该撤"


# ============================================================
# 飞书状态异步回写
# ============================================================

class _FakeWriter:
    """假的飞书写入器：记录调用，模拟 lark-cli 子进程的耗时"""

    def __init__(self):
        self.calls = []

    async def patch_fields(self, record_id, fields):
        await __import__("asyncio").sleep(0.2)   # 模拟 1.4 秒的网络往返
        self.calls.append((record_id, dict(fields)))
        return True, ""


def test_飞书回写不阻塞主流程(monkeypatch):
    """push() 必须立刻返回 —— 不能让 lark-cli 的网络往返卡住触达"""
    import src.notify.bitable as bitable
    fake = _FakeWriter()
    monkeypatch.setattr(bitable, "get_bitable_writer", lambda cfg=None: fake)

    p = _FeishuStatusPusher(get_config(), log=lambda m: None)
    t0 = time.time()
    p.push("rec123", {"触达状态": "已留言"})
    dt = time.time() - t0
    assert dt < 0.1, f"push 阻塞了 {dt*1000:.0f} 毫秒，应该立刻返回"
    assert p.flush(timeout=10) is True
    assert fake.calls == [("rec123", {"触达状态": "已留言"})]


def test_飞书写入器起不来也不能卡住(monkeypatch):
    """写入器初始化失败 → 队列要清掉，不能让 flush 永远等下去"""
    import src.notify.bitable as bitable

    def _boom(cfg=None):
        raise RuntimeError("lark-cli 没了")

    monkeypatch.setattr(bitable, "get_bitable_writer", _boom)
    p = _FeishuStatusPusher(get_config(), log=lambda m: None)
    p.push("rec123", {"触达状态": "已留言"})
    assert p.flush(timeout=5) is True, "写入器挂了也不能一直挂着"


def test_干跑不碰飞书(tmp_path):
    """干跑不写状态，自然也不该碰飞书"""
    w = _worker(tmp_path)
    w._dry = True
    w._set_status({"id": 1, "display_id": "dy001"}, "searching")
    # 队列里不该有东西
    with w._feishu._lock:
        assert w._feishu._pending == 0
