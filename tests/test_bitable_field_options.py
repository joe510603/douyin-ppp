"""飞书表的字段/选项**只能补，不能改**（2026-11-13 用户明确要求）

用户原话：「飞书的触达状态字段，我新加了 已留资。你记录一下哈，**别给我改了**」

背景
----
「已留资」是销售**自己在飞书表里手动加的选项**——
拿到电话/微信后他就把状态改成「已留资」，系统要认这个值。

所以代码里绝对不能有「改已有字段定义」的动作：
- ``+field-update`` 是**完整 PUT 语义**（要读-改-写整个字段定义），
  一旦调用就可能把销售自己加的选项/配置冲掉；
- ``ensure_fields`` 只能**补建缺失的字段**，绝不碰已有字段。

这两条都要用测试钉住，否则以后有人「顺手优化一下」就会出事。
"""
from __future__ import annotations

import pytest

from src.notify.bitable import (
    LEAD_TABLE_FIELDS,
    LEAD_TABLE_FIELDS_V3,
    LarkCliBitableWriter,
)


# ============================================================
# 1. 新建表时，「已留资」「搜不到」要在选项里
# ============================================================

def _options(spec: list[dict], field: str) -> list[str]:
    for f in spec:
        if f["name"] == field:
            return [o["name"] for o in f.get("options") or []]
    return []


def test_触达状态有已留资选项():
    """销售要能在下拉里选「已留资」——新建表时必须带上"""
    assert "已留资" in _options(LEAD_TABLE_FIELDS_V3, "触达状态")
    assert "已留资" in _options(LEAD_TABLE_FIELDS, "触达状态")


def test_触达状态有搜不到选项():
    """「搜不到」是可重试状态，跟「已跳过」要区分开"""
    assert "搜不到" in _options(LEAD_TABLE_FIELDS_V3, "触达状态")
    assert "已跳过" in _options(LEAD_TABLE_FIELDS_V3, "触达状态")


def test_处理状态也有已留资():
    assert "已留资" in _options(LEAD_TABLE_FIELDS_V3, "处理状态")
    assert "已留资" in _options(LEAD_TABLE_FIELDS, "处理状态")


def test_选项不重复():
    """同名选项建两次会出现两个一模一样的下拉项（销售会懵）"""
    for spec in (LEAD_TABLE_FIELDS, LEAD_TABLE_FIELDS_V3):
        for field in ("处理状态", "触达状态", "意向等级"):
            names = _options(spec, field)
            assert len(names) == len(set(names)), f"{field} 有重复选项：{names}"


# ============================================================
# 2. ensure_fields 绝不碰已有字段
# ============================================================

class _FakeWriter:
    """鸭子类型桩：只测 ensure_fields 的行为，不碰网络

    直接调 `LarkCliBitableWriter.ensure_fields(fake)`，
    避免继承基类那一堆初始化（那个还要解析 lark-cli 路径）。
    """

    def __init__(self, existing: list[dict]):
        self._existing = existing
        self.created: list[str] = []
        self.updated: list[str] = []
        self.app_token = "bascnFake"
        self.table_id = "tblFake"
        self.identity = "user"

    @property
    def is_configured(self) -> bool:
        return True

    def list_fields(self):
        async def _l():
            return self._existing
        return _l()

    async def _run(self, args, payload=None):
        # 只允许「建字段」，一旦出现改字段的动作就直接炸
        assert "+field-create" in args, f"不允许改字段定义：{args}"
        name = ""
        if isinstance(payload, dict):
            name = payload.get("name") or payload.get("field_name") or ""
        self.created.append(name)
        return {}


def _ensure(fake) -> None:
    import asyncio
    asyncio.run(LarkCliBitableWriter.ensure_fields(fake))


def test_ensure_fields不碰已有字段():
    """销售自己加的选项（已留资）必须原样保留"""
    existing = [{
        "name": "触达状态",
        "type": "select",
        "options": [{"name": "待触达"}, {"name": "已留言"}, {"name": "已留资"}],
    }]
    w = _FakeWriter(existing)
    _ensure(w)
    assert "触达状态" not in w.created, "已有字段不该被重建（销售加的选项会丢）"
    assert w.updated == [], "不该有任何改字段的动作"


def test_ensure_fields只补缺失的字段():
    existing = [{"name": "触达状态", "type": "select"}]
    w = _FakeWriter(existing)
    _ensure(w)
    assert "触达状态" not in w.created
    assert "抖音号" in w.created, "缺失的字段应该补建"


def test_代码里没有field_update调用():
    """field-update 是完整 PUT，会把销售自己加的选项冲掉——不许出现"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "+field-update" not in text, f"{path} 里出现了 field-update"
