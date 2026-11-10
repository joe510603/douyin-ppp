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


async def _confirm_send(limit: int) -> bool:
    """真发前的二次确认

    这一步会真的在抖音上留下评论，撤不回来，所以必须让用户明确确认。
    返回 True = 确认发送，False = 取消。

    用 NiceGUI 原生的 dialog.submit() + await：
    确认按钮 submit(True)，取消按钮 submit(False)。
    """
    with ui.dialog() as dialog, ui.card().classes("q-pa-lg"):
        ui.label("确认真实发送").classes("text-h6 q-mb-sm")
        ui.label(
            f"即将在抖音上真实留言 {limit} 条。\n\n"
            "这一步会在客户的作品下面留下评论，撤不回来。\n"
            "确认话术没问题、限速和每日上限都设好了吗？"
        ).classes("text-body2 q-mb-lg")
        with ui.row().classes("w-full justify-end gap-sm"):
            ui.button("再想想", on_click=lambda: dialog.submit(False)).props("flat")
            ui.button("确认发送", on_click=lambda: dialog.submit(True)).props(
                "unelevated color=negative"
            )
    dialog.open()
    result = await dialog
    return bool(result)


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

    async def _do_login():
        """扫码登录触达专用浏览器

        抖音的搜索和发评论都要求登录态（cookie 里有 sessionid）。
        点一下会弹出浏览器窗口，扫码后自动检测到。
        """
        from ..outreach.runner import get_outreach_runner

        runner = get_outreach_runner()
        outreach_status.text = "正在打开浏览器，请在弹出的窗口里扫码登录…"
        outreach_log.clear()
        try:
            result = await runner.login(timeout=300)
        except Exception as e:
            log.error(f"扫码登录失败: {e}", exc_info=True)
            outreach_status.text = f"登录失败：{e}"
            ui.notify(f"登录失败：{e}", type="negative")
            return

        for line in runner.recent_logs():
            outreach_log.push(line)

        if result.get("ok"):
            outreach_status.text = "登录成功，可以开始触达了"
            ui.notify("登录成功", type="positive")
        else:
            outreach_status.text = result.get("message") or "登录未完成"
            ui.notify(outreach_status.text, type="warning")

    async def _do_outreach():
        """跑一轮触达任务

        触达跑在专用线程里（Playwright sync API 不能跨线程），
        这里只是发起 + 等结果，主页面照常响应。
        """
        from ..outreach.runner import get_outreach_runner

        runner = get_outreach_runner()
        limit = int(outreach_limit.value or config.outreach.max_per_run)
        dry = bool(dry_switch.value)

        if not dry and not config.outreach.enabled:
            ui.notify("触达功能未启用，请先到「配置管理 → 抖音触达」打开", type="warning")
            return
        if not dry:
            # 真发前必须二次确认——这一步会真的在抖音上留下评论，撤不回来
            confirmed = await _confirm_send(limit)
            if not confirmed:
                return

        outreach_status.text = "触达任务运行中…（可继续用页面其它功能）"
        outreach_log.clear()
        try:
            result = await runner.run(limit=limit, dry_run=dry)
        except Exception as e:
            log.error(f"触达任务失败: {e}", exc_info=True)
            outreach_status.text = f"触达失败: {e}"
            ui.notify(f"触达失败: {e}", type="negative")
            return

        sent = result.get("sent", 0)
        dryn = result.get("dry", 0)
        failed = result.get("failed", 0)
        skipped = result.get("skipped", 0)
        stopped = result.get("stopped") or ""

        msg = (
            f"处理 {result.get('total', 0)} 条　"
            + (f"干跑 {dryn} 条　" if dryn else "")
            + f"已留言 {sent} 条　失败 {failed} 条　跳过 {skipped} 条"
        )
        if stopped:
            msg += f"　｜{stopped}"
        outreach_status.text = msg

        # 把运行日志灌进来
        for line in runner.recent_logs():
            outreach_log.push(line)

        if result.get("need_login"):
            ui.notify("需要登录：请先扫码登录触达专用浏览器", type="warning")
        elif failed:
            ui.notify(msg, type="warning")
        else:
            ui.notify(msg, type="positive")

        await _load_outreach_views()
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
        with ui.expansion("抖音触达队列（第二步：搜人 + 作品留言）", value=True).classes(
            "w-full q-mt-md"
        ):
            with ui.column().classes("w-full q-gutter-sm"):
                outreach_info = ui.label("").classes("text-caption text-grey-7")

                def _refresh_outreach_info():
                    from ..outreach.runner import get_outreach_runner

                    try:
                        r = get_outreach_runner()
                        o = config.outreach
                        pending = r.pending_count()
                        sent = r.sent_today()
                        mode = "干跑" if o.dry_run else "真发"
                        outreach_info.text = (
                            f"待触达 {pending} 条　今日已发 {sent}/{o.daily_limit}　"
                            f"模式：{mode}　"
                            f"间隔 {int(o.min_delay)}-{int(o.max_delay)} 秒"
                        )
                    except Exception as e:
                        outreach_info.text = f"（读取触达状态失败：{e}）"

                _refresh_outreach_info()

                with ui.row().classes("w-full items-end q-col-gutter-sm"):
                    outreach_limit = ui.number(
                        "本轮最多处理", value=config.outreach.max_per_run, min=1, max=50
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")

                    dry_switch = ui.switch(
                        "干跑（只生成话术，不发送）",
                        value=config.outreach.dry_run,
                    ).classes("col-6 col-sm-3")

                    ui.button("开始触达", on_click=_do_outreach).classes("col-6 col-sm-2").props(
                        "unelevated icon=play_arrow"
                    )
                    ui.button("扫码登录", on_click=_do_login).classes("col-6 col-sm-2").props(
                        "flat dense icon=qr_code_scanner"
                    )
                    ui.button("刷新队列", on_click=_refresh_outreach_info).classes("col-6 col-sm-2").props(
                        "flat dense icon=refresh"
                    )

                outreach_status = ui.label("").classes("text-caption")

                # ---- 话术预览（不发任何东西，先看要说什么）----
                with ui.expansion("将要发出去的话术（预览）", value=True).classes("w-full"):
                    preview_table = ui.table(
                        columns=[
                            {"name": "display_id", "label": "抖音号", "field": "display_id", "align": "left"},
                            {"name": "nickname", "label": "昵称", "field": "nickname", "align": "left"},
                            {"name": "intent_level", "label": "意向", "field": "intent_level", "align": "center"},
                            {"name": "car_model", "label": "车型", "field": "car_model", "align": "left"},
                            {"name": "template_id", "label": "角度", "field": "template_id", "align": "center"},
                            {"name": "text", "label": "留言内容", "field": "text", "align": "left"},
                        ],
                        rows=[],
                        row_key="display_id",
                    ).classes("w-full").props("dense flat bordered wrap-cells")

                # ---- 控制台：进度 + 实时日志 + 每条线索的详情 ----
                with ui.expansion("触达控制台（进度 / 实时日志 / 处理详情）", value=True).classes(
                    "w-full"
                ):
                    console_state = ui.label("空闲").classes("text-caption text-grey-8")

                    console_bar = ui.linear_progress(value=0, show_value=False).classes("w-full")

                    console_log = ui.log(max_lines=400).classes("w-full")
                    console_log.style("max-height: 320px; overflow-y: auto")

                    console_table = ui.table(
                        columns=[
                            {"name": "time", "label": "时间", "field": "time", "align": "center"},
                            {"name": "result", "label": "结果", "field": "result", "align": "center"},
                            {"name": "display_id", "label": "抖音号", "field": "display_id", "align": "left"},
                            {"name": "nickname", "label": "昵称", "field": "nickname", "align": "left"},
                            {"name": "aweme_id", "label": "作品", "field": "aweme_id", "align": "left"},
                            {"name": "text", "label": "话术 / 说明", "field": "text", "align": "left"},
                        ],
                        rows=[],
                        row_key="time_display_id",
                    ).classes("w-full").props("dense flat bordered wrap-cells")

                # 控制台状态：给 timer 用
                _console = {"pushed": 0, "rows": []}

                _EVENT_LABELS = {
                    "start": "开始", "current": "处理中", "sent": "已留言",
                    "dry": "干跑", "failed": "失败", "skipped": "跳过",
                    "unshown": "已提交未展示", "risk": "风控", "need_login": "要登录",
                }

                def _console_tick():
                    """每秒刷新控制台：进度条 + 新日志 + 处理详情表"""
                    from ..outreach.runner import get_outreach_runner

                    try:
                        r = get_outreach_runner()
                    except Exception as e:
                        console_state.text = f"（读取触达状态失败：{e}）"
                        return

                    p = r.progress()
                    total = int(p.get("total") or 0)
                    done = int(p.get("done") or 0)
                    idx = int(p.get("index") or 0)
                    mode = p.get("mode") or ""
                    cooldown = int(p.get("cooldown") or 0)

                    bits = []
                    if p.get("running"):
                        bits.append("🟢 运行中")
                        if mode:
                            bits.append(f"模式 {mode}")
                        bits.append(f"第 {idx}/{total} 条")
                        if p.get("current"):
                            bits.append(f"正在处理 {p['current']}")
                    else:
                        bits.append("⚪ 空闲")
                        if total:
                            bits.append(f"上轮 {done}/{total} 条")
                        if mode:
                            bits.append(f"模式 {mode}")
                    if cooldown:
                        bits.append(f"🛑 风控冷却剩 {cooldown} 分钟")
                    if p.get("sent"):
                        bits.append(f"已留言 {p['sent']}")
                    if p.get("dry"):
                        bits.append(f"干跑 {p['dry']}")
                    if p.get("failed"):
                        bits.append(f"失败 {p['failed']}")
                    if p.get("skipped"):
                        bits.append(f"跳过 {p['skipped']}")
                    console_state.text = "　｜　".join(bits)

                    console_bar.value = min(1.0, done / total) if total else 0

                    # 日志增量推送
                    logs = r.recent_logs(limit=1000)
                    if len(logs) < _console["pushed"]:
                        console_log.clear()
                        _console["pushed"] = 0
                    for line in logs[_console["pushed"]:]:
                        console_log.push(line)
                    _console["pushed"] = len(logs)

                    # 处理详情表
                    rows = []
                    for ev in r.recent_events(limit=100):
                        rows.append({
                            "time": ev.get("time", ""),
                            "result": _EVENT_LABELS.get(ev.get("type"), ev.get("type", "")),
                            "display_id": ev.get("display_id", ""),
                            "nickname": ev.get("nickname", ""),
                            "aweme_id": (ev.get("aweme_id") or "")[:20],
                            "text": ev.get("text") or ev.get("error") or "",
                            "time_display_id": f"{ev.get('time','')}{ev.get('display_id','')}",
                        })
                    if rows != _console["rows"]:
                        _console["rows"] = rows
                        console_table.rows = rows
                        console_table.update()

                ui.timer(1.0, _console_tick)

                # ---- 运行日志 ----
                with ui.expansion("运行日志（任务结束后归档）", value=False).classes("w-full"):
                    outreach_log = ui.log(max_lines=300).classes("w-full")

                # ---- 触达记录 ----
                outreach_table = ui.table(
                    columns=[
                        {"name": "created_at", "label": "时间", "field": "created_at", "align": "center"},
                        {"name": "nickname", "label": "昵称", "field": "nickname", "align": "left"},
                        {"name": "display_id", "label": "抖音号", "field": "display_id", "align": "left"},
                        {"name": "status", "label": "状态", "field": "status", "align": "center"},
                        {"name": "aweme_id", "label": "作品", "field": "aweme_id", "align": "left"},
                        {"name": "message", "label": "留言内容", "field": "message", "align": "left"},
                        {"name": "error", "label": "错误", "field": "error", "align": "left"},
                    ],
                    rows=[],
                    row_key="id",
                ).classes("w-full").props("dense flat bordered wrap-cells")

                async def _load_outreach_views():
                    """刷新话术预览 + 触达记录 + 队列信息"""
                    from ..outreach.runner import get_outreach_runner

                    r = get_outreach_runner()
                    try:
                        previews = r.preview(limit=20)
                        for p in previews:
                            p["intent_level"] = LEVEL_LABELS.get(
                                p.get("intent_level"), p.get("intent_level")
                            )
                            p["car_model"] = p.get("car_model") or "-"
                            p["text"] = p.get("text") or ""
                        preview_table.rows = previews
                        preview_table.update()
                    except Exception as e:
                        log.warning(f"加载话术预览失败: {e}")

                    try:
                        rows = r.recent_touches(limit=100)
                        for row in rows:
                            row["status"] = OUTREACH_LABELS.get(
                                row.get("status"), row.get("status")
                            )
                            row["message"] = (row.get("message") or "")[:80]
                            row["error"] = (row.get("error") or "")[:60]
                            row["aweme_id"] = (row.get("aweme_id") or "")[:20]
                        outreach_table.rows = rows
                        outreach_table.update()
                    except Exception as e:
                        log.warning(f"加载触达记录失败: {e}")

                    _refresh_outreach_info()

                # 页面打开时加载一次
                ui.timer(0.6, _load_outreach_views, once=True)

    # ============================================================

    # 打开页面时自动加载一次线索
    ui.timer(0.3, _do_query, once=True)
