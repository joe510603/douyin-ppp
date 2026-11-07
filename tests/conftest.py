"""pytest 全局安全网

最重要的一条：**测试数据绝不能写进真实的飞书多维表格**。

背景：LeadService 在实时命中后会调用 ``sync_leads_to_bitable`` 自动写表，
而 config.yaml 里的 ``notify.bitable_enabled`` 是开着的（生产需要）。
如果不隔离，pytest 造出来的假线索（dy_e2e_001 之类）会真的出现在
销售的飞书表里——这在生产上是不可接受的污染。

所以这里全局把多维表格写入关掉：
- ``src.config.get_config`` —— 覆盖函数内 ``from ..config import get_config``
- ``src.intelligence.lead_service.get_config`` —— 覆盖模块级 import 的绑定
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_bitable(monkeypatch):
    """测试期间禁用多维表格写入"""
    from src.config import get_config

    cfg = get_config().model_copy(deep=True)
    cfg.notify.bitable_enabled = False

    monkeypatch.setattr("src.config.get_config", lambda: cfg)
    monkeypatch.setattr("src.intelligence.lead_service.get_config", lambda: cfg)
    yield
