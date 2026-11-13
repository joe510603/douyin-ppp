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
from ..models.lead import Lead, IntentLevel, INTENT_LEVEL_ORDER, fmt_dt
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
            # 最后一次抓到的时间 = 这条弹幕的时间
            # （触达排序按它倒序：「最近抓到的线索先留言」）
            last_hit_at=fmt_dt(comment.create_time),
            source="rule",
        )

        try:
            lead_id = await db.upsert_lead(lead)
        except Exception as e:
            log.error(f"线索落库失败 {lead_key}: {e}")
            return

        # 取回已存的 record_id 和同步状态标记，避免每次写表都全表扫描一遍。
        # bitable_synced_status 必须取回真实值——它是判断
        # 「本地状态是否真的变了」的唯一依据，
        # 用默认值会误判成「本地没改过」而漏写状态。
        if lead_id:
            lead.id = lead_id
            try:
                row = await db.get_lead(lead_id)
                if row:
                    lead.bitable_record_id = row.get("bitable_record_id") or ""
                    lead.bitable_synced_status = row.get("bitable_synced_status") or ""
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

    # 统一从 DB 回填真实状态（record_id + 上次同步标记）。
    # 调用方构造的 Lead 对象里这两个字段可能还是默认值，
    # 而它们正是判断「本地状态是否真的变了」的唯一依据。
    db_rows: dict[str, dict] = {}
    if db is not None:
        keys = [lead.lead_key for lead in leads if lead.lead_key]
        if keys:
            try:
                rows = await db.get_leads_by_keys(keys)
                db_rows = {r.get("lead_key") or "": r for r in rows}
            except Exception:
                db_rows = {}

    for lead in leads:
        row = db_rows.get(lead.lead_key) or {}
        synced_status = (
            row.get("bitable_synced_status")
            or getattr(lead, "bitable_synced_status", "")
            or ""
        )
        record_id = (
            row.get("bitable_record_id")
            or getattr(lead, "bitable_record_id", "")
            or ""
        )
        # 状态也以 DB 为准（DB 才是运营改状态的落点）
        status = row.get("status") or lead.status
        outreach_status = row.get("outreach_status") or lead.outreach_status

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
            "status": status,
            "outreach_status": outreach_status,
            # 上次同步进飞书时的状态，用来判断本地状态是否真的变了。
            # 销售在飞书表里改过的状态是权威值，不能被本地默认值覆盖回去。
            "bitable_synced_status": synced_status,
            "_bitable_record_id": record_id,
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

    # 回写 record_id + 同步状态标记
    record_ids = result.get("record_ids") or {}
    if db is not None and record_ids:
        from ..notify.bitable import build_synced_status, status_changed

        # 按 lead_key 找 payload——payload 里才是回填后的真实状态
        payload_by_key = {p.get("lead_key"): p for p in payloads}
        lead_by_key = {lead.lead_key: lead for lead in leads}

        for key, rid in record_ids.items():
            lead = lead_by_key.get(key)
            payload = payload_by_key.get(key)
            if not rid or not lead or not lead.id or not payload:
                continue
            updates: dict = {"bitable_record_id": rid}
            # 只有状态真被写进飞书了，才更新同步标记。
            # 被守卫跳过时标记保持原样，下次同步仍会认为「本地没改过」，
            # 于是继续保留销售在飞书表里改过的值。
            if status_changed(payload):
                updates["bitable_synced_status"] = build_synced_status(payload)
            try:
                await db.update_lead(lead.id, **updates)
            except Exception:
                pass

    log.info(
        f"[多维表格] {result.get('created', 0)} 新建 / {result.get('updated', 0)} 更新 "
        f"— {len(payloads)} 条线索"
    )
    return result


# ============================================================
# 反向同步：把飞书表里销售改过的状态拉回本地
# ============================================================

# 飞书表里的状态文案 → 本地 status / outreach_status
_FEISHU_STATUS_TO_LOCAL = {
    "待处理": "new",
    "已跟进": "contacted",
    "已忽略": "ignored",
    # 销售拿到电话/微信了 → 终态，别再留言
    "已留资": "converted",
}
_FEISHU_OUTREACH_TO_LOCAL = {
    "待触达": "pending",
    "搜索中": "searching",
    "已定位": "found",
    "已留言": "commented",
    "失败": "failed",
    "已跳过": "skipped",
    # 「这次没搜到」不是终态，等下轮重试
    "搜不到": "not_found",
    # 「已留资」→ 终态，不再进触达队列
    "已留资": "converted",
}


