"""抖音触达 — 第二步「搜抖音号 + 作品留言」的控制台

为什么单独一个页面
----------------
之前它是线索中心页面底部的一个展开面板（``ui.expansion``），
线索表格又高又长（判定依据那列会换行），面板被压在四屏开外——
用户根本找不到入口，「开始触达」按钮也就没地方点。

现在它是顶部导航里的独立入口，进来第一眼就是进度、实时日志和每条线索的处理详情。
"""

from __future__ import annotations

from nicegui import ui

from ..config import get_config
from ..utils.logger import get_logger

log = get_logger("outreach_page")

config = get_config()

LEVEL_LABELS = {
    "high": "高意向",
    "medium": "中意向",
    "low": "低意向",
}

OUTREACH_LABELS = {
    "pending": "待触达",
    "searching": "搜索中",
    "found": "已定位",
    "commented": "已留言",
    "failed": "失败",
    "skipped": "已跳过",
    "dry_run": "干跑",
}


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


def create_outreach_page():
    """创建抖音触达控制台页面"""

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

    with ui.element("div").classes("app-card w-full q-pa-md page-enter"):

        # ========== 顶部状态 ==========
        with ui.row().classes("w-full items-center q-col-gutter-sm q-mb-md"):
            ui.label("抖音触达（第二步：搜人 + 作品留言）").classes("section-title")
            ui.space()
            ui.label("在抖音上搜抖音号 → 定位到人 → 给他作品留言").classes("text-caption text-grey-7")

        with ui.element("div").classes("w-full"):
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
