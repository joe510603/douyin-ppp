"""规则回溯扫描 — 用规则引擎回扫历史弹幕，批量填充线索池

这是「扫描购车意向」按钮在**不配置 LLM** 时的实现路径：
把时间窗内每个用户的弹幕逐条打分，取该用户的最高分作为意向判定。

与实时筛查共用同一套规则，保证「实时进来」和「回溯历史」判定口径一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from ..models.lead import Lead, IntentLevel, INTENT_LEVEL_ORDER
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from .contact_extractor import extract_contacts
from .intent_analyzer import _build_self_names, _normalize_name
from .rule_scorer import RuleScorer

log = get_logger("rule_scanner")


async def scan_by_rules(
    lookback_hours: int = 72,
    min_level: str = "low",
    limit_users: int = 800,
    force: bool = False,
) -> dict:
    """
    用规则引擎回扫历史弹幕，生成线索。

    Args:
        lookback_hours: 回溯时间窗（小时）
        min_level: 最低意向等级
        limit_users: 最多扫描多少个用户
        force: True 时忽略「已分析过」的去重

    Returns:
        {"scanned","hits","leads","new_leads","level_counts","models"}
    """
    from ..config import get_config

    config = get_config()
    db = get_db()
    if not db or not db._db:
        return {"error": "数据库未就绪", "scanned": 0, "hits": 0, "leads": 0}

    scorer = RuleScorer(min_level=min_level)
    self_names = _build_self_names(config)
    since = datetime.now() - timedelta(hours=lookback_hours)

    groups = await db.query_user_activity(since=since, limit_users=limit_users)
    scanned = len(groups)
    if not groups:
        return {"scanned": 0, "hits": 0, "leads": 0, "new_leads": 0, "level_counts": {}, "models": {}}

    # 已分析过的用户跳过（除非 force）
    if not force:
        existing = await db.get_existing_lead_keys([g["ukey"] for g in groups])
        if existing:
            before = len(groups)
            groups = [g for g in groups if g["ukey"] not in existing]
            log.info(f"规则回扫：跳过 {before - len(groups)} 个已分析用户")

    hits = 0
    leads = []
    level_counts: dict[str, int] = {}
    models: dict[str, int] = {}

    for g in groups:
        # 排除自家账号
        if self_names and _normalize_name(g["nickname"]) in self_names:
            continue

        # 逐条打分，取最高分
        best = None
        for msg in g["messages"]:
            r = scorer.score(msg, g["nickname"], self_names)
            if r.excluded:
                continue
            if not r.is_lead:
                continue
            if best is None or r.score > best.score:
                best = r

        if best is None:
            continue
        hits += 1

        # 规则打分可能把 medium 降到 low（提到车型但无购买行为），这里按最终 level 判
        if INTENT_LEVEL_ORDER.get(best.level, 0) < INTENT_LEVEL_ORDER.get(min_level, 0):
            continue

        contact = extract_contacts(g["messages"])
        lead = Lead(
            lead_key=g["ukey"],
            user_id=g["user_id"],
            display_id=g["display_id"],
            nickname=g["nickname"],
            room_id=g["room_id"],
            monitor_name=g["monitor_name"],
            intent_level=best.level,
            intent_score=best.score,
            intent_reason=best.reason,
            car_model=best.car_model,
            contact_type=contact.contact_type,
            contact_value=contact.value,
            evidence=json.dumps(g["messages"][:3], ensure_ascii=False),
            evidence_count=min(3, len(g["messages"])),
            comment_count=g["count"],
            source="rule",
        )
        leads.append(lead)
        level_counts[best.level] = level_counts.get(best.level, 0) + 1
        if best.car_model:
            models[best.car_model] = models.get(best.car_model, 0) + 1

    new_leads = 0
    try:
        new_leads = await db.upsert_leads_batch(leads)
    except Exception as e:
        log.error(f"线索批量写入失败: {e}")

    # 写入飞书多维表格（与实时路径共用同一套逻辑，按抖音号去重）
    if leads:
        try:
            from .lead_service import sync_leads_to_bitable

            await sync_leads_to_bitable(leads, db=db, config=config)
        except Exception as e:
            log.error(f"线索同步多维表格失败: {e}")

    log.info(
        f"规则回扫完成：扫描 {scanned} 用户 → 命中 {hits} → 入池 {len(leads)} 条 "
        f"（{level_counts}），车型热度 {models}"
    )

    return {
        "scanned": scanned,
        "hits": hits,
        "leads": len(leads),
        "new_leads": new_leads,
        "level_counts": level_counts,
        "models": models,
    }
