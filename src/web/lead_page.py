"""线索中心 — 购车意向线索池 + 抖音触达队列

两个页签：
- 线索池：AI 筛选出的购车意向用户，可推送飞书、手动改判、加入触达队列
- 触达队列：第二步「搜抖音号 + 作品留言」的执行面板
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from nicegui import ui

from ..config import get_config
from ..models.lead import INTENT_LEVEL_ORDER
from ..notify.feishu import get_notifier
from ..storage.db_storage import get_db
from ..utils.logger import get_logger

log = get_logger("lead_page")

LEVEL_LABELS = {
    "high": "高意向",
    "medium": "中意向",
    "low": "低意向",
    "none": "无意向",
}

LEVEL_COLORS = {
    "high": "bg-red-2 text-white",
    "medium": "bg-orange-2 text-white",
    "low": "bg-blue-2 text-white",
    "none": "bg-grey-3 text-dark",
}

STATUS_LABELS = {
    "new": "待处理",
    "pushed": "已推送",
    "contacted": "已跟进",
    "ignored": "已忽略",
}

OUTREACH_LABELS = {
    "pending": "待触达",
    "searching": "搜索中",
    "found": "已定位",
    "commented": "已留言",
    "failed": "失败",
    "skipped": "已跳过",
}


def _store_options() -> dict:
    """门店下拉选项

    从 config 的 monitors 里取，归一化成短名（去「小米汽车」前缀和分隔符），
    这样同一个门店的不同写法会收敛成一项。
    """
    from ..notify.bitable import normalize_store

    cfg = get_config()
    seen: dict[str, str] = {}
    for m in cfg.monitors:
        name = normalize_store(getattr(m, "name", "") or "")
        if name and name not in seen:
            seen[name] = name
    return {"": "全部门店", **seen}


def create_lead_page():
    """创建线索中心页面"""

    config = get_config()
    state = {"scanning": False, "last_scan": ""}

    # 打开页面时自动加载一次线索

    # ============================================================
    # 内部函数
    # ============================================================

    async def _do_query():
        db = get_db()
        if not db or not db._db:
            log.warning("线索查询中止：数据库未就绪")
            ui.notify("数据库未就绪", type="negative")
            return

        levels = [level_filter.value] if level_filter.value else None
        has_contact = None
        if contact_filter.value == "yes":
            has_contact = True
        elif contact_filter.value == "no":
            has_contact = False

        try:
            rows, total = await db.query_leads(
                intent_levels=levels,
                status=status_filter.value or None,
                has_contact=has_contact,
                keyword=keyword_input.value.strip() or None,
                store=store_filter.value or None,
                limit=500,
            )
        except Exception as e:
            log.error(f"线索查询失败: {e}", exc_info=True)
            ui.notify(f"查询失败: {e}", type="negative")
            return

        log.info(f"线索查询完成：命中 {total} 条（等级过滤 {levels}）")

        for r in rows:
            r["intent_level"] = LEVEL_LABELS.get(r.get("intent_level"), r.get("intent_level"))
            r["status"] = STATUS_LABELS.get(r.get("status"), r.get("status"))
            r["outreach_status"] = OUTREACH_LABELS.get(
                r.get("outreach_status"), r.get("outreach_status")
            )
            r["intent_reason"] = (r.get("intent_reason") or "")[:60]
            r["contact_value"] = (r.get("contact_value") or "")[:40]
            r["source"] = "规则" if (r.get("source") or "rule") == "rule" else "AI"

        lead_table.rows = rows
        lead_table.update()
        selected_label.text = f"共 {total} 条，已选 {len(lead_table.selected or [])} 条"
        ui.notify(f"查询完成，共 {total} 条线索", type="positive")

    def _on_select(e):
        selected_label.text = f"已选 {len(e.selection or [])} 条"

    async def _do_scan():
        if state["scanning"]:
            ui.notify("扫描进行中，请稍候", type="warning")
            return

        state["scanning"] = True
        scan_btn.disable()
        scan_result.text = "扫描中，正在用规则引擎回扫历史弹幕..."
        try:
            # ---------- 第一步：规则扫描（不需要 LLM，秒级）----------
            from ..intelligence.rule_scanner import scan_by_rules

            rule_result = await scan_by_rules(
                lookback_hours=int(lookback_select.value or 72),
                min_level=min_level_select.value,
            )
            if rule_result.get("error"):
                scan_result.text = f"扫描失败: {rule_result['error']}"
                ui.notify(rule_result["error"], type="negative")
                return

            counts = rule_result.get("level_counts") or {}
            models = rule_result.get("models") or {}
            model_text = ""
            if models:
                top = sorted(models.items(), key=lambda kv: -kv[1])[:3]
                model_text = "，车型热度 " + "、".join(f"{k}({v})" for k, v in top)

            scan_result.text = (
                f"规则扫描：{rule_result.get('scanned', 0)} 个用户 → "
                f"命中 {rule_result.get('hits', 0)} → 入池 {rule_result.get('leads', 0)} 条 "
                f"（高 {counts.get('high', 0)} / 中 {counts.get('medium', 0)} / 低 {counts.get('low', 0)}）"
                f"{model_text}"
            )
            ui.notify(f"规则扫描完成，命中 {rule_result.get('leads', 0)} 条线索", type="positive")
            await _do_query()

            # ---------- 第二步：LLM 精排（可选，未配置则跳过）----------
            llm = config.llm
            if llm.enabled and llm.api_key:
                scan_result.text += "  ｜ 正在用 AI 复判..."
                from ..intelligence.intent_analyzer import get_intent_analyzer

                llm_result = await get_intent_analyzer().scan(
                    lookback_hours=int(lookback_select.value or 24),
                    min_level=min_level_select.value,
                )
                if llm_result.get("leads"):
                    scan_result.text += (
                        f"  ｜ AI 复判补充 {llm_result.get('leads', 0)} 条"
                    )
                    await _do_query()

        except Exception as e:
            log.error(f"意向扫描失败: {e}", exc_info=True)
            scan_result.text = f"扫描失败: {e}"
            ui.notify(f"扫描失败: {e}", type="negative")
        finally:
            state["scanning"] = False
            scan_btn.enable()

    async def _do_push():
        db = get_db()
        notifier = get_notifier(config)
        if not notifier.is_configured:
            ui.notify("请先在「配置管理」里配置飞书 webhook", type="warning")
            return

        selected = lead_table.selected or []
        levels = [level_filter.value] if level_filter.value else None
        has_contact = True if contact_filter.value == "yes" else None

        if selected:
            rows = await db.get_leads_by_ids([int(i) for i in selected])
        else:
            rows, _ = await db.query_leads(
                intent_levels=levels, has_contact=has_contact, limit=20
            )

        if not rows:
            ui.notify("没有符合条件的线索可推送", type="warning")
            return

        # 只推送配置允许的等级
        push_levels = config.notify.push_levels or ["high", "medium"]
        rows = [r for r in rows if (r.get("intent_level") or "none") in push_levels]
        if not rows:
            ui.notify(f"没有等级在 {push_levels} 内的线索", type="warning")
            return

        ok, err = await notifier.push_leads(rows, title="购车意向线索")
        if ok:
            if config.notify.mark_pushed:
                await db.mark_leads_pushed([r["id"] for r in rows])
            ui.notify(f"已推送 {len(rows)} 条线索到飞书", type="positive")
            await _do_query()
        else:
            log.error(f"飞书推送失败: {err}")
            ui.notify(f"飞书推送失败: {err}", type="negative")

    async def _do_sync_bitable():
        """把线索同步到飞书多维表格

        复用 lead_service.sync_leads_to_bitable —— 它带状态守卫，
        不会把销售在飞书表里改过的「处理状态」覆盖回默认值。
        """
        db = get_db()
        from ..intelligence.lead_service import sync_leads_to_bitable

        selected = lead_table.selected or []
        levels = [level_filter.value] if level_filter.value else None
        has_contact = True if contact_filter.value == "yes" else None

        if selected:
            rows = await db.get_leads_by_ids([int(i) for i in selected])
        else:
            rows, _ = await db.query_leads(
                intent_levels=levels, has_contact=has_contact, limit=100
            )
        if not rows:
            ui.notify("没有符合条件的线索可同步", type="warning")
            return

        ui.notify(f"正在同步 {len(rows)} 条线索到多维表格...")

        # 转成 Lead 对象，走统一的同步路径（带状态守卫 + record_id 回写）
        from ..models.lead import Lead

        leads = []
        for r in rows:
            r = dict(r)
            try:
                leads.append(Lead(**{k: v for k, v in r.items() if k in Lead.model_fields}))
            except Exception as e:
                log.warning(f"线索 {r.get('id')} 转换失败，跳过: {e}")

        try:
            result = await sync_leads_to_bitable(leads, db=db, config=config)
        except Exception as e:
            log.error(f"多维表格同步失败: {e}", exc_info=True)
            ui.notify(f"同步失败: {e}", type="negative")
            return

        if result.get("error"):
            ui.notify(f"同步失败: {result['error']}", type="negative")
            return

        msg = (
            f"同步完成：新建 {result.get('created', 0)} 条，"
            f"更新 {result.get('updated', 0)} 条，失败 {result.get('failed', 0)} 条"
        )
        ui.notify(msg, type="positive" if not result.get("failed") else "warning")
        await _do_query()

    async def _do_pull_status():
        """把飞书表里销售改过的状态拉回本地"""
        db = get_db()
        from ..intelligence.lead_service import pull_bitable_status

        ui.notify("正在从飞书表拉取状态...")
        try:
            result = await pull_bitable_status(db=db, config=config)
        except Exception as e:
            log.error(f"拉取飞书状态失败: {e}", exc_info=True)
            ui.notify(f"拉取失败: {e}", type="negative")
            return

        if result.get("error"):
            ui.notify(f"拉取失败: {result['error']}", type="warning")
            return
        msg = f"已同步 {result.get('updated', 0)} 条状态变更"
        ui.notify(msg, type="positive")
        await _do_query()

    async def _bulk_status(new_status: str):
        db = get_db()
        selected = [int(i) for i in (lead_table.selected or [])]
        if not selected:
            ui.notify("请先选择线索", type="warning")
            return
        for lid in selected:
            await db.update_lead(lid, status=new_status)
        ui.notify(f"已更新 {len(selected)} 条线索", type="positive")
        await _do_query()

    async def _bulk_add_outreach():
        db = get_db()
        selected = [int(i) for i in (lead_table.selected or [])]
        if not selected:
            ui.notify("请先选择线索", type="warning")
            return

        added = 0
        skipped = 0
        for row in await db.get_leads_by_ids(selected):
            if not row.get("display_id"):
                skipped += 1
                continue
            if row.get("outreach_status") in ("commented", "searching", "found"):
                skipped += 1
                continue
            await db.update_lead(
                row["id"], outreach_status="pending", outreach_note=""
            )
            added += 1

        msg = f"已加入触达队列 {added} 条"
        if skipped:
            msg += f"，跳过 {skipped} 条（无抖音号或已在队列中）"
        ui.notify(msg, type="positive")
        await _do_query()

    async def _do_outreach():
        db = get_db()
        from ..outreach.runner import get_runner

        runner = get_runner()
        limit = int(outreach_limit.value or config.outreach.max_per_run)
        message = outreach_msg.value.strip() or ""

        outreach_status.text = "触达任务启动中..."
        try:
            result = await runner.run(limit=limit, message_override=message)
        except Exception as e:
            log.error(f"触达任务失败: {e}", exc_info=True)
            outreach_status.text = f"触达失败: {e}"
            ui.notify(f"触达失败: {e}", type="negative")
            return

        outreach_status.text = (
            f"触达完成：处理 {result.get('total', 0)} 条，"
            f"成功 {result.get('success', 0)}，失败 {result.get('failed', 0)}，"
            f"跳过 {result.get('skipped', 0)}"
        )
        ui.notify(outreach_status.text, type="positive" if result.get("success") else "warning")

        rows, _ = await db.query_outreach_records(limit=100)
        for r in rows:
            r["status"] = OUTREACH_LABELS.get(r.get("status"), r.get("status"))
            r["message"] = (r.get("message") or "")[:50]
            r["target_url"] = (r.get("target_url") or "")[:60]
            r["error"] = (r.get("error") or "")[:60]
        outreach_table.rows = rows
        outreach_table.update()
        await _do_query()

    with ui.element("div").classes("app-card w-full q-pa-md page-enter"):

        # ========== 顶部状态 ==========
        with ui.row().classes("w-full items-center q-col-gutter-sm q-mb-md"):
            ui.label("线索中心").classes("section-title")
            ui.space()
            status_label = ui.label("").classes("text-caption text-grey-7")

        def _refresh_status():
            llm = config.llm
            llm_ok = llm.enabled and bool(llm.api_key)
            notify = get_notifier(config)
            bits = [
                f"LLM: {'✅ ' + llm.model if llm_ok else '❌ 未配置'}",
                f"飞书: {'✅ 已配置' if notify.is_configured else '❌ 未配置'}",
            ]
            status_label.text = "  |  ".join(bits)

        _refresh_status()

        # ========== 操作区 ==========
        with ui.row().classes("w-full items-end q-col-gutter-sm q-mb-md"):
            lookback_select = ui.select(
                options={
                    "3": "最近 3 小时",
                    "6": "最近 6 小时",
                    "12": "最近 12 小时",
                    "24": "最近 24 小时",
                    "72": "最近 3 天",
                },
                value=str(config.lead.lookback_hours),
                label="扫描回溯范围",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            min_level_select = ui.select(
                options={
                    "high": "仅高意向",
                    "medium": "中意向及以上",
                    "low": "低意向及以上",
                },
                value=config.lead.min_intent_level,
                label="最低意向等级",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            scan_btn = ui.button("扫描购车意向", on_click=_do_scan).classes(
                "col-6 col-sm-2"
            ).props("unelevated icon=radar")
            ui.button("推送到飞书", on_click=_do_push).classes("col-6 col-sm-2").props(
                "unelevated icon=send color=accent"
            )

        scan_result = ui.label("").classes("text-caption q-mb-md")

        # ========== 筛选 ==========
        with ui.row().classes("w-full items-end q-col-gutter-sm q-mb-sm"):
            level_filter = ui.select(
                options={
                    "high": "高意向",
                    "medium": "中意向",
                    "low": "低意向",
                    "": "不限等级",
                },
                value="high",
                label="意向等级",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            status_filter = ui.select(
                options={"": "全部状态", "new": "待处理", "pushed": "已推送",
                         "contacted": "已跟进", "ignored": "已忽略"},
                value="",
                label="处理状态",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            contact_filter = ui.select(
                options={"": "全部", "yes": "仅看有联系方式", "no": "仅看无联系方式"},
                value="",
                label="联系方式",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            store_filter = ui.select(
                options=_store_options(),
                value="",
                label="门店",
            ).props("dense outlined clearable").classes("col-6 col-sm-2 app-input")

            keyword_input = ui.input("搜索昵称/抖音号").props(
                "dense outlined clearable"
            ).classes("col-6 col-sm-3 app-input")

            ui.button("查询", on_click=_do_query).classes("col-6 col-sm-1").props(
                "flat dense"
            )

        # ========== 线索表格 ==========
        lead_table = ui.table(
            columns=[
                {"name": "id", "label": "ID", "field": "id", "align": "center", "sortable": True},
                {"name": "nickname", "label": "昵称", "field": "nickname", "align": "left"},
                {"name": "display_id", "label": "抖音号", "field": "display_id", "align": "left"},
                {"name": "intent_level", "label": "意向", "field": "intent_level", "align": "center"},
                {"name": "intent_score", "label": "分数", "field": "intent_score", "align": "center", "sortable": True},
                {"name": "store", "label": "门店", "field": "store", "align": "left"},
                {"name": "car_model", "label": "车型", "field": "car_model", "align": "left"},
                {"name": "contact_value", "label": "联系方式", "field": "contact_value", "align": "left"},
                {"name": "intent_reason", "label": "判定依据", "field": "intent_reason", "align": "left"},
                {"name": "source", "label": "来源", "field": "source", "align": "center"},
                {"name": "status", "label": "状态", "field": "status", "align": "center"},
                {"name": "outreach_status", "label": "触达", "field": "outreach_status", "align": "center"},
            ],
            rows=[],
            row_key="id",
            selection="multiple",
            on_select=_on_select,
        ).classes("w-full").props("dense flat bordered wrap-cells")

        selected_label = ui.label("已选 0 条").classes("text-caption q-my-sm")

        with ui.row().classes("w-full items-center q-gutter-sm q-mb-md"):
            ui.button("标记为已跟进", on_click=lambda: _bulk_status("contacted")).props("flat dense")
            ui.button("忽略", on_click=lambda: _bulk_status("ignored")).props("flat dense")
            ui.button("加入触达队列", on_click=_bulk_add_outreach).props(
                "flat dense icon=send_to_mobile"
            )
            ui.button("同步到多维表格", on_click=_do_sync_bitable).props(
                "flat dense icon=table_chart"
            )
            ui.button("拉取飞书状态", on_click=_do_pull_status).props(
                "flat dense icon=cloud_download"
            )
            ui.button("刷新", on_click=_do_query).props("flat dense icon=refresh")

        # ========== 触达队列页签 ==========
        with ui.expansion("抖音触达队列（第二步：搜人 + 作品留言）", value=False).classes(
            "w-full q-mt-md"
        ):
            with ui.column().classes("w-full q-gutter-sm"):
                ui.label(
                    "在抖音真实界面搜索线索的抖音号，定位其主页并给作品留言。"
                    "启用了限速与每日上限，失败会自动停止。"
                ).classes("text-caption text-grey-7")

                with ui.row().classes("w-full items-end q-col-gutter-sm"):
                    outreach_limit = ui.number(
                        "本轮最多处理", value=config.outreach.max_per_run, min=1, max=50
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")
                    outreach_msg = ui.input("留言文案（留空则用模板）").props(
                        "dense outlined clearable"
                    ).classes("col-12 col-sm-6 app-input")
                    ui.button("开始触达", on_click=_do_outreach).classes("col-6 col-sm-2").props(
                        "unelevated icon=play_arrow"
                    )

                outreach_status = ui.label("").classes("text-caption")
                outreach_table = ui.table(
                    columns=[
                        {"name": "created_at", "label": "时间", "field": "created_at", "align": "center"},
                        {"name": "nickname", "label": "昵称", "field": "nickname", "align": "left"},
                        {"name": "display_id", "label": "抖音号", "field": "display_id", "align": "left"},
                        {"name": "status", "label": "状态", "field": "status", "align": "center"},
                        {"name": "target_url", "label": "目标主页", "field": "target_url", "align": "left"},
                        {"name": "message", "label": "留言内容", "field": "message", "align": "left"},
                        {"name": "error", "label": "错误", "field": "error", "align": "left"},
                    ],
                    rows=[],
                    row_key="id",
                ).classes("w-full").props("dense flat bordered wrap-cells")

    # ============================================================

    # 打开页面时自动加载一次线索
    ui.timer(0.3, _do_query, once=True)
