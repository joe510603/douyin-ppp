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
import json
import re
import shutil
import tempfile
from pathlib import Path
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
    # 与 LEAD_TABLE_FIELDS_V3 同序：发现时间在第一位（飞书主字段恒在最左列）
    {"name": "发现时间", "type": TYPE_DATE, "description": "线索发现日期"},
    {"name": "抖音号", "type": TYPE_TEXT, "description": "抖音平台分配的不变 ID（displayId）"},
    {"name": "昵称", "type": TYPE_TEXT},
    {"name": "意向等级", "type": TYPE_SELECT, "options": [
        {"name": "高意向", "color_index": 0},   # 红
        {"name": "中意向", "color_index": 1},   # 橙
        {"name": "低意向", "color_index": 2},   # 蓝
    ]},
    {"name": "意向分", "type": TYPE_NUMBER, "description": "0-100，规则引擎打分"},
    {"name": "车型", "type": TYPE_TEXT, "description": "客户关注的车型"},
    {"name": "弹幕原文", "type": TYPE_TEXT, "description": "触发这条线索的弹幕"},
    {"name": "判定依据", "type": TYPE_TEXT, "description": "命中的购车信号"},
    {"name": "来源直播间", "type": TYPE_TEXT},
    {"name": "门店", "type": TYPE_TEXT, "description": "归一化后的门店名，用于按门店筛选和分配销售"},
    {"name": "联系方式", "type": TYPE_TEXT, "description": "电话/微信/QQ，从弹幕提取"},
    {"name": "处理状态", "type": TYPE_SELECT, "options": [
        {"name": "待处理", "color_index": 4},
        {"name": "已跟进", "color_index": 0},
        {"name": "已忽略", "color_index": 3},
        # ⚠「已留资」是**用户 2026-11-13 自己在飞书表里加的选项**
        # （销售拿到电话/微信后手动标的状态）。
        # 这里的定义只用于**新建表**；已存在的表不会被改
        # （ensure_fields 只补建缺失字段，从不覆盖已有字段的选项）。
        # 千万别把这个选项删掉 —— 删了销售就没法标「已留资」了。
        {"name": "已留资", "color_index": 1},
    ]},
    {"name": "触达状态", "type": TYPE_SELECT, "options": [
        {"name": "待触达", "color_index": 4},
        {"name": "搜索中", "color_index": 1},
        {"name": "已定位", "color_index": 1},
        {"name": "已留言", "color_index": 2},
        {"name": "失败", "color_index": 3},
        {"name": "已跳过", "color_index": 4},
        # ⚠「已留资」= 用户手动标的终态（拿到电话/微信了）→ 不再留言
        # ⚠「搜不到」= 这次没搜到（可重试，不是终态）
        {"name": "已留资", "color_index": 1},
        {"name": "搜不到", "color_index": 4},
    ]},
]


def _mask(text: str) -> str:
    """脱敏，用于日志"""
    if not text:
        return "(空)"
    return f"***{text[-4:]}"


# ============================================================
# 线索 → 字段映射（HTTP / lark-cli 两种后端共用）
# ============================================================

LEVEL_LABELS = {"high": "高意向", "medium": "中意向", "low": "低意向"}
STATUS_LABELS = {"new": "待处理", "pushed": "待处理", "contacted": "已跟进",
                 "ignored": "已忽略", "converted": "已留资"}
OUTREACH_LABELS = {
    "pending": "待触达", "searching": "搜索中", "found": "已定位",
    "commented": "已留言", "failed": "失败", "skipped": "已跳过",
    # 「搜不到」不是终态（等下轮重试），要跟「已跳过」区分开
    "not_found": "搜不到",
    # 「已留资」= 销售已经拿到电话/微信了 → 终态，别再留言
    "converted": "已留资",
}

