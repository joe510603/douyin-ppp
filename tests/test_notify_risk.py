"""触达告警走哪个通道 —— 群机器人优先，私聊兜底

背景
----
2026-11-12 实测：lark-cli 私聊消息**发得出去**（返回 ok=true，消息确实到了），
但飞书**私聊会话不产生手机推送**——消息躺在那儿，人根本不知道。

用户反馈「目前这个提醒方式飞书不会有提示」就是这个原因。
所以改成：**群机器人发红卡片 + @人**（群消息会推，@ 还会触发强提醒），
私聊只当兜底。

要盯住的：
1. 群机器人配了 → 走群机器人，**不**再走私聊（别重复打扰）
2. 群机器人没配 → 退私聊（总比一声不吭强）
3. 群机器人配了但发失败 → 退私聊
4. 两个都没配 → 返回 False，只在页面上提示，不抛异常
5. 卡片里要带 @ 人（@ 才是飞书的强提醒）
"""

from __future__ import annotations

import sqlite3

import pytest

from src.config import get_config
from src.outreach.worker import OutreachWorker

LEADS_DDL = """
CREATE TABLE leads (
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

OUTREACH_RECORDS_DDL = """
CREATE TABLE outreach_records (
    id TEXT PRIMARY KEY,
    lead_id INTEGER DEFAULT NULL,
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    record_id TEXT DEFAULT '',
    sec_uid TEXT DEFAULT '',
    aweme_id TEXT DEFAULT '',
    aweme_desc TEXT DEFAULT '',
    template_id TEXT DEFAULT '',
    channel TEXT DEFAULT 'comment',
    verified INTEGER DEFAULT 0
)
"""


def _make_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(LEADS_DDL)
    conn.execute(OUTREACH_RECORDS_DDL)
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def notifier_calls(monkeypatch):
    """记录两个通道各自被调用了几次、参数是什么"""
    calls: dict = {"group": [], "im": []}

    async def fake_group(self, **kw):
        calls["group"].append(kw)
        if getattr(fake_group, "should_fail", False):
            return False, "模拟发送失败"
        return True, ""

    async def fake_im(self, text):
        calls["im"].append(text)
        return True, ""

    monkeypatch.setattr(
        "src.notify.feishu.FeishuNotifier.send_risk_card", fake_group, raising=True
    )
    monkeypatch.setattr(
        "src.notify.lark_im.LarkIMNotifier.send_text", fake_im, raising=True
    )
    return calls


@pytest.fixture
def cfg_notify():
    cfg = get_config()
    n = cfg.notify
    old = (
        n.risk_webhook_enabled, n.risk_webhook_url, n.risk_webhook_secret,
        n.risk_at_open_id, n.lark_im_enabled, n.lark_im_open_id,
    )
    n.risk_webhook_enabled = True
    n.risk_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/FAKE"
    n.risk_webhook_secret = ""
    n.risk_at_open_id = "ou_test"
    n.lark_im_enabled = True
    n.lark_im_open_id = "ou_test"
    yield n
    (n.risk_webhook_enabled, n.risk_webhook_url, n.risk_webhook_secret,
     n.risk_at_open_id, n.lark_im_enabled, n.lark_im_open_id) = old


def _worker(tmp_path):
    return OutreachWorker(get_config(), _make_db(tmp_path), log=lambda m: None)


def test_群机器人配了就走群机器人(tmp_path, cfg_notify, notifier_calls):
    w = _worker(tmp_path)
    ok = w._notify_risk("verify", "抖音要求验证本人操作", {"display_id": "dy001"})
    assert ok is True
    assert len(notifier_calls["group"]) == 1
    assert notifier_calls["im"] == [], "群机器人通了就不该再私信，别重复打扰"


def test_群机器人卡片里要带上要艾特的人(tmp_path, cfg_notify, notifier_calls):
    w = _worker(tmp_path)
    w._notify_risk("verify", "要求验证", {"display_id": "dy001"})
    kw = notifier_calls["group"][0]
    assert kw["at_open_id"] == "ou_test", "不 @ 人就不会触发飞书强提醒"
    assert kw["who"], "要写清楚卡在哪条线索上"
    assert kw["steps"], "要写清楚怎么办"


def test_群机器人没配就退私聊(tmp_path, cfg_notify, notifier_calls):
    cfg_notify.risk_webhook_url = ""
    w = _worker(tmp_path)
    ok = w._notify_risk("verify", "要求验证", {"display_id": "dy001"})
    assert ok is True
    assert notifier_calls["group"] == []
    assert len(notifier_calls["im"]) == 1


def test_群机器人发失败要退私聊(tmp_path, cfg_notify, notifier_calls, monkeypatch):
    import src.notify.feishu as feishu_mod

    async def failing(self, **kw):
        notifier_calls["group"].append(kw)
        return False, "模拟失败"

    monkeypatch.setattr(feishu_mod.FeishuNotifier, "send_risk_card", failing)
    w = _worker(tmp_path)
    ok = w._notify_risk("verify", "要求验证", {"display_id": "dy001"})
    assert ok is True
    assert len(notifier_calls["group"]) == 1
    assert len(notifier_calls["im"]) == 1, "群机器人失败不能就这么算了"


def test_两个都没配就只在页面提示(tmp_path, cfg_notify, notifier_calls):
    cfg_notify.risk_webhook_url = ""
    cfg_notify.lark_im_enabled = False
    w = _worker(tmp_path)
    ok = w._notify_risk("verify", "要求验证", {"display_id": "dy001"})
    assert ok is False
    assert notifier_calls["group"] == []
    assert notifier_calls["im"] == []


def test_群机器人开关关掉就不走群(tmp_path, cfg_notify, notifier_calls):
    cfg_notify.risk_webhook_enabled = False
    w = _worker(tmp_path)
    w._notify_risk("verify", "要求验证", {"display_id": "dy001"})
    assert notifier_calls["group"] == []
    assert len(notifier_calls["im"]) == 1


# ============================================================
# 卡片本身：红色 + 标题 + 内容
# ============================================================

def test_风险卡片是红色的且带标题():
    """卡片要够扎眼——飞书里红色头部才叫「告警」"""
    from src.notify.feishu import FeishuNotifier

    n = FeishuNotifier(webhook_url="https://example.invalid/hook")
    sent: dict = {}

    async def fake_send(body):
        sent["body"] = body
        return True, ""

    n.send = fake_send
    import asyncio

    ok, err = asyncio.run(n.send_risk_card(
        title="⚠️ 抖音要求验证本人操作",
        reason="平台原话",
        steps="1. 打开浏览器窗口\n2. 拖拼图",
        at_open_id="ou_xxx",
        who="抖音号 dy001",
    ))
    assert ok is True
    body = sent["body"]
    assert body["msg_type"] == "interactive"
    card = body["card"]
    assert card["header"]["template"] == "red"
    assert "验证" in card["header"]["title"]["content"]
    text = "\n".join(
        e.get("text", {}).get("content", "") for e in card["elements"]
    )
    assert "ou_xxx" in text, "卡片里要真的 @ 到人"
    assert "dy001" in text
    assert "拖拼图" in text
