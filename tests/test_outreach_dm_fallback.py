"""私信兜底：没作品 → 改发私信

用户 2026-11-14：「如果一个抖音号没有作品，那就发私信。如果是私密账号就添加好友」。

实测（2026-11-14 探针，8 个真实主页）：
- 主页上「私信」按钮 8/8 可见，私信面板输入框 7/8 能渲染出来
- 私密账号主页上有一行字「**私密账号**」→ 判据成立
- **web 端根本没有「加好友 / 添加朋友」按钮**（0/8）——
  抖音的好友功能只在 App 里，网页版做不到。
  所以私密账号只能走私信 + 销售手动用 App 加。

盯住的：
1. 没作品 → 走私信，不是直接跳过
2. 私信的额度**跟留言分开算**（陌生人私信风控严得多）
3. 私信发不出去 → 退回「跳过」，连续失败到阈值 → 停手
4. 私信成功 → 本地状态 messaged、飞书写「已私信」
5. 私信之后要多等一会儿（比留言慢）
6. 干跑不发送、不回写状态
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.store import (  # noqa: E402
    STATUS_MESSAGED, STATUS_SKIPPED, OutreachStore,
)
from src.outreach.worker import OutreachWorker  # noqa: E402


# ============================================================
# 假对象
# ============================================================

class _FakeDouyin:
    """假装是个 Douyin

    只实现私信兜底这条路径用到的东西：
    send_dm / _body_text / check_risk
    """

    def __init__(self, dm_ok: bool = True, body: str = "", msg: str = "",
                 is_private: bool = False):
        self.dm_ok = dm_ok
        self.body = body
        self.msg = msg
        self.is_private = is_private
        self.sent: list[tuple[str, str]] = []
        self.calls = 0

    def send_dm(self, sec_uid: str, text: str) -> dict:
        self.calls += 1
        if self.dm_ok:
            self.sent.append((sec_uid, text))
            return {"ok": True, "msg": "已发送", "is_private": self.is_private}
        return {"ok": False, "msg": self.msg or "私信功能已关闭",
                "is_private": self.is_private}

    def detect_private_account(self) -> bool:
        """跟真实实现一样：当前页面上有没有「私密账号」那行字"""
        return "私密账号" in (self.body or "")

    def _body_text(self) -> str:
        return self.body

    def check_risk(self) -> None:
        return None


def _建库(tmp_path):
    """建一个带 leads 表的测试库

    列跟真实的 leads 表对齐 —— pending_leads / set_outreach_status
    会 SELECT 一堆列，少一列就 no such column。
    """
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
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, not_found_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
        " outreach_status, status) VALUES (1, 'test_dy_id', '测试昵称A', "
        " 'MS4wLjABAAAAtest', '测试门店', 'pending', 'new')"
    )
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, **cfg_over):
    # ⚠ 必须用**独立副本**：``get_config()`` 返回的是全局单例，
    #   别的测试改了间隔配置会串到这边来（全量跑就挂、单跑就过）。
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.dm_fallback = True
    cfg.outreach.dm_daily_limit = 10
    cfg.outreach.dm_fail_threshold = 2
    cfg.outreach.dm_delay_min = 90
    cfg.outreach.dm_delay_max = 150
    # 留言的间隔也钉死，免得被别的测试改掉
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 20
    cfg.outreach.max_delay = 30
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(_建库(tmp_path)), log=logs.append)
    # worker 的 _dry 默认是 True（run() 里才按参数覆盖），
    # 这里默认走真发分支，要测干跑的用例自己再打开。
    w._dry = False
    return w, logs


def _lead(**kw) -> dict:
    lead = {
        "id": 1,
        "display_id": "test_dy_id",
        "nickname": "测试昵称A",
        "sec_uid": "MS4wLjABAAAAtest",
        "store": "测试门店",
        "car_model": "小米SU7",
        "contact_type": "",
        "contact_value": "",
        "evidence": "[]",
        "monitor_name": "测试直播间",
        "bitable_record_id": "",
        "intent_level": "high",
        "intent_score": 90,
    }
    lead.update(kw)
    return lead


# ============================================================
# 额度：私信跟留言分开算
# ============================================================

def test_私信额度跟留言分开(tmp_path):
    """留言发到 100 条也不该把私信额度用掉

    陌生人私信被限 → 账号的私信功能被关 → 连留言一起废。
    所以两个额度必须分开。
    """
    w, _ = _worker(tmp_path, dm_daily_limit=10)
    try:
        # 记 5 条留言 + 3 条私信
        for i in range(5):
            w.st.add_touch(lead_id=1, display_id=f"c{i}", status="sent",
                           channel="comment")
        for i in range(3):
            w.st.add_touch(lead_id=1, display_id=f"d{i}", status="sent",
                           channel="dm")
        assert w.st.sent_today() == 8, "留言和私信都要算进「今天发了多少」"
        assert w.st.dm_sent_today() == 3, "私信额度只看私信"
        assert w._dm_quota_left() == 7, "私信还剩 7 条额度"
    finally:
        w.st.close()


def test_私信额度用完就不再发(tmp_path):
    w, logs = _worker(tmp_path, dm_daily_limit=2)
    try:
        for i in range(2):
            w.st.add_touch(lead_id=1, display_id=f"d{i}", status="sent",
                           channel="dm")
        assert w._dm_quota_left() == 0
        dy = _FakeDouyin()
        with pytest.raises(Exception) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert "额度" in str(ei.value), str(ei.value)
        assert dy.calls == 0, "额度用完就不该再发私信"
    finally:
        w.st.close()


# ============================================================
# 主流程：没作品 → 私信
# ============================================================

def test_没作品改发私信(tmp_path):
    """核心：以前是「跳过」，现在要发私信"""
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=True)
        summary = {"dry": 0}
        w._fallback_to_dm(dy, _lead(), summary, why="该用户没有公开作品")
        assert dy.calls == 1, "应该真的调了私信"
        assert summary.get("messaged") == 1
        # 私信正文要用 dm 版本（更长，一次说清来意）
        assert dy.sent[0][1], "私信正文不能为空"
        # 本地状态
        row = w.st.get_lead(1)
        assert row and row.get("outreach_status") == STATUS_MESSAGED
    finally:
        w.st.close()


def test_私信成功会记一条channel为dm的触达记录(tmp_path):
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=True)
        w._fallback_to_dm(dy, _lead(), {"dry": 0})
        rows = w.st.recent_touches(limit=5)
        assert any(r["channel"] == "dm" and r["status"] == "sent" for r in rows), rows
    finally:
        w.st.close()


def test_私信发不出去退回跳过(tmp_path):
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="对方未关注")
        with pytest.raises(Exception) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        # 失败 → NoWorks（worker 会把它记成「跳过」，留给人工）
        assert "私信也没发出去" in str(ei.value), str(ei.value)
    finally:
        w.st.close()


def test_连续私信失败到阈值就停手(tmp_path):
    """私信被限是账号级的风险，不能一直撞

    跟留言的「软风控」一个道理，但计数**分开**：
    留言发不出去不代表私信发不出去，反之亦然。
    """
    w, _ = _worker(tmp_path, dm_fail_threshold=2)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="私信功能已关闭")
        with pytest.raises(Exception):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})      # 第 1 次
        with pytest.raises(Exception) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})      # 第 2 次 → 停手
        assert "私信功能被限" in str(ei.value), str(ei.value)
    finally:
        w.st.close()


def test_干跑不发送也不回写(tmp_path):
    """干跑只验证「能不能找到人」，私信更不能真发

    真发了私信就收不回来了 —— 陌生人私信只有一次机会。
    """
    w, _ = _worker(tmp_path)
    try:
        w._dry = True
        dy = _FakeDouyin(dm_ok=True)
        summary = {"dry": 0}
        w._fallback_to_dm(dy, _lead(), summary)
        assert dy.calls == 0, "干跑绝不能发私信"
        assert summary.get("dry") == 1
    finally:
        w._dry = False
        w.st.close()


# ============================================================
# 私密账号
# ============================================================

def test_能认出私密账号(tmp_path):
    """主页上有一行「私密账号」就是它

    判据要精确：页面上还有「私密作品」这类别的文案，
    跟「私密账号」不是一回事。
    """
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(body="关注 粉丝 获赞\n私密账号\n作品 0")
        lead = _lead()
        assert w._detect_private_account(dy, lead) is True
        assert lead.get("is_private_account") is True
    finally:
        w.st.close()


def test_不是私密账号就别乱判(tmp_path):
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(body="关注 粉丝 获赞\n作品 12\n私密作品")
        assert w._detect_private_account(dy, _lead()) is False
    finally:
        w.st.close()


def test_私密账号的私信要提示销售手动加好友(tmp_path):
    """网页版加不了好友 → 私信之外还要给销售留个话

    销售看到「私密账号」才知道要用抖音 App 手动加好友。
    """
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=True, body="私密账号")
        w._fallback_to_dm(dy, _lead(), {"dry": 0})
        joined = " ".join(logs)
        assert "加好友" in joined, joined
    finally:
        w.st.close()


def test_私密账号以发送结果为准(tmp_path):
    """发送过程一定打开过对方主页 → 这时的判断才是准的

    发送前页面可能还停在搜索结果页（那儿没有「私密账号」字样），
    判成「不是」就是错的。所以要以 send_dm 返回的为准。
    """
    w, logs = _worker(tmp_path)
    try:
        # 发送前页面是搜索结果页（没有「私密账号」）
        dy = _FakeDouyin(dm_ok=True, body="搜索结果", is_private=True)
        w._fallback_to_dm(dy, _lead(), {"dry": 0})
        joined = " ".join(logs)
        assert "私密账号" in joined, joined
    finally:
        w.st.close()


# ============================================================
# 间隔：私信之后要多等
# ============================================================

def test_私信之后用更长的间隔(tmp_path):
    """紧挨着发两条私信很容易被判定为骚扰"""
    w, logs = _worker(tmp_path, dm_delay_min=90, dm_delay_max=150)
    try:
        slept: list[float] = []
        w._sleep = lambda sec: slept.append(sec)
        w._last_channel = "dm"
        w._sleep_between()
        assert slept and 90 <= slept[0] <= 150, slept
    finally:
        w.st.close()


def test_留言之后用正常间隔(tmp_path):
    w, logs = _worker(tmp_path)
    try:
        slept: list[float] = []
        w._sleep = lambda sec: slept.append(sec)
        w._last_channel = "comment"
        w._sleep_between()
        assert slept, slept
        assert 20 <= slept[0] <= 30, f"留言该用快档间隔，实际 {slept}"
    finally:
        w.st.close()


# ============================================================
# 状态映射：本地 ↔ 飞书
# ============================================================

def test_已私信的飞书映射齐全():
    """本地状态、飞书选项名、网页筛选三处要对得上

    对不上就会出现「本地说已私信、飞书说已跳过」这种鬼话。
    """
    from src.intelligence.lead_service import _FEISHU_OUTREACH_TO_LOCAL
    from src.notify.bitable import OUTREACH_LABELS
    from src.web.lead_page import OUTREACH_LABELS as PAGE_LABELS

    assert OUTREACH_LABELS.get("messaged") == "已私信"
    assert PAGE_LABELS.get("messaged") == "已私信"
    assert _FEISHU_OUTREACH_TO_LOCAL.get("已私信") == "messaged"


def test_已私信是终态不进队列(tmp_path):
    """私信过了就别再留言了 —— 陌生人私信只有一次机会

    重复发私信会被抖音判成骚扰，代价很大。
    """
    db = _建库(tmp_path)
    st = OutreachStore(db)
    try:
        st.set_outreach_status(1, STATUS_MESSAGED, "已私信")
        pending = st.pending_leads()
        ids = [p["id"] for p in pending]
        assert 1 not in ids, "已私信的不该在待触达队列里"
    finally:
        st.close()
