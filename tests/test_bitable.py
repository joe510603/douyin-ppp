"""飞书多维表格写入器测试 — 双通道凭据 + 字段映射"""

import asyncio

import pytest

from src.notify.bitable import (
    LEAD_TABLE_FIELDS,
    LEAD_TABLE_FIELDS_V3,
    TYPE_SELECT,
    TYPE_TEXT,
    BitableWriter,
    LarkCliBitableWriter,
    _cell_text,
    _lead_to_fields,
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


# ============================================================
# lark-cli 后端
# ============================================================

def _cli_writer(**kwargs) -> LarkCliBitableWriter:
    base = dict(
        app_token="basXXX",
        table_id="tblXXX",
        identity="user",
        cli_path="/bin/echo",   # 存在的假路径，只为通过 is_configured
    )
    base.update(kwargs)
    return LarkCliBitableWriter(**base)


def test_cli_writer_requires_table_and_binary():
    w = _cli_writer(app_token="", table_id="")
    assert w.is_configured is False
    w2 = _cli_writer(table_id="")
    assert w2.is_configured is False


def test_cli_writer_falls_back_to_auto_probe():
    """显式路径不存在时回退到 PATH 探测（本机有 lark-cli 时仍可用）"""
    w = _cli_writer(cli_path="/definitely/not/here")
    resolved = w.cli_path
    assert resolved
    assert w.is_configured is True


def test_cli_writer_builds_fields_with_string_date():
    """v3 API 的日期字段用字符串，不是毫秒时间戳"""
    w = _cli_writer()
    f = w._build_fields(_lead(_found_at="2026-11-07 15:56:06"))
    assert f["发现时间"] == "2026-11-07 15:56:06"
    assert f["抖音号"] == "dy001"
    assert f["意向等级"] == "高意向"


def test_cell_text_handles_all_shapes():
    assert _cell_text(None) == ""
    assert _cell_text("abc") == "abc"
    assert _cell_text(["高意向"]) == "高意向"
    assert _cell_text([{"text": "高意向"}]) == "高意向"
    assert _cell_text({"text": "x"}) == "x"
    assert _cell_text(90) == "90"


def test_lead_to_fields_drops_none_select():
    fields = _lead_to_fields(_lead(intent_level="none"), 0)
    assert "意向等级" not in fields
    assert fields["意向分"] == 88


def test_cli_writer_parses_record_list():
    """record-list 返回二维数组 + record_id_list，要按列名找抖音号"""
    w = _cli_writer()

    async def fake_run(args, payload=None):
        assert args[1] == "+record-list"
        return {
            "data": {
                "fields": ["抖音号", "昵称"],
                "record_id_list": ["rec1", "rec2"],
                "data": [["dy001", "张三"], ["dy002", "李四"]],
            }
        }

    w._run = fake_run
    rid = asyncio.run(w.search_by_douyin_id("dy002"))
    assert rid == "rec2"
    assert asyncio.run(w.search_by_douyin_id("nope")) is None


def test_cli_writer_sync_updates_existing():
    """已存在的抖音号走批量 update，不新建"""
    w = _cli_writer()
    calls = []

    async def fake_run(args, payload=None):
        calls.append((args[1], payload))
        if args[1] == "+field-list":
            return {"data": {"fields": [{"name": f["name"]} for f in LEAD_TABLE_FIELDS_V3]}}
        if args[1] == "+record-list":
            return {"data": {"fields": ["抖音号"], "record_id_list": ["recX"], "data": [["dy001"]]}}
        if args[1] == "+record-batch-update":
            return {"data": {}}
        return {"data": {"record_id_list": []}}

    w._run = fake_run
    res = asyncio.run(w.sync_leads([_lead()]))
    assert res["updated"] == 1
    assert res["created"] == 0
    assert res["record_ids"]["dy001"] == "recX"
    assert any(c[0] == "+record-batch-update" for c in calls)
    assert not any(c[0] == "+record-batch-create" for c in calls)


def test_cli_writer_sync_skips_table_scan_when_record_id_known():
    """已有 record_id 时不该再全表扫描一次"""
    w = _cli_writer()
    calls = []

    async def fake_run(args, payload=None):
        calls.append(args[1])
        if args[1] == "+field-list":
            return {"data": {"fields": [{"name": f["name"]} for f in LEAD_TABLE_FIELDS_V3]}}
        if args[1] == "+record-batch-update":
            return {"data": {}}
        return {"data": {}}

    w._run = fake_run
    res = asyncio.run(w.sync_leads([_lead(_bitable_record_id="recKnown")]))
    assert res["updated"] == 1
    assert "+record-list" not in calls
    assert res["record_ids"]["dy001"] == "recKnown"


def test_cli_writer_sync_creates_when_missing():
    w = _cli_writer()

    async def fake_run(args, payload=None):
        if args[1] == "+field-list":
            return {"data": {"fields": [{"name": f["name"]} for f in LEAD_TABLE_FIELDS_V3]}}
        if args[1] == "+record-list":
            return {"data": {"fields": ["抖音号"], "record_id_list": [], "data": []}}
        if args[1] == "+record-batch-create":
            return {"data": {"record_id_list": ["recNew"]}}
        return {"data": {}}

    w._run = fake_run
    res = asyncio.run(w.sync_leads([_lead()]))
    assert res["created"] == 1
    assert res["record_ids"]["dy001"] == "recNew"


def test_cli_writer_batch_update_uses_per_record_values():
    """批量更新是「每条记录一份字段值」，不能同值套用"""
    w = _cli_writer()
    seen = {}

    async def fake_run(args, payload=None):
        if args[1] == "+field-list":
            return {"data": {"fields": [{"name": f["name"]} for f in LEAD_TABLE_FIELDS_V3]}}
        if args[1] == "+record-list":
            return {"data": {"fields": ["抖音号"], "record_id_list": ["r1", "r2"], "data": [["a"], ["b"]]}}
        if args[1] == "+record-batch-update":
            seen.update(payload)
            return {"data": {}}
        return {"data": {}}

    w._run = fake_run
    asyncio.run(w.sync_leads([
        _lead(display_id="a", intent_score=90),
        _lead(display_id="b", intent_score=50),
    ]))
    assert set(seen["update_records"].keys()) == {"r1", "r2"}
    assert seen["update_records"]["r1"]["意向分"] == 90
    assert seen["update_records"]["r2"]["意向分"] == 50


def test_v3_field_spec_matches_runtime_spec():
    """bootstrap 脚本和运行时必须用同一份字段定义"""
    assert [f["name"] for f in LEAD_TABLE_FIELDS_V3] == [f["name"] for f in LEAD_TABLE_FIELDS]


def test_get_bitable_writer_dispatch():
    """auto：没有 app 凭据时走 lark_cli，有凭据时走 http"""
    from src.config import Settings
    from src.notify.bitable import get_bitable_writer

    cfg = Settings()
    cfg.notify.bitable_app_token = "basXXX"
    cfg.notify.bitable_table_id = "tblXXX"
    cfg.notify.bitable_backend = "auto"
    assert isinstance(get_bitable_writer(cfg), LarkCliBitableWriter)

    cfg.notify.bitable_app_id = "cli_x"
    cfg.notify.bitable_app_secret = "s"
    assert isinstance(get_bitable_writer(cfg), BitableWriter)

    cfg.notify.bitable_backend = "lark_cli"
    assert isinstance(get_bitable_writer(cfg), LarkCliBitableWriter)