# 门店名归一化：配置里同一个门店有多种写法
#   '小米汽车｜世茂广场店' / '小米汽车 | 成都龙湖滨江体验店' / '小米汽车 ｜ 机场路店'
# 分隔符混用了全角竖线、半角竖线、顿号，还夹着空格。
# 归一化成「成都龙湖滨江体验店」这种干净的短名，
# 这样飞书表里的「门店」列才能按同一套名字筛选、分给对应销售。
_STORE_SEP_RE = re.compile(r"[\s|｜丨、/／·・\-—]+")
_STORE_BRAND_RE = re.compile(r"^小米\s*汽车")


def normalize_store(monitor_name: str) -> str:
    """把监控项名称归一化成门店短名

    >>> normalize_store('小米汽车｜世茂广场店')
    '世茂广场店'
    >>> normalize_store('小米汽车 | 成都龙湖滨江体验店')
    '成都龙湖滨江体验店'
    >>> normalize_store('小米汽车 ｜ 机场路店')
    '机场路店'
    """
    if not monitor_name:
        return ""
    name = _STORE_SEP_RE.sub("", (monitor_name or "").strip())
    name = _STORE_BRAND_RE.sub("", name).strip()
    return name or (monitor_name or "").strip()


def status_changed(lead: dict) -> bool:
    """本地状态相对上次同步是否真的变了

    「处理状态」和「触达状态」是**双向**字段：
    - 销售在飞书表里手动改（标「已跟进」/「已忽略」/「已跳过」）
    - 本地运营在网页线索中心里改

    谁后改的谁说了算。如果本地状态和上次同步进飞书的值一样，
    说明本地这次没动过，就**不能**把飞书端的值覆盖回默认值——
    否则销售刚标完「已跟进」，下一次后台同步就把它抹回「待处理」。

    Args:
        lead: 线索字典，需带 bitable_synced_status 和 status / outreach_status

    Returns:
        True 表示本地状态有实质变化，应该写进飞书
    """
    synced = (lead.get("bitable_synced_status") or "").strip()
    if not synced:
        # 从没同步过（新建记录）→ 状态由本地决定
        return True

    cur = STATUS_LABELS.get(lead.get("status") or "", "待处理")
    synced_status, _, synced_outreach = synced.partition("|")
    if cur != (synced_status or "").strip():
        return True

    cur_out = OUTREACH_LABELS.get(lead.get("outreach_status") or "", "待触达")
    return cur_out != (synced_outreach or "").strip()


def build_synced_status(lead: dict) -> str:
    """生成「上次同步进飞书的状态」标记，写回本地用于下次比对"""
    cur = STATUS_LABELS.get(lead.get("status") or "", "待处理")
    cur_out = OUTREACH_LABELS.get(lead.get("outreach_status") or "", "待触达")
    return f"{cur}|{cur_out}"


