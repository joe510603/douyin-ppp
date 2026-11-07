"""触达任务编排器 — 限速、每日上限、失败自停、执行记录

执行顺序：
1. 校验开关与每日上限
2. 取待触达线索（有抖音号、状态 pending）
3. 启动浏览器（一次任务复用一个会话）
4. 逐条：生成话术 → 随机延时 → 搜人 → 留言 → 落库
5. 连续失败达到阈值立即停止（避免把账号打进风控）
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from datetime import datetime
from typing import Optional

from ..config import get_config
from ..models.lead import OutreachRecord, OutreachStatus
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from .operator import DouyinOperator

log = get_logger("outreach_runner")


class OutreachRunner:
    """抖音触达任务执行器"""

    def __init__(self):
        self.config = get_config()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def run(
        self,
        limit: Optional[int] = None,
        message_override: str = "",
    ) -> dict:
        """
        执行一轮触达任务。

        Args:
            limit: 本轮最多处理多少条，默认取配置
            message_override: 指定留言文案（留空则用模板或 AI 生成）

        Returns:
            {"total","success","failed","skipped","stopped_reason"}
        """
        if self._running:
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "stopped_reason": "已有触达任务在运行"}

        oc = self.config.outreach
        if not oc.enabled:
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "stopped_reason": "触达功能未启用（配置管理 → 触达）"}

        db = get_db()
        if not db or not db._db:
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "stopped_reason": "数据库未就绪"}

        max_per_run = int(limit or oc.max_per_run)

        # 每日上限检查
        done_today = await db.count_outreach_today()
        if done_today >= oc.daily_limit:
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "stopped_reason": f"已达每日上限（今日 {done_today}/{oc.daily_limit}）"}

        # 取待触达线索
        leads, _ = await db.query_leads(outreach_status="pending", limit=max_per_run * 2)
        targets = [l for l in leads if l.get("display_id")][:max_per_run]

        if not targets:
            return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                    "stopped_reason": "没有待触达线索"}

        self._running = True
        stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "stopped_reason": ""}
        consecutive_failures = 0

        try:
            async with DouyinOperator() as op:
                for idx, lead in enumerate(targets):
                    # 每日上限二次检查
                    done_today = await db.count_outreach_today()
                    if done_today >= oc.daily_limit:
                        stats["stopped_reason"] = f"已达每日上限（{done_today}/{oc.daily_limit}）"
                        break

                    display_id = lead.get("display_id") or ""
                    lead_id = lead.get("id")

                    # 生成话术
                    message = message_override or await self._build_message(lead)

                    record = OutreachRecord(
                        id=uuid.uuid4().hex[:16],
                        lead_id=lead_id,
                        display_id=display_id,
                        nickname=lead.get("nickname") or "",
                        message=message,
                        status=OutreachStatus.SEARCHING.value,
                    )
                    await db.insert_outreach_record(record)
                    await db.update_lead(lead_id, outreach_status=OutreachStatus.SEARCHING.value)
                    stats["total"] += 1

                    # 随机延时（第一条之前也等一下，更像真人）
                    await asyncio.sleep(random.uniform(oc.min_delay, oc.max_delay))

                    ok, note, profile_url = await op.search_and_comment(
                        display_id, message, comments_per_user=oc.comments_per_user
                    )

                    if ok:
                        stats["success"] += 1
                        consecutive_failures = 0
                        await db.update_outreach_record(
                            record.id,
                            status=OutreachStatus.COMMENTED.value,
                            target_url=profile_url,
                            error="",
                        )
                        await db.update_lead(
                            lead_id,
                            outreach_status=OutreachStatus.COMMENTED.value,
                            outreach_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            outreach_note=profile_url,
                        )
                        log.info(f"[触达] ✅ {display_id} 留言成功")
                    else:
                        stats["failed"] += 1
                        consecutive_failures += 1
                        await db.update_outreach_record(
                            record.id,
                            status=OutreachStatus.FAILED.value,
                            target_url=profile_url,
                            error=note[:300],
                        )
                        await db.update_lead(
                            lead_id,
                            outreach_status=OutreachStatus.FAILED.value,
                            outreach_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            outreach_note=note[:300],
                        )
                        log.warning(f"[触达] ❌ {display_id} 失败: {note}")

                        # 连续失败达阈值 → 立即停止，保护账号
                        if consecutive_failures >= oc.fail_stop_threshold:
                            stats["stopped_reason"] = (
                                f"连续失败 {consecutive_failures} 次，已自动停止"
                            )
                            log.warning(f"[触达] {stats['stopped_reason']}")
                            break

        except Exception as e:
            log.error(f"触达任务异常: {e}", exc_info=True)
            stats["stopped_reason"] = f"任务异常: {e}"
        finally:
            self._running = False

        return stats

    async def _build_message(self, lead: dict) -> str:
        """
        生成留言文案。

        优先级：配置模板 → AI 生成 → 配置模板兜底
        """
        template = self.config.outreach.message_template or ""
        evidence = ""
        raw = lead.get("evidence") or ""
        if raw:
            try:
                items = json.loads(raw)
                if isinstance(items, list):
                    evidence = "；".join(str(i) for i in items)
            except (json.JSONDecodeError, TypeError):
                evidence = str(raw)[:200]

        model_text = self._fill_template(template, lead)

        # 有 LLM 就让 AI 写一版更自然的
        llm = self.config.llm
        if llm.enabled and llm.api_key:
            try:
                from ..intelligence.prompts import (
                    MESSAGE_SYSTEM_PROMPT,
                    build_message_prompt,
                    parse_json_object,
                )
                from ..utils.llm_client import LLMClient

                prompt = build_message_prompt(
                    nickname=lead.get("nickname") or "",
                    display_id=lead.get("display_id") or "",
                    context=evidence,
                    intent_level=lead.get("intent_level") or "",
                    car_model=lead.get("car_model") or "",
                )
                async with LLMClient() as client:
                    if client.is_enabled:
                        raw_reply = await client.chat(
                            [
                                {"role": "system", "content": MESSAGE_SYSTEM_PROMPT},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.7,
                            max_tokens=500,
                        )
                        if raw_reply:
                            obj = parse_json_object(raw_reply)
                            msg = str(obj.get("message") or "").strip()
                            if msg:
                                return msg[:200]
            except Exception as e:
                log.warning(f"AI 生成留言失败，改用模板: {e}")

        return model_text

    def _fill_template(self, template: str, lead: dict) -> str:
        """填充留言模板"""
        if not template:
            return "看到你在直播间聊到我们店里的车型，方便留个联系方式细聊吗？"
        return template.format(
            name=lead.get("nickname") or "朋友",
            nickname=lead.get("nickname") or "朋友",
            model=lead.get("car_model") or "我们店里的车型",
            live_room=lead.get("monitor_name") or lead.get("live_room") or "直播间",
            display_id=lead.get("display_id") or "",
        )[:200]


_runner: Optional[OutreachRunner] = None


def get_runner() -> OutreachRunner:
    """获取全局执行器实例"""
    global _runner
    if _runner is None:
        _runner = OutreachRunner()
    return _runner
