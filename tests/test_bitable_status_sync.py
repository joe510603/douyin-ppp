"""飞书表状态双向同步测试 — 保护销售在飞书里改过的值

背景：
「处理状态」「触达状态」是**双向**字段。
- 销售在飞书表里手动改（标「已跟进」「已忽略」「已跳过」）
- 本地网页线索中心也能改

本程序只负责往飞书写线索。如果每次同步都把本地默认值写回去，
销售刚标完「已跟进」，后台下一轮同步就把它抹回「待处理」。

这里覆盖三条防线：
1. 推送守卫 —— 本地状态没变过就不写状态字段
2. 回拉 —— 销售改的值能流回本地
3. 门店归一化 —— 同一个门店的多种写法收敛成同一个短名
"""

import pytest

from src.notify.bitable import (
    build_synced_status,
    normalize_store,
    status_changed,
    _lead_to_fields,
)


def _lead(**over):
    base = {
        "lead_key": "k1",
        "display_id": "abc123",
        "nickname": "小明",
        "intent_level": "high",
        "intent_score": 60,
        "intent_reason": "落地多少",
        "car_model": "SU7",
        "contact_value": "",
        "monitor_name": "小米汽车｜世茂广场店",
        "live_room": "",
        "evidence": "[]",
        "status": "new",
        "outreach_status": "pending",
        "bitable_synced_status": "",
    }
    base.update(over)
    return base


# ============================================================
# 推送守卫：status_changed
# ============================================================

def test_status_changed_true_when_never_synced():
    """从没同步过 → 状态由本地决定，应该写"""
    assert status_changed(_lead(bitable_synced_status="")) is True


def test_status_changed_false_when_local_unchanged():
    """本地状态和上次同步一样 → 不能覆盖飞书端的值"""
    lead = _lead(
        status="new",
        outreach_status="pending",
        bitable_synced_status="待处理|待触达",
    )
    assert status_changed(lead) is False


def test_status_changed_true_when_local_status_moved():
    """本地把状态改成「已跟进」→ 应该写进飞书"""
    lead = _lead(
        status="contacted",
        outreach_status="pending",
        bitable_synced_status="待处理|待触达",
    )
    assert status_changed(lead) is True


def test_status_changed_true_when_local_outreach_moved():
    """本地触达状态变了也要写"""
    lead = _lead(
        status="new",
        outreach_status="commented",
        bitable_synced_status="待处理|待触达",
    )
    assert status_changed(lead) is True


def test_status_changed_true_when_local_ignored():
    lead = _lead(
        status="ignored",
        outreach_status="pending",
        bitable_synced_status="待处理|待触达",
    )
    assert status_changed(lead) is True


# ============================================================
# 同步标记：build_synced_status
# ============================================================

def test_build_synced_status_format():
    lead = _lead(status="contacted", outreach_status="commented")
    assert build_synced_status(lead) == "已跟进|已留言"


def test_build_synced_status_roundtrip():
    """标记写进去后，status_changed 应该认为「没变」"""
    lead = _lead(status="contacted", outreach_status="pending")
    marker = build_synced_status(lead)
    lead["bitable_synced_status"] = marker
    assert status_changed(lead) is False


# ============================================================
# _lead_to_fields 的 skip_status
# ============================================================

def test_lead_to_fields_writes_status_by_default():
    f = _lead_to_fields(_lead(), "2026-11-07 10:00:00")
    assert f["处理状态"] == "待处理"
    assert f["触达状态"] == "待触达"


def test_lead_to_fields_skips_status_when_asked():
    f = _lead_to_fields(_lead(), "2026-11-07 10:00:00", skip_status=True)
    assert "处理状态" not in f
    assert "触达状态" not in f


def test_lead_to_fields_still_writes_other_fields_when_skipping_status():
    """跳过状态不能连带把线索正文也跳了"""
    f = _lead_to_fields(_lead(), "2026-11-07 10:00:00", skip_status=True)
    assert f["抖音号"] == "abc123"
    assert f["意向等级"] == "高意向"
    assert f["意向分"] == 60
    assert f["发现时间"] == "2026-11-07 10:00:00"


# ============================================================
# 门店归一化
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("小米汽车｜世茂广场店", "世茂广场店"),
    ("小米汽车 | 成都龙湖滨江体验店", "成都龙湖滨江体验店"),
    ("小米汽车 ｜ 机场路店", "机场路店"),
    ("小米汽车丨成都万象城店", "成都万象城店"),
    ("小米汽车｜德阳万达体验店", "德阳万达体验店"),
    ("小米汽车｜泸州万象汇店", "泸州万象汇店"),
    ("小米汽车｜合肥滨湖悦方体验店", "合肥滨湖悦方体验店"),
])
def test_normalize_store(raw, expected):
    assert normalize_store(raw) == expected


def test_normalize_store_same_store_different_writers():
    """同一个门店的两种写法必须归一成同一个名字"""
    a = normalize_store("小米汽车｜成都龙湖滨江体验店")
    b = normalize_store("小米汽车 | 成都龙湖滨江体验店")
    assert a == b


def test_normalize_store_keeps_non_xiaomi_name():
    """非自家品牌的监控名不要瞎改"""
    assert normalize_store("超人说新车") == "超人说新车"


def test_normalize_store_empty():
    assert normalize_store("") == ""


def test_lead_to_fields_includes_store():
    f = _lead_to_fields(_lead(monitor_name="小米汽车｜世茂广场店"), "2026-11-07 10:00:00")
    assert f["门店"] == "世茂广场店"


def test_lead_to_fields_omits_store_when_no_monitor_name():
    f = _lead_to_fields(_lead(monitor_name=""), "2026-11-07 10:00:00")
    assert "门店" not in f