def _lead_to_fields(lead: dict, found_at: Any, skip_status: bool = False) -> dict:
    """
    把一条线索映射成多维表格的 fields 字典。

    Args:
        lead: 线索字典
        found_at: 发现时间的值。HTTP 后端（v1 API）用毫秒时间戳，
                  lark-cli 后端（v3 API）用 "YYYY-MM-DD HH:mm:ss" 字符串
        skip_status: True 时不写「处理状态」「触达状态」两个字段。

            这两个字段是**双向**的：销售在飞书表里手动改它们（比如标「已跟进」，
            或把「触达状态」改成「已跳过」），本地不该再拿默认值覆盖回去。
            调用方在确认本地状态相对上次同步确实变了时，才传 False。
    """
    import json as _json

    evidence = lead.get("evidence") or ""
    if isinstance(evidence, str) and evidence.startswith("["):
        try:
            items = _json.loads(evidence)
            evidence = " / ".join(str(i) for i in items)
        except (ValueError, TypeError):
            pass

    fields: dict[str, Any] = {
        "抖音号": lead.get("display_id") or "",
        "昵称": lead.get("nickname") or "",
        "意向等级": LEVEL_LABELS.get(lead.get("intent_level") or "", None),
        "意向分": int(lead.get("intent_score") or 0),
        "车型": lead.get("car_model") or "",
        "判定依据": (lead.get("intent_reason") or "")[:200],
        "来源直播间": lead.get("monitor_name") or lead.get("live_room") or "",
        "弹幕原文": (evidence or "")[:1000],
    }
    # 门店：把门店名归一化（去掉「小米汽车」前缀和各种分隔符），
    # 让 14 个门店能按同一套名字筛选、分配给销售。
    store = normalize_store(lead.get("monitor_name") or "")
    if store:
        fields["门店"] = store

    if not skip_status:
        fields["处理状态"] = STATUS_LABELS.get(lead.get("status") or "", "待处理")
        fields["触达状态"] = OUTREACH_LABELS.get(lead.get("outreach_status") or "", "待触达")

    # 联系方式只在真有值时写，空值会让开放平台报错
    contact = lead.get("contact_value") or ""
    if contact:
        fields["联系方式"] = contact
    fields["发现时间"] = found_at

    # 去掉 None 值（单选字段没匹配上时会是 None）
    return {k: v for k, v in fields.items() if v is not None}


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

    def _build_fields(self, lead: dict, is_new: bool = False) -> dict:
        """把一条线索映射成多维表格的 fields 字典

        状态字段只在本地确实变了的时候才写，避免覆盖销售在飞书表里改的值。

        Args:
            lead: 线索字典
            is_new: True 表示这是**新建**记录。
                新建时必须写状态字段——飞书端那两格不能是空的，
                销售看着会以为线索还没处理。
                守卫只适用于「更新已有记录」。
        """
        return _lead_to_fields(
            lead,
            lead.get("_found_ts") or 0,
            skip_status=(not is_new) and (not status_changed(lead)),
        )

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

        records = [{"fields": self._build_fields(lead, is_new=True)} for lead in leads]
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

    async def patch_fields(self, record_id: str, fields: dict) -> tuple[bool, str]:
        """只更新指定字段（部分更新），不动其他列

        用于「补写飞书端为空的字段」这类场景——
        不能用 update_record（PUT 是整体替换，会把没传的列清空）。
        """
        if not self.is_configured or not record_id or not fields:
            return False, "参数缺失"

        url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
               f"/tables/{self.table_id}/records/{record_id}")
        body = {"fields": fields}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.patch(url, headers=await self._headers(), json=body)
                data = resp.json()
        except Exception as e:
            return False, str(e)

        if data.get("code") != 0:
            return False, f"code={data.get('code')} msg={data.get('msg')}"
        return True, ""

    async def _fetch_all_items(self) -> list[dict]:
        """翻页拉全表记录

        为什么必须翻页：`records` 接口单次最多 500 条，不带 page_token
        就只拿得到第一页。表里的线索已经超过这个数了，
        只拉一页 = 去重失效（建重复行）+ 状态回流失效。
        """
        url = (f"{_API_BASE}/bitable/v1/apps/{self.app_token}"
               f"/tables/{self.table_id}/records")
        out: list[dict] = []
        page_token = ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = await self._headers()
                while True:
                    params: dict = {"page_size": 500}
                    if page_token:
                        params["page_token"] = page_token
                    resp = await client.get(url, headers=headers, params=params)
                    data = resp.json()
                    if data.get("code") != 0:
                        break
                    d = data.get("data") or {}
                    out.extend(d.get("items") or [])
                    page_token = d.get("page_token") or ""
                    if not d.get("has_more") or not page_token:
                        break
        except Exception:
            pass
        return out

    async def search_by_douyin_id(self, douyin_id: str) -> Optional[str]:
        """
        按抖音号查已有记录的 record_id（用于去重）。

        飞书筛选接口需要 field_id，比较绕；这里用直接翻页 + 本地比对，
        线索量级（千级）下完全够用。
        """
        if not self.is_configured or not douyin_id:
            return None

        for item in await self._fetch_all_items():
            fields = item.get("fields") or {}
            value = fields.get("抖音号")
            if isinstance(value, list):
                value = "".join(str(v.get("text", "")) if isinstance(v, dict) else str(v) for v in value)
            if value and str(value) == douyin_id:
                return item.get("record_id")
        return None

    async def fetch_status_map(self) -> dict[str, dict]:
        """拉取全表的「抖音号 → 状态」映射

        用来把销售在飞书表里手动改的状态回流到本地。
        """
        out: dict[str, dict] = {}
        if not self.is_configured:
            return out

        for item in await self._fetch_all_items():
            fields = item.get("fields") or {}
            dy = _cell_text(fields.get("抖音号"))
            if not dy:
                continue
            out[dy] = {
                "处理状态": _cell_text(fields.get("处理状态")),
                "触达状态": _cell_text(fields.get("触达状态")),
            }
        return out

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


