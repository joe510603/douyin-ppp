"""实时线索服务 — 弹幕进来即判定，异步落库 + 推送飞书

设计要点：
1. **打分在热路径但极轻**：纯正则，几十微秒，不拖慢采集
2. **落库与推送在旁路队列**：asyncio.Queue 缓冲，后台 worker 处理，
   飞书超时不会阻塞弹幕采集
3. **推送去重**：同一用户 N 分钟内不重复推（避免刷屏）
4. **证据累积**：同一用户多次发言，证据弹幕会累加，意向等级取最高

不依赖 LLM —— 这是主力路径。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_config
from ..models.comment import LiveComment
from ..models.lead import Lead, IntentLevel, INTENT_LEVEL_ORDER
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from .contact_extractor import extract_contacts
from .intent_analyzer import _build_self_names
from .rule_scorer import RuleScorer, ScoreResult

log = get_logger("lead_service")

# 队列最大长度，超了丢弃最旧的（背压保护）
_MAX_QUEUE = 2000

# 平台占位用户 id：弹幕流里会出现 user_id='111111' / '0' 的系统消息，
# 它们不是真人。历史扫描路径（query_user_activity）本来就排除了这两个，
# 实时路径必须对齐，否则占位账号会混进线索池。
_PLACEHOLDER_USER_IDS = {"111111", "0"}


class LeadService:
    """实时购车意向线索服务"""

    def __init__(self):
        self.config = get_config()
        self.scorer = RuleScorer(min_level=self.config.lead.min_intent_level)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._worker: Optional[asyncio.Task] = None
        self._self_names = _build_self_names(self.config)
        # 最近推送过的用户 → 上次推送时间（去重冷却）
        self._pushed_at: dict[str, datetime] = {}
        # 最近写入多维表格的用户 → 上次写入时间（写表冷却）
        self._bitable_at: dict[str, datetime] = {}
        # 运行期统计
        self.stats = {
            "scanned": 0,
            "hits": 0,
            "excluded": 0,
            "pushed": 0,
            "tabled": 0,
            "dropped": 0,
        }

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self):
        """启动后台 worker"""
        if self._worker and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._worker_loop(), name="lead-service")
        log.info(f"实时线索服务已启动（最低等级 {self.config.lead.min_intent_level}）")

    async def stop(self):
        """停止 worker（会先把队列排空）"""
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _worker_loop(self):
        """后台消费队列"""
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break
            if item is None:
                break
            try:
                await self._handle(item)
            except Exception as e:
                log.error(f"实时线索处理失败: {e}", exc_info=True)
            finally:
                self._queue.task_done()

    # ============================================================
    # 热路径：打分 + 入队（非阻塞）
    # ============================================================

    def submit(self, comment: LiveComment) -> Optional[ScoreResult]:
        """
        对一条新弹幕打分，命中则入队等待落库。

        这个函数在采集热路径上被调用，必须**极轻且不阻塞**。

        Returns:
            命中的打分结果；未命中返回 None
        """
        if not self.config.lead.realtime_enabled:
            return None
        if comment.message_type != "WebcastChatMessage":
            return None
        # 平台占位用户不是真人
        if str(comment.user_id or "") in _PLACEHOLDER_USER_IDS:
            return None

        try:
            result = self.scorer.score(
                comment.content, comment.user_nickname, self._self_names
            )
        except Exception as e:
            log.warning(f"规则打分异常: {e}")
            return None

        self.stats["scanned"] += 1
        if result.excluded:
            self.stats["excluded"] += 1
            return None
        if not result.is_lead:
            return None

        self.stats["hits"] += 1
        try:
            self._queue.put_nowait((comment, result))
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            return None
        return result

    # ============================================================
    # 旁路：落库 + 推送
    # ============================================================

    async def _handle(self, item: tuple[LiveComment, ScoreResult]):
        """处理一条命中线索：落库 + 按条件推飞书"""
        comment, result = item
        db = get_db()
        if not db or not db._db:
            return

        contact = extract_contacts([comment.content])

        lead_key = comment.user_display_id or comment.user_id or (
            f"{comment.room_id}:{comment.user_nickname}"
        )
        if not lead_key:
            return

        lead = Lead(
            lead_key=lead_key,
            user_id=comment.user_id or "",
            display_id=comment.user_display_id or "",
            nickname=comment.user_nickname or "",
            room_id=comment.room_id or "",
            monitor_name=comment.monitor_name or "",
            intent_level=result.level,
            intent_score=result.score,
            intent_reason=result.reason,
            car_model=result.car_model,
            contact_type=contact.contact_type,
            contact_value=contact.value,
            evidence=json.dumps([comment.content[:200]], ensure_ascii=False),
            evidence_count=1,
            comment_count=1,
            source="rule",
        )

        try:
            lead_id = await db.upsert_lead(lead)
        except Exception as e:
            log.error(f"线索落库失败 {lead_key}: {e}")
            return

        # 取回已存的 record_id，避免每次写表都全表扫描一遍
        if lead_id:
            lead.id = lead_id
            try:
                row = await db.get_lead(lead_id)
                if row:
                    lead.bitable_record_id = row.get("bitable_record_id") or ""
            except Exception:
                pass

        # ---- 写入多维表格（与群推送解耦：群推送关掉也不影响上表）----
        await self._maybe_sync_bitable(db, lead, result.level)

        # 是否需要推飞书
        if not self.config.lead.realtime_push:
            return
        push_levels = self.config.notify.push_levels or ["high", "medium"]
        if result.level not in push_levels:
            return
        if self.config.notify.contact_only and not contact.has_contact:
            return
        if self._in_cooldown(lead_key):
            return

        await self._push(lead, db)

    async def _push(self, lead: Lead, db):
        """推送到飞书群"""
        from ..notify.feishu import get_notifier

        notifier = get_notifier(self.config)
        if not notifier.is_configured:
            return

        ok, err = await notifier.push_leads(
            [{
                "nickname": lead.nickname,
                "display_id": lead.display_id,
                "intent_level": lead.intent_level,
                "intent_score": lead.intent_score,
                "intent_reason": lead.intent_reason,
                "car_model": lead.car_model,
                "contact_value": lead.contact_value,
                "monitor_name": lead.monitor_name,
                "live_room": lead.live_room,
            }],
            title="🔔 新购车意向",
        )

        if ok:
            self.stats["pushed"] += 1
            self._pushed_at[lead.lead_key] = datetime.now()
            if self.config.notify.mark_pushed:
                try:
                    await db.mark_leads_pushed([lead.id] if lead.id else [])
                except Exception:
                    pass
            log.info(
                f"[实时线索] 已推送飞书: {lead.nickname} "
                f"({lead.intent_level} {lead.intent_score}分) {lead.intent_reason}"
            )
        else:
            log.warning(f"[实时线索] 飞书推送失败: {err}")

    def _in_cooldown(self, lead_key: str) -> bool:
        """同一用户是否处于推送冷却期"""
        cooldown = self.config.lead.push_cooldown_minutes
        if cooldown <= 0:
            return False
        last = self._pushed_at.get(lead_key)
        if last and datetime.now() - last < timedelta(minutes=cooldown):
            return True
        return False

    def _in_bitable_cooldown(self, lead_key: str) -> bool:
        """同一用户距离上次写表的间隔"""
        cooldown = self.config.notify.bitable_cooldown_minutes
        if cooldown <= 0:
            return False
        last = self._bitable_at.get(lead_key)
        if last and datetime.now() - last < timedelta(minutes=cooldown):
            return True
        return False

    async def _maybe_sync_bitable(self, db, lead: Lead, level: str):
        """把线索写入飞书多维表格

        **与群推送解耦**：群机器人关掉（webhook 没配）时，上表照常进行——
        线索表才是长期沉淀的地方，群推送只是即时提醒。

        同一用户短时间内重复命中只更新不新建，避免把表刷爆。
        """
        cfg = self.config.notify
        if not cfg.bitable_enabled:
            return

        levels = cfg.bitable_levels or ["high", "medium", "low"]
        if level not in levels:
            return
        if self._in_bitable_cooldown(lead.lead_key):
            return

        result = await sync_leads_to_bitable([lead], db=db, config=self.config)
        self._bitable_at[lead.lead_key] = datetime.now()
        if not result.get("error"):
            self.stats["tabled"] = self.stats.get("tabled", 0) + 1


async def sync_leads_to_bitable(
    leads: list[Lead],
    db=None,
    config=None,
) -> dict:
    """把一批线索写入飞书多维表格（按抖音号去重，命中已有则更新）

    实时筛查和历史回扫共用这条路径。
    """
    from ..notify.bitable import get_bitable_writer

    if config is None:
        from ..config import get_config
        config = get_config()

    if not config.notify.bitable_enabled:
        return {"created": 0, "updated": 0, "failed": 0, "error": "", "record_ids": {}}
    if not leads:
        return {"created": 0, "updated": 0, "failed": 0, "error": "", "record_ids": {}}

    writer = get_bitable_writer(config)
    if not writer.is_configured:
        log.warning("[多维表格] 未配置完整或找不到 lark-cli，跳过写入")
        return {"created": 0, "updated": 0, "failed": 0, "error": "未配置", "record_ids": {}}

    now = datetime.now()
    payloads = []
    for lead in leads:
        payloads.append({
            "lead_key": lead.lead_key,
            "display_id": lead.display_id,
            "nickname": lead.nickname,
            "intent_level": lead.intent_level,
            "intent_score": lead.intent_score,
            "intent_reason": lead.intent_reason,
            "car_model": lead.car_model,
            "contact_value": lead.contact_value,
            "monitor_name": lead.monitor_name,
            "live_room": lead.live_room,
            "evidence": lead.evidence,
            "status": lead.status,
            "outreach_status": lead.outreach_status,
            "_bitable_record_id": lead.bitable_record_id,
            "_found_ts": int(now.timestamp() * 1000),        # http 后端用毫秒时间戳
            "_found_at": now.strftime("%Y-%m-%d %H:%M:%S"),  # lark_cli 后端用字符串
        })

    try:
        result = await writer.sync_leads(payloads)
    except Exception as e:
        log.error(f"[多维表格] 写入失败: {e}")
        return {"created": 0, "updated": 0, "failed": len(payloads), "error": str(e), "record_ids": {}}

    if result.get("error"):
        log.error(f"[多维表格] 写入失败: {result['error']}")
        return result

    # 回写 record_id
    record_ids = result.get("record_ids") or {}
    if db is not None and record_ids:
        for lead in leads:
            rid = record_ids.get(lead.lead_key)
            if rid and lead.id:
                try:
                    await db.update_lead(lead.id, bitable_record_id=rid)
                except Exception:
                    pass

    log.info(
        f"[多维表格] {result.get('created', 0)} 新建 / {result.get('updated', 0)} 更新 "
        f"— {len(payloads)} 条线索"
    )
    return result


_service: Optional[LeadService] = None


def get_lead_service() -> LeadService:
    """获取全局线索服务"""
    global _service
    if _service is None:
        _service = LeadService()
    return _service
