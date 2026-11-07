"""购车意向分析器 — 识别直播间里有真实购车意向的用户

流程：
1. 从 live_comments 拉取时间窗内的弹幕，按用户聚合
2. 规则预筛：只保留「含购车关键词」或「含联系方式」的用户（省 token，且几乎不丢召回）
3. 规则提取联系方式（本地，零成本）
4. 批量交给 LLM 判定意向等级 + 车型 + 证据
5. 合并结果 → 写入 leads 表

判定「宁漏勿误」与「宁多勿少」之间取平衡：LLM 负责语义判断，
规则只做廉价的噪声过滤，不代替 LLM 下结论。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_config
from ..models.lead import Lead, IntentLevel, INTENT_LEVEL_ORDER
from ..storage.db_storage import get_db
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger
from .contact_extractor import extract_contacts
from .prompts import INTENT_SYSTEM_PROMPT, build_intent_prompt, parse_json_array

log = get_logger("intent_analyzer")

# ============================================================
# 预筛关键词
# ============================================================

# 购车相关关键词——命中任意一个才值得送 LLM
_CAR_KEYWORDS = (
    "车", "款", "价格", "价钱", "多少钱", "落地", "优惠", "折扣", "降价", "便宜",
    "提车", "订车", "下单", "现车", "库存", "试驾", "到店", "门店", "地址", "哪里",
    "配置", "续航", "电池", "充电", "油耗", "空间", "座椅", "后备箱", "外观", "内饰",
    "智驾", "辅助驾驶", "预算", "置换", "贷款", "分期", "保险", "上牌", "牌照",
    "喜欢", "想买", "入手", "考虑", "推荐", "怎么样", "值得", "对比", "哪个好",
    "电话", "微信", "加v", "加V", "vx", "VX", "联系", "留个",
)

# 无意义的弹幕（纯表情/跟风），直接丢
_NOISE_PATTERNS = (
    re.compile(r"^[6\s哈呵嘿嘻嘿嘿嘻]+$"),
    re.compile(r"^[\U0001F300-\U0001FAFF\s]+$"),   # emoji
    re.compile(r"^[前后左右上下]+$"),
)

_VALID_LEVELS = {l.value for l in IntentLevel}

# 昵称归一化：去掉空白与各种分隔符，用于比对
_NAME_NOISE_RE = re.compile(r"[\s|｜\-_·•,，.。、:：]+")


def _normalize_name(name: str) -> str:
    """昵称归一化，便于比对"""
    return _NAME_NOISE_RE.sub("", (name or "").lower())


def _build_self_names(config) -> set[str]:
    """
    收集「自己家」账号的昵称集合。

    直播弹幕里也包含主播/门店账号自己发的消息（自动回复、优惠播报等），
    这些内容天然命中购车关键词，若不过滤会被当成高意向客户。
    监控配置里的账号名与弹幕里的昵称一致，用昵称比对排除。
    """
    names = set()
    for m in getattr(config, "monitors", []) or []:
        n = getattr(m, "name", "")
        if n:
            names.add(_normalize_name(n))
    return names


def _is_noise(text: str) -> bool:
    """判断是否是无意义弹幕"""
    t = text.strip()
    if not t:
        return True
    for pat in _NOISE_PATTERNS:
        if pat.match(t):
            return True
    return False


def _has_car_keyword(texts: list[str]) -> bool:
    """是否命中购车关键词"""
    for t in texts:
        low = t.lower()
        for kw in _CAR_KEYWORDS:
            if kw in low:
                return True
    return False


def _normalize_level(raw: str) -> str:
    """把 LLM 返回的等级规整到合法值"""
    level = (raw or "").strip().lower()
    if level in _VALID_LEVELS:
        return level
    # 容错：模型可能返回 "HIGH"、"高"、"high_intent" 之类
    if "high" in level or "高" in level:
        return IntentLevel.HIGH.value
    if "medium" in level or "mid" in level or "中" in level:
        return IntentLevel.MEDIUM.value
    if "low" in level or "低" in level:
        return IntentLevel.LOW.value
    return IntentLevel.NONE.value


class IntentAnalyzer:
    """购车意向分析器"""

    def __init__(self):
        self.config = get_config()

    @property
    def lead_config(self):
        return self.config.lead

    async def scan(
        self,
        lookback_hours: Optional[int] = None,
        limit_users: int = 300,
        min_level: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """
        扫描并识别购车意向线索。

        Args:
            lookback_hours: 回溯时间窗（小时），默认取配置
            limit_users: 本轮最多分析多少个用户
            min_level: 最低意向等级，低于此级别不写入线索池
            force: True 时忽略「已分析过」的去重，重新分析

        Returns:
            {"scanned":用户总数, "candidates":送 LLM 的用户数,
             "leads":写入线索池数, "new_leads":新增数, "level_counts":{...}}
        """
        db = get_db()
        if not db or not db._db:
            return {"error": "数据库未就绪", "scanned": 0, "candidates": 0, "leads": 0}

        if min_level is None:
            min_level = self.lead_config.min_intent_level
        if lookback_hours is None:
            lookback_hours = self.lead_config.lookback_hours

        since = datetime.now() - timedelta(hours=lookback_hours)

        # 1. 拉取并按用户聚合
        groups = await db.query_user_activity(
            since=since,
            limit_users=limit_users,
            min_messages=1,
        )
        scanned = len(groups)
        if not groups:
            log.info("意向扫描：时间窗内无弹幕")
            return {"scanned": 0, "candidates": 0, "leads": 0, "new_leads": 0, "level_counts": {}}

        # 2. 规则预筛 + 联系方式提取
        self_names = _build_self_names(self.config)
        candidates = []
        pre_extracted: dict[str, object] = {}
        skipped_self = 0
        for g in groups:
            messages = [m for m in g["messages"] if not _is_noise(m)]
            if not messages:
                continue
            # 排除自己家（主播/门店）账号的消息
            if self_names and _normalize_name(g["nickname"]) in self_names:
                skipped_self += 1
                continue
            contact = extract_contacts(messages)
            if not _has_car_keyword(messages) and not contact.has_contact:
                continue
            key = g["ukey"]
            if not key:
                continue
            candidates.append({
                "user_id": g["user_id"],
                "display_id": g["display_id"],
                "nickname": g["nickname"],
                "messages": messages,
                "_key": key,
            })
            pre_extracted[key] = contact

        # 3. 已分析过的用户直接跳过（除非 force）
        if not force and candidates:
            existing = await db.get_existing_lead_keys([c["_key"] for c in candidates])
            if existing:
                before = len(candidates)
                candidates = [c for c in candidates if c["_key"] not in existing]
                log.info(f"意向扫描：跳过 {before - len(candidates)} 个已分析用户")

        if not candidates:
            log.info("意向扫描：无待分析候选用户")
            return {"scanned": scanned, "candidates": 0, "leads": 0, "new_leads": 0, "level_counts": {}}

        log.info(
            f"意向扫描：{scanned} 个用户 → {len(candidates)} 个候选送 LLM 分析"
            + (f"（排除自家账号 {skipped_self} 个）" if skipped_self else "")
        )

        # 4. 批量送 LLM
        analyzed = await self._analyze_batches(candidates)

        # 5. 合并 + 过滤 + 落库
        leads = []
        level_counts: dict[str, int] = {}
        for item in analyzed:
            lead = self._build_lead(item, pre_extracted)
            if not lead:
                continue
            level_counts[lead.intent_level] = level_counts.get(lead.intent_level, 0) + 1
            if INTENT_LEVEL_ORDER.get(lead.intent_level, 0) < INTENT_LEVEL_ORDER.get(min_level, 0):
                continue
            leads.append(lead)

        new_leads = 0
        for lead in leads:
            try:
                lead_id = await db.upsert_lead(lead)
                if lead_id:
                    new_leads += 1
            except Exception as e:
                log.error(f"线索写入失败 {lead.lead_key}: {e}")

        log.info(
            f"意向扫描完成：候选 {len(candidates)}，命中线索 {len(leads)} "
            f"（{level_counts}），写入 {new_leads}"
        )

        return {
            "scanned": scanned,
            "candidates": len(candidates),
            "leads": len(leads),
            "new_leads": new_leads,
            "level_counts": level_counts,
            "lead_objects": leads,
        }

    async def _analyze_batches(self, candidates: list[dict]) -> list[dict]:
        """分批调用 LLM，返回解析后的结果列表"""
        batch_size = max(1, self.lead_config.batch_size)
        results: list[dict] = []

        async with LLMClient() as client:
            if not client.is_enabled:
                log.warning("LLM 未启用或未配置 API Key，跳过意向分析")
                return results

            for i in range(0, len(candidates), batch_size):
                batch = candidates[i:i + batch_size]
                prompt = build_intent_prompt(batch)
                try:
                    raw = await client.chat(
                        [
                            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.2,
                    )
                except Exception as e:
                    log.error(f"意向分析批次 {i // batch_size + 1} 失败: {e}")
                    continue

                if not raw:
                    continue

                parsed = parse_json_array(raw)
                if not parsed:
                    log.error(f"意向分析批次 {i // batch_size + 1} 返回无法解析: {raw[:200]}")
                    continue

                # 按 user_id / display_id 对齐回原候选
                by_id = {}
                for c in batch:
                    if c.get("user_id"):
                        by_id[str(c["user_id"])] = c
                    if c.get("display_id"):
                        by_id[str(c["display_id"])] = c

                for idx, item in enumerate(parsed):
                    uid = str(item.get("user_id") or "")
                    did = str(item.get("display_id") or "")
                    origin = by_id.get(uid) or by_id.get(did)
                    if origin is None:
                        # 模型丢失/改写了 id → 按返回顺序与批次顺序对齐兜底
                        if len(parsed) == len(batch):
                            origin = batch[idx]
                        else:
                            log.debug(f"意向结果无法对齐，跳过: {uid or did}")
                            continue
                    results.append({**origin, "_llm": item})

        return results

    def _build_lead(self, item: dict, pre_extracted: dict) -> Optional[Lead]:
        """把 LLM 结果 + 规则结果合并成 Lead"""
        llm = item.get("_llm") or {}
        key = item.get("_key") or ""
        if not key:
            return None

        level = _normalize_level(str(llm.get("intent_level", "")))
        if level == IntentLevel.NONE.value:
            # LLM 判定无意向 → 不入线索池（除非规则抽到了联系方式）
            contact = pre_extracted.get(key)
            if not (contact and contact.has_contact):
                return None

        # 联系方式：规则优先（更可信），LLM 补充
        rule_contact = pre_extracted.get(key)
        contact_type, contact_value = "", ""
        if rule_contact and rule_contact.has_contact:
            contact_type, contact_value = rule_contact.contact_type, rule_contact.value
        else:
            llm_contact = str(llm.get("contact") or "").strip()
            if llm_contact:
                contact_type = "llm"
                contact_value = llm_contact

        # 证据
        evidence = llm.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        evidence = [str(e)[:200] for e in evidence if e][:3]
        if not evidence:
            evidence = [m[:200] for m in (item.get("messages") or [])[:3]]

        try:
            score = int(llm.get("intent_score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))

        return Lead(
            lead_key=key,
            user_id=item.get("user_id") or "",
            display_id=item.get("display_id") or "",
            nickname=item.get("nickname") or "",
            room_id=item.get("room_id") or "",
            monitor_name=item.get("monitor_name") or "",
            live_room=item.get("live_room") or "",
            intent_level=level,
            intent_score=score,
            intent_reason=str(llm.get("reason") or "")[:300],
            car_model=str(llm.get("car_model") or "")[:60],
            contact_type=contact_type,
            contact_value=contact_value,
            evidence=json.dumps(evidence, ensure_ascii=False),
            evidence_count=len(evidence),
            comment_count=len(item.get("messages") or []),
        )


_analyzer: Optional[IntentAnalyzer] = None


def get_intent_analyzer() -> IntentAnalyzer:
    """获取全局分析器实例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = IntentAnalyzer()
    return _analyzer