# ============================================================
# 字段定义（飞书 v3 shortcut JSON 规范）
#
# lark-cli 后端建字段用这份；scripts/bootstrap_bitable.py 也 import 它，
# 避免两处各写一份字段表导致漂移。
# ============================================================

LEAD_TABLE_FIELDS_V3: list[dict] = [
    # 发现时间放第一列：飞书的主字段恒在最左列，而 API 不允许改「哪个字段是主字段」，
    # 所以建表时第一个字段就是主字段（见 scripts/migrate_bitable_layout.py）。
    # 显示格式 yyyy/MM/dd——只要日期，不要时分秒。
    {"name": "发现时间", "type": "datetime", "style": {"format": "yyyy/MM/dd"}},
    {"name": "抖音号", "type": "text", "style": {"type": "plain"},
     "description": "抖音平台分配的不变 ID（displayId），本表的去重键"},
    {"name": "昵称", "type": "text", "style": {"type": "plain"}},
    {"name": "意向等级", "type": "select", "multiple": False, "options": [
        {"name": "高意向", "hue": "Red", "lightness": "Light"},
        {"name": "中意向", "hue": "Orange", "lightness": "Light"},
        {"name": "低意向", "hue": "Blue", "lightness": "Light"},
    ]},
    {"name": "意向分", "type": "number",
     "style": {"type": "plain", "precision": 0, "percentage": False, "thousands_separator": False},
     "description": "0-100，规则引擎打分"},
    {"name": "车型", "type": "text", "style": {"type": "plain"}, "description": "客户关注的车型"},
    {"name": "弹幕原文", "type": "text", "style": {"type": "plain"},
     "description": "触发这条线索的原始弹幕"},
    {"name": "判定依据", "type": "text", "style": {"type": "plain"}, "description": "命中的购车信号"},
    {"name": "来源直播间", "type": "text", "style": {"type": "plain"}},
    {"name": "门店", "type": "text", "style": {"type": "plain"},
     "description": "归一化后的门店名，用于按门店筛选和分配销售"},
    {"name": "联系方式", "type": "text", "style": {"type": "plain"},
     "description": "电话/微信/QQ，从弹幕提取"},
    {"name": "处理状态", "type": "select", "multiple": False, "options": [
        {"name": "待处理", "hue": "Gray", "lightness": "Lighter"},
        {"name": "已跟进", "hue": "Green", "lightness": "Light"},
        {"name": "已忽略", "hue": "Red", "lightness": "Lighter"},
        # ⚠「已留资」是**用户 2026-11-13 自己在飞书表里加的选项**，
        # 销售拿到电话/微信后手动标它。
        # 这里只用于**新建表**；已有的表不会被改
        # （ensure_fields 只补建缺失字段，从不覆盖已有字段的选项）。
        # 别删 —— 删了销售就没法标「已留资」了。
        {"name": "已留资", "hue": "Blue", "lightness": "Lighter"},
    ]},
    {"name": "触达状态", "type": "select", "multiple": False, "options": [
        {"name": "待触达", "hue": "Gray", "lightness": "Lighter"},
        {"name": "搜索中", "hue": "Orange", "lightness": "Light"},
        {"name": "已定位", "hue": "Wathet", "lightness": "Light"},
        {"name": "已留言", "hue": "Green", "lightness": "Light"},
        {"name": "失败", "hue": "Red", "lightness": "Lighter"},
        {"name": "已跳过", "hue": "Gray", "lightness": "Lighter"},
        # ⚠「已留资」= 用户手动标的终态（拿到电话/微信了）→ 不再留言
        # ⚠「搜不到」= 这次没搜到（可重试，不是终态）
        {"name": "已留资", "hue": "Blue", "lightness": "Lighter"},
        {"name": "搜不到", "hue": "Gray", "lightness": "Lighter"},
    ]},
]


