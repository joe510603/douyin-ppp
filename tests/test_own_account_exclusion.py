"""**自家账号绝不能进触达名单** —— 不能给门店官方号发留言广告

问题（2026-11-17 实测）
----------------------
门店官方号「小米汽车-成都金牛凯德体验店」自己在直播间发的口播文案
「SU7-YU7-澎程，首付、分期、全款、补贴各种方案，点击右下角私信主播领取！！」
被规则打分器判成「价格询问、金融补贴」→ 当成高意向客户入池 →
程序**给人家发了留言广告**（08:09:16 实发）。

根因
----
排除自家账号靠**昵称比对**，但两边对不上：

    配置里：  小米汽车-成都金牛凯德体验店 认证徽章   ← 带「认证徽章」后缀
    弹幕里：  小米汽车-成都金牛凯德体验店            ← 没有后缀

昵称会被改、会带后缀 → 不可靠。

可靠判据是 **secUid**（proto field 46，永久不变、唯一）：
弹幕发送者的 secUid == 主播的 secUid（anchor_id）→ 就是主播自己发的。

要钉住的（三道防线）：
1. **入口**：打分前按 secUid 排除自家账号 → 不再产生脏线索
2. **出口**：取待触达队列时按 secUid 剔除自家账号 → 历史脏线索也不发
3. 真的观众**不能**被误伤
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.intelligence.lead_service import LeadService
from src.models.comment import LiveComment, MessageType

ANCHOR_SEC = "MS4wLjABAAAAhTvFBd6MTBgp_rI4awCcWJwBboZnllpQMKkkhEtvyHw"
VIEWER_SEC = "MS4wLjABAAAAQJ2zCD0HHf9WxWyD70bl4Ijxs3WcghIgUDCIOK6p5BRR4IzFrh52eTjNAS4eQikM"
PITCH_TEXT = "SU7-YU7-澎程，首付、分期、全款、补贴各种方案，点击右下角私信主播领取！！"


def _svc() -> LeadService:
    svc = LeadService.__new__(LeadService)   # 不跑 __init__（会连 DB）
    svc.config = type("C", (), {"lead": type("L", (), {"realtime_enabled": True})})()
    svc.stats = {"scanned": 0, "hits": 0, "excluded": 0, "pushed": 0,
                 "tabled": 0, "dropped": 0}
    svc._own_sec_ids = {ANCHOR_SEC}
    svc._own_display_ids = set()
    svc._self_names = set()
    svc._queue = type("Q", (), {"put_nowait": lambda self, x: None})()
    return svc


def _comment(user_sec: str, content: str = PITCH_TEXT) -> LiveComment:
    return LiveComment(
        content=content,
        message_type=MessageType.CHAT,
        user_id="76067217273",
        user_sec_id=user_sec,
        user_display_id="76067217273",
        user_nickname="小米汽车-成都金牛凯德体验店",
        anchor_id=ANCHOR_SEC,
        anchor_name="小米汽车-成都金牛凯德体验店",
        room_id="7708737755269827362",
        monitor_name="小米汽车-成都金牛凯德体验店 认证徽章",
    )


def test_主播自己的口播文案不算线索():
    """这是本 bug 的核心场景：口播文案被判成「价格询问」。"""
    svc = _svc()
    assert svc._is_own_account(_comment(ANCHOR_SEC)) is True, \
        "主播自己发的弹幕必须被认出来"


def test_真观众不算自家账号():
    """别把真客户误伤 —— 这是排除逻辑最危险的副作用。"""
    svc = _svc()
    assert svc._is_own_account(_comment(VIEWER_SEC)) is False


def test_没有secUid时不能乱判():
    """老数据没有 secUid → 宁可放过，也不能把真客户误杀。"""
    svc = _svc()
    c = _comment(VIEWER_SEC)
    c.user_sec_id = ""
    assert svc._is_own_account(c) is False


def test_我们监控的其他账号也要排除():
    """连麦/多主播场景：自家任何一个账号发的都不算线索。"""
    svc = _svc()
    svc._own_sec_ids = {ANCHOR_SEC, VIEWER_SEC}
    assert svc._is_own_account(_comment(VIEWER_SEC)) is True


def test_触达队列剔掉自家账号():
    """第二道防线：历史脏线索也不能进触达名单。"""
    from src.outreach.store import OutreachStore

    st = OutreachStore.__new__(OutreachStore)
    st._own_sec_ids = lambda: {ANCHOR_SEC}
    leads = [
        {"id": 1, "sec_uid": ANCHOR_SEC, "nickname": "小米汽车-成都金牛凯德体验店"},
        {"id": 2, "sec_uid": VIEWER_SEC, "nickname": "真客户"},
        {"id": 3, "sec_uid": "", "nickname": "没有secUid的"},
    ]
    out = st._drop_own_accounts(leads)
    ids = [d["id"] for d in out]
    assert 1 not in ids, "自家账号必须被剔掉"
    assert 2 in ids and 3 in ids, "真客户和没 secUid 的都要保留"


def test_队列为空时原样返回():
    from src.outreach.store import OutreachStore
    st = OutreachStore.__new__(OutreachStore)
    st._own_sec_ids = lambda: {ANCHOR_SEC}
    assert st._drop_own_accounts([]) == []
