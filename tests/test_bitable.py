"""飞书多维表格写入器测试 — 双通道凭据 + 字段映射"""

import asyncio

import pytest

from src.notify.bitable import (
    LEAD_TABLE_FIELDS,
    TYPE_SELECT,
    TYPE_TEXT,
    BitableWriter,
)


# ============================================================
# httpx 假客户端
# ============================================================

CALLS: list[tuple] = []
TOKEN_PAYLOAD = {"code": 0, "tenant_access_token": "t-abc123", "expire": 7200}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        CALLS.append(("POST", url, json))
        if "tenant_access_token" in url:
            return _FakeResponse(TOKEN_PAYLOAD)
        return _FakeResponse({"code": 0, "data": {"items": [{"field_name": "抖音号"}]}})

    async def get(self, url, headers=None, params=None, **kwargs):
        CALLS.append(("GET", url, None))
        return _FakeResponse({"code": 0, "data": {"items": [{"field_name": "抖音号"}]}})


@pytest.fixture(autouse=True)
def _reset_calls(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr("src.notify.bitable.httpx.AsyncClient", _FakeClient)
    yield
    CALLS.clear()


def _writer(**kwargs) -> BitableWriter:
    base = dict(
        app_id="cli_xxx",
        app_secret="secret",
        app_token="bascnXXX",
        table_id="tblXXX",
    )
    base.update(kwargs)
    return BitableWriter(**base)


# ============================================================
# 凭据：两条通道
# ============================================================

def test_is_configured_requires_table_identifiers():
    w = _writer(app_token="", table_id="")
    assert w.is_configured is False


def test_is_configured_with_app_credentials():
    w = _writer()
    assert w.has_app_credentials is True
    assert w.is_configured is True


def test_is_configured_with_user_token_only():
    w = _writer(app_id="", app_secret="", user_token="u-token")
    assert w.has_app_credentials is False
    assert w.is_configured is True


def test_is_configured_without_any_credential():
    w = _writer(app_id="", app_secret="", user_token="")
    assert w.is_configured is False


def test_user_token_channel_skips_app_auth():
    """只配 user_token 时不应去打 app 鉴权接口"""
    w = _writer(app_id="", app_secret="", user_token="u-token")
    token = asyncio.run(w._get_token())
    assert token == "u-token"
    assert not any("tenant_access_token" in c[1] for c in CALLS)


def test_app_channel_fetches_and_caches_tenant_token():
    w = _writer()
    first = asyncio.run(w._get_token())
    second = asyncio.run(w._get_token())
    assert first == "t-abc123"
    assert second == "t-abc123"
    auth_calls = [c for c in CALLS if "tenant_access_token" in c[1]]
    assert len(auth_calls) == 1, "token 应被缓存，不应重复请求"


def test_app_channel_takes_priority_over_user_token():
    w = _writer(user_token="u-token")
    token = asyncio.run(w._get_token())
    assert token == "t-abc123"


# ============================================================
# 字段映射
# ============================================================

def _lead(**kwargs) -> dict:
    base = {
        "lead_key": "dy001",
        "display_id": "dy001",
        "nickname": "张三",
        "intent_level": "high",
        "intent_score": 88,
        "intent_reason": "命中信号：价格询问",
        "car_model": "YU7",
        "contact_value": "",
        "monitor_name": "小米汽车 | 机场路店",
        "evidence": '["落地多少"]',
        "status": "new",
        "outreach_status": "pending",
        "_found_ts": 1794061350000,
    }
    base.update(kwargs)
    return base


def test_build_fields_maps_known_values():
    f = _writer()._build_fields(_lead())
    assert f["抖音号"] == "dy001"
    assert f["昵称"] == "张三"
    assert f["意向等级"] == "高意向"
    assert f["意向分"] == 88
    assert f["车型"] == "YU7"
    assert f["来源直播间"] == "小米汽车 | 机场路店"
    assert f["处理状态"] == "待处理"
    assert f["触达状态"] == "待触达"
    assert f["发现时间"] == 1794061350000


def test_build_fields_flattens_evidence_json():
    f = _writer()._build_fields(_lead(evidence='["落地多少", "有现车吗"]'))
    assert f["弹幕原文"] == "落地多少 / 有现车吗"


def test_build_fields_omits_empty_contact():
    """空联系方式要整条省掉，不能传空串给字段"""
    f = _writer()._build_fields(_lead(contact_value=""))
    assert "联系方式" not in f


def test_build_fields_keeps_contact_when_present():
    f = _writer()._build_fields(_lead(contact_value="13800138000"))
    assert f["联系方式"] == "13800138000"


def test_build_fields_maps_all_levels():
    for level, expected in [("high", "高意向"), ("medium", "中意向"), ("low", "低意向")]:
        f = _writer()._build_fields(_lead(intent_level=level))
        assert f["意向等级"] == expected


def test_build_fields_drops_unknown_select():
    """等级不认识时不能硬塞 None，应整条省掉"""
    f = _writer()._build_fields(_lead(intent_level="none"))
    assert "意向等级" not in f


def test_build_fields_maps_outreach_states():
    for st, expected in [
        ("searching", "搜索中"), ("found", "已定位"),
        ("commented", "已留言"), ("failed", "失败"),
    ]:
        f = _writer()._build_fields(_lead(outreach_status=st))
        assert f["触达状态"] == expected


# ============================================================
# 字段规格
# ============================================================

def test_field_spec_covers_twelve_fields():
    names = [f["name"] for f in LEAD_TABLE_FIELDS]
    assert len(names) == 12
    assert names == [
        "抖音号", "昵称", "意向等级", "意向分", "车型", "联系方式",
        "判定依据", "来源直播间", "弹幕原文", "处理状态", "触达状态", "发现时间",
    ]


def test_contact_field_is_text_not_phone():
    """联系方式可能是微信/QQ，用电话字段会被开放平台拒掉"""
    spec = next(f for f in LEAD_TABLE_FIELDS if f["name"] == "联系方式")
    assert spec["type"] == TYPE_TEXT


def test_select_fields_declare_options():
    for name in ("意向等级", "处理状态", "触达状态"):
        spec = next(f for f in LEAD_TABLE_FIELDS if f["name"] == name)
        assert spec["type"] == TYPE_SELECT
        assert spec.get("options"), f"{name} 应声明选项"
