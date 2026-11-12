"""pytest 全局安全网

最重要的一条：**测试数据绝不能写进真实的飞书多维表格，也绝不能发真消息出去**。

背景
----
LeadService 在实时命中后会调用 ``sync_leads_to_bitable`` 自动写表，
而 config.yaml 里的 ``notify.bitable_enabled`` 是开着的（生产需要）。
如果不隔离，pytest 造出来的假线索（dy_e2e_001 之类）会真的出现在
销售的飞书表里——这在生产上是不可接受的污染。

通知通道是同一个坑，而且更隐蔽：
``notify.risk_webhook_url`` 是**生产群机器人的真实地址**
（2026-11-12 实测踩到：改完通知逻辑跑测试，6 个用例真的往用户群里
发了 6 张「抖音要求验证本人操作」的告警卡片）。
测试文件里那句「避免测试真的发消息出去」只挡住了私聊通道，
新加的群机器人通道没被挡住——**只要读生产配置，就一定会发出去**。

所以这里全局把**所有对外通道**都关掉：
- 多维表格写入（bitable）
- 群机器人 webhook（risk_webhook + 新线索推送 webhook）
- 飞书私聊（lark_im）

要测某个通道就**自己 monkeypatch 那个发送方法**，
别指望配置开关——配置是生产配置，一改就真发出去了。

覆盖范围：
- ``src.config.get_config`` —— 覆盖函数内 ``from ..config import get_config``
- ``src.intelligence.lead_service.get_config`` —— 覆盖模块级 import 的绑定
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_bitable(monkeypatch):
    """测试期间禁用多维表格写入 + 所有飞书对外通道"""
    from src.config import get_config

    cfg = get_config().model_copy(deep=True)
    cfg.notify.bitable_enabled = False

    # ---- 通知通道：全部关掉，测试不许真发消息出去 ----
    # 群机器人（触达告警）
    cfg.notify.risk_webhook_enabled = False
    cfg.notify.risk_webhook_url = ""
    cfg.notify.risk_webhook_secret = ""
    # 群机器人（新线索推送）
    cfg.notify.enabled = False
    cfg.notify.webhook_url = ""
    cfg.notify.secret = ""
    # 飞书私聊
    cfg.notify.lark_im_enabled = False

    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    monkeypatch.setattr("src.intelligence.lead_service.get_config", lambda: cfg)

    # ⚠ 上面那个 monkeypatch **覆盖不到模块级 import** 的地方：
    #   `from src.config import get_config` 在 import 那一刻就把函数绑死了，
    #   之后再改模块属性对它无效（很多测试文件就是这么写的）。
    #   结果就是那些文件拿到的是**带真实 webhook 的生产配置**，
    #   测试一跑就真的往用户群里发消息（2026-11-12 实测：6 张告警卡片）。
    #
    # 所以还要**直接把单例本身的对外通道关掉**——
    # 单例是所有 get_config() 返回的同一个对象，关了谁都发不出去。
    real = get_config()
    old = {
        "bitable_enabled": real.notify.bitable_enabled,
        "risk_webhook_enabled": real.notify.risk_webhook_enabled,
        "risk_webhook_url": real.notify.risk_webhook_url,
        "risk_webhook_secret": real.notify.risk_webhook_secret,
        "enabled": real.notify.enabled,
        "webhook_url": real.notify.webhook_url,
        "secret": real.notify.secret,
        "lark_im_enabled": real.notify.lark_im_enabled,
    }
    real.notify.bitable_enabled = False
    real.notify.risk_webhook_enabled = False
    real.notify.risk_webhook_url = ""
    real.notify.risk_webhook_secret = ""
    real.notify.enabled = False
    real.notify.webhook_url = ""
    real.notify.secret = ""
    real.notify.lark_im_enabled = False
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(real.notify, k, v)