def _cell_text(value: Any) -> str:
    """把多维表格单元格的多种返回形态统一成字符串"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.append(_cell_text(item))
        return "".join(parts)
    return str(value)


class LarkCliBitableWriter:
    """通过 lark-cli 写多维表格

    认证完全交给 lark-cli（用户身份，或已绑定的应用），
    token 过期由 lark-cli 自己续期——适合 24h 后台自跑程序。

    适用场景：已经用 lark-cli 授权过，但拿不到企业自建应用的 app_secret。
    """

    def __init__(
        self,
        app_token: str,
        table_id: str,
        identity: str = "user",
        cli_path: str = "",
        timeout: int = 60,
    ):
        self.app_token = (app_token or "").strip()
        self.table_id = (table_id or "").strip()
        self.identity = (identity or "user").strip() or "user"
        self._configured_cli = (cli_path or "").strip()
        self.timeout = timeout
        self._resolved_cli = ""

    # ============================================================
    # 基础配置
    # ============================================================

    @property
    def cli_path(self) -> str:
        """定位 lark-cli 可执行文件

        launchd 注入的 PATH 很短（只有 /usr/bin:/bin 等），
        用户级 npm 全局目录不在里面，所以要显式兜底。
        """
        if self._resolved_cli:
            return self._resolved_cli
        if self._configured_cli and Path(self._configured_cli).exists():
            self._resolved_cli = self._configured_cli
            return self._resolved_cli
        found = shutil.which("lark-cli")
        if found:
            self._resolved_cli = found
            return found
        for cand in (
            Path.home() / ".npm-global/bin/lark-cli",
            Path("/opt/homebrew/bin/lark-cli"),
            Path("/usr/local/bin/lark-cli"),
        ):
            if cand.exists():
                self._resolved_cli = str(cand)
                return found if found else str(cand)
        return ""

    @property
    def is_configured(self) -> bool:
        return bool(self.app_token and self.table_id and self.cli_path)

    # ============================================================
    # CLI 调用
    # ============================================================

    def _run_sync(self, args: list[str], payload: Optional[dict] = None) -> dict:
        import subprocess

        cmd = [self.cli_path, *args]
        tmp: Optional[str] = None
        try:
            if payload is not None:
                # --json @file 只接受 cwd / /tmp / ~/files 下的路径
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", delete=False, encoding="utf-8", dir="/tmp"
                ) as f:
                    json.dump(payload, f, ensure_ascii=False)
                    tmp = f.name
                cmd += ["--json", f"@{tmp}"]
            cmd += ["--as", self.identity, "--format", "json"]

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            out = (proc.stdout or "").strip()
            if not out:
                raise RuntimeError(f"lark-cli 无输出: {(proc.stderr or '')[:300]}")
            data = json.loads(out)
            if not data.get("ok", True):
                err = data.get("error") or data
                raise RuntimeError(json.dumps(err, ensure_ascii=False)[:400])
            return data
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    async def _run(self, args: list[str], payload: Optional[dict] = None) -> dict:
        # subprocess 是阻塞的，放线程里跑，别卡住事件循环
        return await asyncio.to_thread(self._run_sync, args, payload)

    # ============================================================
    # 字段
    # ============================================================

    async def list_fields(self) -> list[dict]:
        data = await self._run([
            "base", "+field-list",
            "--base-token", self.app_token,
            "--table-id", self.table_id,
        ])
        return (data.get("data") or {}).get("fields") or []

    async def ensure_fields(self, spec: Optional[list[dict]] = None) -> list[str]:
        """按 spec 补建缺失字段（幂等）"""
        if not self.is_configured:
            return []
        spec = spec or LEAD_TABLE_FIELDS_V3
        existing = await self.list_fields()
        existing_names = {f.get("name") for f in existing}
        created: list[str] = []

        for field in spec:
            name = field["name"]
            if name in existing_names:
                continue
            try:
                await self._run(
                    ["base", "+field-create", "--base-token", self.app_token,
                     "--table-id", self.table_id],
                    field,
                )
                created.append(name)
                log.info(f"✅ 已创建字段: {name}")
            except Exception as e:
                log.error(f"创建字段「{name}」失败: {e}")
        return created

    # ============================================================
    # 记录
    # ============================================================

    def _build_fields(self, lead: dict, is_new: bool = False) -> dict:
        """v3 API 的日期字段接受 "YYYY-MM-DD HH:mm:ss" 字符串

        状态字段只在本地确实变了的时候才写，避免覆盖销售在飞书表里改的值。

        Args:
            lead: 线索字典
            is_new: True 表示这是**新建**记录——新建时必须写状态字段，
                飞书端那两格不能是空的。守卫只适用于「更新已有记录」。
        """
        found_at = lead.get("_found_at") or lead.get("created_at") or ""
        return _lead_to_fields(
            lead,
            found_at,
            skip_status=(not is_new) and (not status_changed(lead)),
        )

    async def create_records(self, leads: list[dict]) -> tuple[list[str], str]:
        if not leads:
            return [], ""
        records = [{"fields": self._build_fields(lead, is_new=True)} for lead in leads]
        try:
            data = await self._run(
                ["base", "+record-batch-create", "--base-token", self.app_token,
                 "--table-id", self.table_id],
                {"create_records": records},
            )
        except Exception as e:
            log.error(f"批量写入多维表格失败: {e}")
            return [], str(e)

        d = data.get("data") or {}
        ids = [str(x) for x in (d.get("record_id_list") or [])]
        return ids, ""

    async def update_record(self, record_id: str, lead: dict) -> tuple[bool, str]:
        if not record_id:
            return False, "参数缺失"
        payload = self._build_fields(lead)
        try:
            await self._run(
                ["base", "+record-upsert", "--base-token", self.app_token,
                 "--table-id", self.table_id, "--record-id", record_id],
                payload,
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    async def patch_fields(self, record_id: str, fields: dict) -> tuple[bool, str]:
        """只更新指定字段（部分更新），不动其他列

        用于「补写飞书端为空的字段」这类场景。
        """
        if not record_id or not fields:
            return False, "参数缺失"
        try:
            await self._run(
                ["base", "+record-upsert", "--base-token", self.app_token,
                 "--table-id", self.table_id, "--record-id", record_id],
                fields,
            )
            return True, ""
        except Exception as e:
            return False, str(e)

    # record-list 单次最多 200 条（json 格式）。
    # 表里的线索早就超过这个数了（2026-11-12 实测 386 条），
    # 所以「拉一页就当全表」这个假设已经不成立。
    _PAGE_SIZE = 200

    async def _record_list_paged(
        self,
        field_ids: Optional[list[str]] = None,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> tuple[list[str], list[str], list[list]]:
        """翻页拉全表记录，返回 (字段名, record_id 列表, 行数据)

        为什么必须翻页
        --------------
        以前只拉第一页（200 条）就当「全表」用了，后果有两处，
        而且**都是静默失效**——不报错，只是有一半数据看不见：

        1. 去重只看得到前 200 条 → 后一半的抖音号查不到 record_id
           → **建出重复行**（2026-11-12 实测表里已经有 6 条重复）
        2. 回拉销售改的「处理状态」同样只看得到前 200 条
           → 后一半线索的状态同步不回来
           （多机共用一张飞书表、靠回拉互相同步，这个断了就等于两台机器各跑各的）

        注意 `has_more` 才是权威的翻页依据，不能靠「这页返回条数 < limit」来猜：
        服务端在某些条件下会少给几行，猜错了就会漏记录。
        """
        names: list[str] = []
        ids: list[str] = []
        rows: list[list] = []
        offset = 0
        for _ in range(max_pages):
            args = [
                "base", "+record-list",
                "--base-token", self.app_token,
                "--table-id", self.table_id,
                "--limit", str(page_size),
                "--offset", str(offset),
            ]
            for f in (field_ids or []):
                args += ["--field-id", f]
            try:
                data = await self._run(args)
            except Exception as e:
                log.warning(f"[多维表格] 读取记录失败（offset={offset}）: {e}")
                break
            d = data.get("data") or {}
            if not names:
                names = d.get("fields") or []
            batch_ids = d.get("record_id_list") or []
            batch_rows = d.get("data") or []
            ids.extend(batch_ids)
            rows.extend(batch_rows)
            if not d.get("has_more") or not batch_rows:
                break
            offset += len(batch_rows)
        return names, ids, rows

    async def _fetch_douyin_map(self) -> dict[str, str]:
        """拉一次全表，建立 抖音号 → record_id 映射（用于去重）

        ⚠ 必须翻页拉全表：只拉第一页的话，表里后一半的抖音号查不到
        record_id → 会建出重复行（实测已经出现 6 条重复）。
        """
        names, ids, rows = await self._record_list_paged(["抖音号"])
        try:
            di = names.index("抖音号")
        except ValueError:
            return {}

        out: dict[str, str] = {}
        for rid, row in zip(ids, rows):
            if di >= len(row):
                continue
            value = _cell_text(row[di])
            if value:
                out[value] = str(rid)
        return out

    async def search_by_douyin_id(self, douyin_id: str) -> Optional[str]:
        """按抖音号查已有记录的 record_id"""
        if not douyin_id:
            return None
        return (await self._fetch_douyin_map()).get(douyin_id)

    async def fetch_status_map(self) -> dict[str, dict]:
        """拉取全表的「抖音号 → 状态」映射

        用来把销售在飞书表里手动改的状态回流到本地。
        只读 3 个字段（抖音号 / 处理状态 / 触达状态），减少传输量。

        ⚠ 必须翻页拉全表：只拉第一页的话，表里后一半的线索状态永远
        拉不回来——多机共用一张飞书表就靠这个对齐，拉不全等于两台机器各跑各的。

        Returns:
            {抖音号: {"处理状态": "已跟进", "触达状态": "已留言"}}
        """
        out: dict[str, dict] = {}
        names, _ids, rows = await self._record_list_paged(
            ["抖音号", "处理状态", "触达状态"])
        try:
            i_dy = names.index("抖音号")
        except ValueError:
            return out
        i_st = names.index("处理状态") if "处理状态" in names else -1
        i_os = names.index("触达状态") if "触达状态" in names else -1

        for row in rows:
            if i_dy >= len(row):
                continue
            dy = _cell_text(row[i_dy])
            if not dy:
                continue
            out[dy] = {
                "处理状态": _cell_text(row[i_st]) if 0 <= i_st < len(row) else "",
                "触达状态": _cell_text(row[i_os]) if 0 <= i_os < len(row) else "",
            }
        return out

    # ============================================================
    # 对外入口
    # ============================================================

    async def sync_leads(self, leads: list[dict], update_existing: bool = True) -> dict:
        """同步线索到多维表格（按抖音号去重）

        性能要点：
        - 全表映射只拉一次（而且只在有线索缺 record_id 时才拉）
        - 更新走 record-batch-update（逐条不同值，单次上限 200）
        - 新建走 record-batch-create（单次上限 200）
        """
        result = {"created": 0, "updated": 0, "failed": 0, "error": "", "record_ids": {}}
        if not self.is_configured:
            result["error"] = "多维表格未配置或找不到 lark-cli"
            return result
        if not leads:
            return result

        await self.ensure_fields()

        # 只有在有线索缺 record_id 时才拉一次全表映射
        by_dy: dict[str, str] = {}
        if any(not lead.get("_bitable_record_id") for lead in leads):
            by_dy = await self._fetch_douyin_map()

        to_update: list[tuple[str, dict]] = []
        to_create: list[tuple[dict, dict]] = []   # (lead, fields)

        for lead in leads:
            key = lead.get("lead_key") or lead.get("display_id")
            record_id = lead.get("_bitable_record_id") or by_dy.get(lead.get("display_id") or "")
            if record_id and update_existing:
                # 更新已有记录：状态字段套守卫，不覆盖销售改过的值
                fields = self._build_fields(lead)
                to_update.append((record_id, fields))
                result["record_ids"][key] = record_id
            else:
                # 新建记录：必须写状态字段，飞书端那两格不能是空的
                to_create.append((lead, self._build_fields(lead, is_new=True)))

        # ---- 批量更新 ----
        for i in range(0, len(to_update), 200):
            chunk = to_update[i:i + 200]
            payload = {"update_records": {rid: fields for rid, fields in chunk}}
            try:
                await self._run(
                    ["base", "+record-batch-update", "--base-token", self.app_token,
                     "--table-id", self.table_id],
                    payload,
                )
                result["updated"] += len(chunk)
            except Exception as e:
                log.error(f"[多维表格] 批量更新失败: {e}")
                result["failed"] += len(chunk)
                result["error"] = str(e)

        # ---- 批量新建 ----
        for i in range(0, len(to_create), 200):
            chunk = to_create[i:i + 200]
            payload = {"create_records": [{"fields": fields} for _lead, fields in chunk]}
            try:
                data = await self._run(
                    ["base", "+record-batch-create", "--base-token", self.app_token,
                     "--table-id", self.table_id],
                    payload,
                )
                ids = [str(x) for x in ((data.get("data") or {}).get("record_id_list") or [])]
                result["created"] += len(chunk)
                for idx, (lead, _fields) in enumerate(chunk):
                    key = lead.get("lead_key") or lead.get("display_id")
                    if idx < len(ids):
                        result["record_ids"][key] = ids[idx]
                        # 立刻把新建的映射回灌：同一批里若有两个线索同抖音号，
                        # 后一个不能再建一条
                        dy = lead.get("display_id") or ""
                        if dy:
                            by_dy[dy] = ids[idx]
            except Exception as e:
                log.error(f"[多维表格] 批量新建失败: {e}")
                result["failed"] += len(chunk)
                result["error"] = str(e)

        return result


def get_bitable_writer(config=None) -> BitableWriter:
    """按配置构造写入器

    三种后端：
    - ``http``：企业自建应用 app_id/app_secret（换 tenant_access_token）
      或用户 access_token，直连开放平台
    - ``lark_cli``：交给 lark-cli 写，认证与 token 续期都由 lark-cli 负责
    - ``auto``（默认）：有 app 凭据走 http，否则有 lark-cli 就走 lark_cli
    """
    if config is None:
        from ..config import get_config
        config = get_config()
    n = config.notify

    backend = (n.bitable_backend or "auto").strip().lower()
    if backend == "auto":
        if n.bitable_app_id and n.bitable_app_secret:
            backend = "http"
        elif n.bitable_user_token:
            backend = "http"
        else:
            backend = "lark_cli"

    if backend == "lark_cli":
        return LarkCliBitableWriter(
            app_token=n.bitable_app_token,
            table_id=n.bitable_table_id,
            identity=n.bitable_lark_cli_identity,
            cli_path=n.bitable_lark_cli_path,
        )

    return BitableWriter(
        app_id=n.bitable_app_id,
        app_secret=n.bitable_app_secret,
        app_token=n.bitable_app_token,
        table_id=n.bitable_table_id,
        user_token=n.bitable_user_token,
    )
