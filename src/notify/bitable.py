"""飞书多维表格（Bitable）写入器

与群机器人 webhook 并行的一条通道：群机器人负责「即时叫醒销售」，
多维表格负责「沉淀可跟进的线索清单」。

认证方式：企业自建应用 app_id + app_secret → tenant_access_token
（与 autoengine-monitor 项目的 feishu_bitable.py 同一套模式）

幂等策略：
- 字段：首次写入时按 spec 自动补建缺失字段，已存在则跳过
- 记录：按 lead_key 记录已创建的 record_id，后续走 update 而非重复 create
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..utils.logger import get_logger

log = get_logger("feishu_bitable")

# 开放平台地址
_API_BASE = "https://open.feishu.cn/open-apis"

# ============================================================
# 字段类型（开放平台 v1 数字枚举）
# ============================================================

TYPE_TEXT = 1        # 多行文本
TYPE_NUMBER = 2      # 数字
TYPE_SELECT = 3      # 单选
TYPE_MULTI_SELECT = 4  # 多选
TYPE_DATE = 5        # 日期
TYPE_CHECKBOX = 7    # 复选框
TYPE_PHONE = 13      # 电话号码
TYPE_HYPERLINK = 15  # 超链接

# ============================================================
# 线索表字段设计
# 按销售跟进的实际顺序排列：是谁 → 有多想买 → 怎么联系 → 从哪来 → 怎么跟进
# ============================================================

LEAD_TABLE_FIELDS: list[dict] = [
    {"name": "抖音号", "type": TYPE_TEXT, "description": "抖音平台分配的不变 ID（displayId）"},
    {"name": "昵称", "type": TYPE_TEXT},
    {"name": "意向等级", "type": TYPE_SELECT, "options": [
        {"name": "高意向", "color_index": 0},   # 红
        {"name": "中意向", "color_index": 1},   # 橙
        {"name": "低意向", "color_index": 2},   # 蓝
    ]},
    {"name": "意向分", "type": TYPE_NUMBER, "description": "0-100，规则引擎打分"},
    {"name": "车型", "type": TYPE_TEXT, "description": "客户关注的车型"},
    {"name": "联系方式", "type": TYPE_TEXT, "description": "电话/微信/QQ，从弹幕提取"},
    {"name": "判定依据", "type": TYPE_TEXT, "description": "命中的购车信号"},
    {"name": "来源直播间", "type": TYPE_TEXT},
    {"name": "弹幕原文", "type": TYPE_TEXT, "description": "触发这条线索的弹幕"},
    {"name": "处理状态", "type": TYPE_SELECT, "options": [
        {"name": "待处理", "color_index": 4},
        {"name": "已跟进", "color_index": 0},
        {"name": "已忽略", "color_index": 3},
    ]},
    {"name": "触达状态", "type": TYPE_SELECT, "options": [
        {"name": "待触达", "color_index": 4},
        {"name": "搜索中", "color_index": 1},
        {"name": "已定位", "color_index": 1},
        {"name": "已留言", "color_index": 2},
        {"name": "失败", "color_index": 3},
    ]},
    {"name": "发现时间", "type": TYPE_DATE},
]


def _mask(text: str) -> str:
    """脱敏，用于日志"""
    if not text:
        return "(空)"
    return f"***{text[-4:]}"


class BitableWriter:
    """飞书多维表格写入器"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        timeout: int = 20,
        user_token: str = "",
    ):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.app_token = (app_token or "").strip()
        self.table_id = (table_id or "").strip()
        self.user_token = (user_token or "").strip()
        self.timeout = timeout
        self._token: str = ""
        self._token_expire_at: float = 0.0

    # ============================================================
    # 基础配置
    # ============================================================

    @property
    def has_app_credentials(self) -> bool:
        """是否配了企业自建应用凭据（推荐通道）"""
        return bool(self.app_id and self.app_secret)

    @property
    def is_configured(self) -> bool:
        """表标识齐全 + 至少一种可用凭据"""
        if not (self.app_token and self.table_id):
            return False
        return self.has_app_credentials or bool(self.user_token)

    async def _get_token(self) -> str:
        """取 access_token（有效期 2 小时，缓存复用）

        两条通道：
        1. **企业自建应用**（推荐）——app_id/app_secret 换 tenant_access_token，
           后台自跑程序用这个，不会因为用户授权过期而中断
        2. **用户 access_token**（备选）——适合临时排查，但会过期，
           过期后需要重新授权
        """
        import time

        # 没有应用凭据时，走用户 token 通道
        if not self.has_app_credentials:
            return self.user_token

        if self._token and time.time() < self._token_expire_at - 60:
            return self._token

        url = f"{_API_BASE}/auth/v3/tenant_access_token/internal"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                data = resp.json()
        except Exception as e:
            log.error(f"获取 tenant_access_token 失败: {e}")
            raise

        if data.get("code") != 0:
            raise RuntimeError(f"飞书鉴权失败: code={data.get('code')} msg={data.get('msg')}")

        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + data.get("expire", 7200)
        return self._token

    async def _headers(self) -> dict:
        token = await self._get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ============================================================
    # 字段
    # ============================================================

    async def list_fields(self) -> list[dict]:
        """列出表现有字段"""
        url = f"{_API_BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=await self._headers())
                data = resp.json()
        except Exception as e:
            log.error(f"读取多维表格字段失败: {e}")
            return []

        if data.get("code") != 0:
            log.error(f"读取字段失败: {data.get('msg')}")
            return []
        return (data.get("data") or {}).get("items") or []

    async def ensure_fields(self, spec: Optional[list[dict]] = None) -> list[str]:
        """
        按 spec 补建缺失字段（幂等）。

        Returns:
            本次新建的字段名列表
        """
        if not self.is_configured:
            return []

        spec = spec or LEAD_TABLE_FIELDS
        existing = await self.list_fields()
        existing_names = {f.get("field_name") for f in existing}
        created: list[str] = []

        for field in spec:
            name = field["name"]
            if name in existing_names:
                continue
            body: dict[str, Any] = {
                "field_name": name,
                "type": field["type"],
            }
            if field.get("description"):
                body["description"] = field["description"] if isinstance(field["description"], dict) else {
                    "disable_sync": True,
                    "text": field["description"],
                }
            if field["type"] == TYPE_SELECT:
                body["property"] = {"options": field.get("options") or []}
            elif field["type"] == TYPE_DATE:
                body["property"] = {"date_formatter": "yyyy/MM/dd HH:mm"}

            url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
                   f"/tables/{self.table_id}/fields")
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, headers=await self._headers(), json=body)
                    data = resp.json()
            except Exception as e:
                log.error(f"创建字段「{name}」异常: {e}")
                continue

            if data.get("code") == 0:
                created.append(name)
                log.info(f"✅ 已创建字段: {name}")
            else:
                log.error(f"创建字段「{name}」失败: {data.get('msg')}")

        if created:
            log.info(f"多维表格字段补建完成，新建 {len(created)} 个: {created}")
        return created

    # ============================================================
    # 记录
    # ============================================================

    def _build_fields(self, lead: dict) -> dict:
        """把一条线索映射成多维表格的 fields 字典"""
        level_map = {"high": "高意向", "medium": "中意向", "low": "低意向"}
        status_map = {"new": "待处理", "contacted": "已跟进", "ignored": "已忽略"}
        outreach_map = {
            "pending": "待触达", "searching": "搜索中", "found": "已定位",
            "commented": "已留言", "failed": "失败", "skipped": "已跳过",
        }

        evidence = lead.get("evidence") or ""
        if isinstance(evidence, str) and evidence.startswith("["):
            try:
                import json
                items = json.loads(evidence)
                evidence = " / ".join(str(i) for i in items)
            except (ValueError, TypeError):
                pass

        fields: dict[str, Any] = {
            "抖音号": lead.get("display_id") or "",
            "昵称": lead.get("nickname") or "",
            "意向等级": level_map.get(lead.get("intent_level") or "", None),
            "意向分": int(lead.get("intent_score") or 0),
            "车型": lead.get("car_model") or "",
            "判定依据": (lead.get("intent_reason") or "")[:200],
            "来源直播间": lead.get("monitor_name") or lead.get("live_room") or "",
            "弹幕原文": (evidence or "")[:500],
            "处理状态": status_map.get(lead.get("status") or "", "待处理"),
            "触达状态": outreach_map.get(lead.get("outreach_status") or "", "待触达"),
        }
        # 电话号码字段只在真有联系方式时写，空值会让开放平台报错
        contact = lead.get("contact_value") or ""
        if contact:
            fields["联系方式"] = contact
        # 发现时间用毫秒时间戳
        fields["发现时间"] = lead.get("_found_ts") or 0

        # 去掉 None 值（单选字段没匹配上时会是 None）
        return {k: v for k, v in fields.items() if v is not None}

    async def create_records(self, leads: list[dict]) -> tuple[list[str], str]:
        """
        批量创建记录。

        Returns:
            (新建成功的 record_id 列表, 错误信息)
        """
        if not self.is_configured:
            return [], "多维表格未配置"
        if not leads:
            return [], "没有待写入的线索"

        records = [{"fields": self._build_fields(lead)} for lead in leads]
        url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
               f"/tables/{self.table_id}/records/batch_create")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers=await self._headers(),
                    json={"records": records},
                )
                data = resp.json()
        except Exception as e:
            log.error(f"批量写入多维表格失败: {e}")
            return [], str(e)

        if data.get("code") != 0:
            log.error(f"批量写入失败: {data.get('msg')}")
            return [], f"code={data.get('code')} msg={data.get('msg')}"

        return (data.get("data") or {}).get("records") or [], ""

    async def update_record(self, record_id: str, lead: dict) -> tuple[bool, str]:
        """更新一条已有记录"""
        if not self.is_configured or not record_id:
            return False, "参数缺失"

        url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
               f"/tables/{self.table_id}/records/{record_id}")
        body = {"fields": self._build_fields(lead)}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.put(url, headers=await self._headers(), json=body)
                data = resp.json()
        except Exception as e:
            return False, str(e)

        if data.get("code") != 0:
            return False, f"code={data.get('code')} msg={data.get('msg')}"
        return True, ""

    async def search_by_douyin_id(self, douyin_id: str) -> Optional[str]:
        """
        按抖音号查已有记录的 record_id（用于去重）。

        飞书筛选接口需要 field_id，比较绕；这里用直接翻页 + 本地比对，
        线索量级（千级）下完全够用。
        """
        if not self.is_configured or not douyin_id:
            return None

        url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
               f"/tables/{self.table_id}/records")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    url,
                    headers=await self._headers(),
                    params={"page_size": 500},
                )
                data = resp.json()
        except Exception:
            return None

        if data.get("code") != 0:
            return None

        for item in (data.get("data") or {}).get("items") or []:
            fields = item.get("fields") or {}
            value = fields.get("抖音号")
            if isinstance(value, list):
                value = "".join(str(v.get("text", "")) if isinstance(v, dict) else str(v) for v in value)
            if value and str(value) == douyin_id:
                return item.get("record_id")
        return None

    # ============================================================
    # 对外入口
    # ============================================================

    async def sync_leads(self, leads: list[dict], update_existing: bool = True) -> dict:
        """
        同步线索到多维表格。

        Args:
            leads: 线索字典列表
            update_existing: True 时已存在的线索改为 update 而不是重复 create

        Returns:
            {"created": n, "updated": n, "failed": n, "error": str}
        """
        result = {"created": 0, "updated": 0, "failed": 0, "error": "", "record_ids": {}}
        if not self.is_configured:
            result["error"] = "多维表格未配置"
            return result

        await self.ensure_fields()

        if not update_existing:
            records, err = await self.create_records(leads)
            if err:
                result["error"] = err
                result["failed"] = len(leads)
                return result
            result["created"] = len(records)
            for rec, lead in zip(records, leads):
                rid = rec.get("record_id") if isinstance(rec, dict) else None
                if rid:
                    result["record_ids"][lead.get("lead_key") or lead.get("display_id")] = rid
            return result

        # 逐条：有 record_id 就更新，没有就新建
        for lead in leads:
            key = lead.get("lead_key") or lead.get("display_id")
            record_id = lead.get("_bitable_record_id")
            if not record_id:
                record_id = await self.search_by_douyin_id(lead.get("display_id") or "")
            if record_id:
                ok, err = await self.update_record(record_id, lead)
                if ok:
                    result["updated"] += 1
                    result["record_ids"][key] = record_id
                    continue
                log.warning(f"更新记录失败，改为新建: {err}")
            records, err = await self.create_records([lead])
            if err:
                result["failed"] += 1
                result["error"] = err
                continue
            if records:
                rec = records[0]
                rid = rec.get("record_id") if isinstance(rec, dict) else None
                result["created"] += 1
                if rid:
                    result["record_ids"][key] = rid
            else:
                result["failed"] += 1

        return result


def get_bitable_writer(config=None) -> BitableWriter:
    """按配置构造写入器"""
    if config is None:
        from ..config import get_config
        config = get_config()
    n = config.notify
    return BitableWriter(
        app_id=n.bitable_app_id,
        app_secret=n.bitable_app_secret,
        app_token=n.bitable_app_token,
        table_id=n.bitable_table_id,
        user_token=n.bitable_user_token,
    )