async def pull_bitable_status(db=None, config=None) -> dict:
    """把飞书表里销售手动改过的状态回流到本地

    「处理状态」「触达状态」是双向字段：销售在飞书表里改，本地网页也能改。
    本程序只负责往飞书写线索，**不能**把销售改过的值覆盖回去——
    但本地的线索中心也应该反映销售的处理进度，所以要定期拉回来。

    只在「本地状态相对上次同步没变过」时才采纳飞书的值，
    避免本地运营刚改完就被飞书端的旧值冲掉。

    Returns:
        {"pulled": n, "updated": n, "skipped": n, "error": str}
    """
    result = {"pulled": 0, "updated": 0, "skipped": 0, "error": ""}

    if config is None:
        config = get_config()
    if db is None:
        db = get_db()
    if not db or not getattr(db, "_db", None):
        result["error"] = "数据库未就绪"
        return result
    if not config.notify.bitable_enabled:
        result["error"] = "未启用多维表格"
        return result

    from ..notify.bitable import get_bitable_writer

    writer = get_bitable_writer(config)
    if not writer.is_configured:
        result["error"] = "未配置"
        return result

    try:
        status_map = await writer.fetch_status_map()
    except Exception as e:
        result["error"] = str(e)
        return result

    if not status_map:
        result["error"] = "读到 0 条记录"
        return result

    result["pulled"] = len(status_map)

    # 取本地有 record_id 的线索（只有这些才对得上飞书的记录）
    cursor = await db._db.execute(
        "SELECT id, display_id, status, outreach_status, bitable_synced_status, "
        "bitable_record_id FROM leads WHERE display_id != ''"
    )
    rows = await cursor.fetchall()

    from ..notify.bitable import STATUS_LABELS, OUTREACH_LABELS

    updated = 0
    skipped = 0
    for row in rows:
        r = dict(row)
        remote = status_map.get(r.get("display_id") or "")
        if not remote:
            continue

        remote_st = remote.get("处理状态") or ""
        remote_os = remote.get("触达状态") or ""

        local_st = STATUS_LABELS.get(r.get("status") or "", "待处理")
        local_os = OUTREACH_LABELS.get(r.get("outreach_status") or "", "待触达")

        # 飞书端状态为空 → 说明这条记录建的时候状态字段被跳过了
        # （历史记录，或首次同步时守卫判定「本地没变过」而没写）。
        # 把本地的值补写回飞书，否则飞书表里这一格永远是空白，
        # 销售看着会以为这条线索还没被处理。
        # 只补空字段，不覆盖飞书端已有的值——销售改过的状态依然是权威的。
        if not remote_st or not remote_os:
            if await _write_back_status(writer, r, local_st, local_os, remote_st, remote_os):
                updated += 1
            continue

        # 本地状态和上次同步进飞书的值一致 → 说明本地这次没动过，
        # 可以安全采纳飞书的值（销售改的）
        synced = (r.get("bitable_synced_status") or "").strip()
        if synced:
            s_st, _, s_os = synced.partition("|")
            local_unchanged = (local_st == (s_st or "").strip())
            local_unchanged = local_unchanged and (local_os == (s_os or "").strip())
        else:
            # 从没同步过 → 本地状态是默认的，采纳飞书的
            local_unchanged = local_st == "待处理" and local_os == "待触达"

        if not local_unchanged:
            skipped += 1
            continue

        updates: dict = {}
        new_status = _FEISHU_STATUS_TO_LOCAL.get(remote_st)
        if new_status and new_status != r.get("status"):
            updates["status"] = new_status
        new_outreach = _FEISHU_OUTREACH_TO_LOCAL.get(remote_os)
        if new_outreach and new_outreach != r.get("outreach_status"):
            updates["outreach_status"] = new_outreach

        # ---- 「已留资」两列必须一起生效 ----
        #
        # 销售在飞书表里把状态改成「已留资」（意思是已经拿到电话/微信了），
        # 不管改的是「处理状态」还是「触达状态」那一列，
        # 这个人就**不该再被留言**了 → 触达状态必须一起变成终态。
        #
        # 反过来也成立：只把处理状态改成「已留资」而触达状态还写着「待触达」，
        # 这个人照样会被系统翻出来留言 —— 那是骚扰。
        if new_status == "converted" or new_outreach == "converted":
            updates["status"] = "converted"
            updates["outreach_status"] = "converted"

        if not updates:
            skipped += 1
            continue

        # 同步标记也跟着更新成飞书端的值，避免下一轮又认为「本地没改过」
        updates["bitable_synced_status"] = f"{remote_st or local_st}|{remote_os or local_os}"
        try:
            await db.update_lead(r["id"], **updates)
            updated += 1
            # 「已留资」要把**两列**都写回飞书，
            # 否则两列各说各话：本地是「已留资」、飞书还写着「待触达」，
            # 下一轮回拉又会把本地的终态改回「待触达」。
            if updates.get("outreach_status") == "converted":
                try:
                    await writer.patch_fields(
                        r.get("bitable_record_id") or "",
                        {"处理状态": "已留资", "触达状态": "已留资"},
                    )
                except Exception as e:
                    log.warning(f"[多维表格] 写回「已留资」失败: {e}")
        except Exception:
            pass

    result["updated"] = updated
    result["skipped"] = skipped
    if updated:
        log.info(f"[多维表格] 回拉状态：{updated} 条已更新，{skipped} 条跳过")
    return result


async def _write_back_status(
    writer, row: dict, local_st: str, local_os: str, remote_st: str, remote_os: str
) -> bool:
    """把本地的状态补写回飞书（飞书端该字段为空时）

    只补空字段，不覆盖飞书端已有的值——
    销售改过的状态依然是权威的。
    """
    rid = row.get("bitable_record_id") or ""
    if not rid:
        return False
    patch: dict = {}
    if not remote_st:
        patch["处理状态"] = local_st
    if not remote_os:
        patch["触达状态"] = local_os
    if not patch:
        return False
    try:
        await writer.patch_fields(rid, patch)
        return True
    except Exception as e:
        log.warning(f"[多维表格] 补写状态失败 {rid}: {e}")
        return False


_service: Optional[LeadService] = None


def get_lead_service() -> LeadService:
    """获取全局线索服务"""
    global _service
    if _service is None:
        _service = LeadService()
    return _service
