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

# 撞验证码时等人工验证多久（页面上要显示这个数）
captcha_wait_minutes = int(config.outreach.captcha_wait_minutes or 15)

LEVEL_LABELS = {
    "high": "高意向",
    "medium": "中意向",
    "low": "低意向",
}

# 停手横幅要显示哪几种情况。
# ⚠️ 词汇必须跟后端一致（verify / limit / login / search）。
# 以前这里写的是 "captcha"，后端发的是 "verify" → 撞验证时横幅压根不显示，
# 用户只看到一行日志，不知道发生了什么、也不知道该干什么。
#
# search = 抖音**网页版搜索接口**被限流（2026-11-12 加）：
# 账号是好的、扫码没用、也不该锁 120 分钟冷却——
# 该做的是少搜索、等一等，别把人往错误方向指引。
STOP_KINDS = ("limit", "login", "verify", "search")

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

    async def _do_resume():
        """人工验证完了，让触达接着跑"""
        from ..outreach.runner import get_outreach_runner

        runner = get_outreach_runner()
        if runner.resume():
            ui.notify("已继续", type="positive")
        else:
            ui.notify("现在没有在等验证", type="warning")

    async def _do_show_window():
        """把浏览器窗口叫到屏幕中间，让用户自己去操作验证

        平时窗口藏在屏幕外（看不见），撞验证码时用户往往找不到它。
        这里发个信号给 worker 线程（浏览器归那个线程管，主线程碰不了），
        由它把窗口挪回来。
        """
        from ..outreach.runner import get_outreach_runner

        runner = get_outreach_runner()
        if runner.show_browser_window():
            ui.notify("窗口正在叫到屏幕中间，去那个窗口里验证", type="positive")
        else:
            ui.notify("现在没有在等验证", type="warning")

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

        # 本轮临时间隔（留空 = 用配置里的）
        def _num(v):
            try:
                return float(v) if v not in (None, "") else None
            except Exception:
                return None

        delay_min = _num(outreach_delay_min.value)
        delay_max = _num(outreach_delay_max.value)
        if delay_min is not None and delay_max is not None and delay_max < delay_min:
            delay_min, delay_max = delay_max, delay_min

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
            result = await runner.run(limit=limit, dry_run=dry,
                                      delay_min=delay_min, delay_max=delay_max,
                                      sort_by=str(outreach_sort_by.value or "recent"))
        except Exception as e:
            log.error(f"触达任务失败: {e}", exc_info=True)
            outreach_status.text = f"触达失败: {e}"
            ui.notify(f"触达失败: {e}", type="negative")
            return

        sent = result.get("sent", 0)
        dryn = result.get("dry", 0)
        failed = result.get("failed", 0)
        skipped = result.get("skipped", 0)
        not_found = result.get("not_found", 0)
        stopped = result.get("stopped") or ""

        msg = (
            f"处理 {result.get('total', 0)} 条　"
            + (f"干跑 {dryn} 条　" if dryn else "")
            + f"已留言 {sent} 条　失败 {failed} 条　跳过 {skipped} 条"
        )
        # 「搜不到人」单列出来——它不是失败，是这些抖音号本来就失效了
        if not_found:
            msg += f"（其中搜不到人 {not_found} 条）"
        if stopped:
            msg += f"　｜{stopped}"
        outreach_status.text = msg

        # 把运行日志灌进来
        for line in runner.recent_logs():
            outreach_log.push(line)

        if result.get("need_login"):
            ui.notify("需要登录：请点「扫码登录」扫码", type="negative")
        elif result.get("error"):
            # 冷却中 / 撞风控 → 说清楚为什么，别只丢一句「失败」
            ui.notify(str(result["error"])[:120], type="negative")
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

                # ---- 自动触达：不守着也能跑 ----
                # 用户要求「能自动取做触达，不用我在手动跑了，有可以设置自动」。
                # 开关、间隔、干跑/真发都在这儿当场调，不用去配置页。
                with ui.element("div").classes("w-full q-mt-sm q-pa-md").style(
                    "background:#f0f7ff; border:1px solid #7aa7e0; border-radius:8px;"
                ) as auto_box:
                    with ui.row().classes("w-full items-center q-gutter-sm"):
                        ui.label("🤖 自动触达").classes("text-body2 font-weight-bold")

                        auto_switch = ui.switch(
                            "开启",
                            value=bool(config.outreach.auto_mode),
                        )

                        auto_interval = ui.number(
                            "每隔(分钟)",
                            value=int(config.outreach.auto_interval_minutes or 60),
                            min=5, max=1440, step=5,
                        ).props("dense outlined").classes("col-4 col-sm-2 app-input")

                        auto_max_run = ui.number(
                            "每轮最多",
                            value=int(config.outreach.auto_max_per_run or 10),
                            min=1, max=50, step=1,
                        ).props("dense outlined").classes("col-4 col-sm-2 app-input")

                        auto_dry_switch = ui.switch(
                            "干跑（不真发）",
                            value=bool(config.outreach.auto_dry_run),
                        )

                    auto_status = ui.label("").classes("text-caption text-grey-7 q-mt-xs")

                    ui.label(
                        "⚠️ 自动真发 = 没人值守。抖音随时可能要求「本人操作验证」，"
                        "撞上了会飞书叫你、停下来等你验证（不会自动发出去）。"
                        "另外：多台机器都开自动触达会**重复留言**——去重只看本机记录，"
                        "看不见另一台机器发过什么。"
                    ).classes("text-caption text-grey-7 q-mt-xs").style("line-height:1.7;")

                def _refresh_auto_status():
                    """刷新自动触达状态（上次/下次/在等什么）"""
                    from ..outreach.auto_scheduler import get_auto_scheduler

                    try:
                        st = get_auto_scheduler().status()
                    except Exception as e:
                        auto_status.text = f"（读取自动触达状态失败：{e}）"
                        return

                    if not st["configured"]:
                        auto_status.text = "自动触达：未开启"
                        return
                    if not st["enabled"]:
                        auto_status.text = (
                            "自动触达：已打开开关，但「启用抖音触达」没开 → 不会跑"
                        )
                        return

                    mode = "干跑" if st["dry_run"] else "真发"
                    if st["running"]:
                        # 正在跑的时候 last_run_at 还是上一轮的，
                        # 直接说「还没跑过」会让人以为开关没生效
                        bits = [f"自动触达：⏳ 正在跑本轮（{mode}）"]
                    else:
                        bits = [f"自动触达：运行中（{mode}）"]
                    if st["last_run_at"]:
                        r = st.get("last_result") or {}
                        detail = (
                            f"发 {r.get('sent', 0)}　干跑 {r.get('dry', 0)}　"
                            f"失败 {r.get('failed', 0)}"
                        )
                        bits.append(f"上次 {st['last_run_at']}（{detail}）")
                    elif not st["running"]:
                        bits.append("还没跑过")
                    if st["next_run_at"]:
                        bits.append(f"下次 {st['next_run_at']}")
                    if st["cooldown"]:
                        bits.append(f"⚠️ 限流冷却中，还剩 {st['cooldown']} 分钟，等冷却完再跑")
                    elif st["last_error"]:
                        bits.append(f"（{st['last_error']}）")
                    auto_status.text = "　".join(bits)

                def _save_auto_settings():
                    """保存自动触达设置"""
                    from ..config import get_config, save_config, reload_config

                    cfg = get_config()
                    cfg.outreach.auto_mode = bool(auto_switch.value)
                    cfg.outreach.auto_dry_run = bool(auto_dry_switch.value)
                    try:
                        cfg.outreach.auto_interval_minutes = int(
                            auto_interval.value or 60
                        )
                    except Exception:
                        pass
                    try:
                        cfg.outreach.auto_max_per_run = int(auto_max_run.value or 10)
                    except Exception:
                        pass
                    save_config(cfg)
                    reload_config()
                    _refresh_auto_status()

                def _toggle_auto():
                    """开关变化：开着就立刻跑一轮，别让人干等一个周期"""
                    from ..outreach.auto_scheduler import get_auto_scheduler

                    _save_auto_settings()
                    if auto_switch.value:
                        get_auto_scheduler().trigger_now()
                        ui.notify("自动触达已开启，马上跑第一轮", type="positive")
                    else:
                        ui.notify("自动触达已关闭", type="info")
                    _refresh_auto_status()

                auto_switch.on_value_change(_toggle_auto)
                auto_dry_switch.on_value_change(_save_auto_settings)
                auto_interval.on_value_change(_save_auto_settings)
                auto_max_run.on_value_change(_save_auto_settings)
                _refresh_auto_status()

                # 现在两个刷新函数都定义好了，统一刷一次
                _refresh_outreach_info()

                with ui.row().classes("w-full items-end q-col-gutter-sm"):
                    outreach_limit = ui.number(
                        "本轮最多处理", value=config.outreach.max_per_run, min=1, max=50
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")

                    # 两条之间的间隔：在这儿当场调，不用每次去配置页。
                    # 留空 = 用配置里的 min_delay / max_delay。
                    outreach_delay_min = ui.number(
                        "最小间隔(秒)", value=None, min=0, max=3600, step=5
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")
                    outreach_delay_max = ui.number(
                        "最大间隔(秒)", value=None, min=0, max=3600, step=5
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")

                    # 排序：默认「最近抓到优先」
                    # （直播间里刚问完价的人最热乎，可能人就在直播间等着回复）
                    outreach_sort_by = ui.select(
                        options={
                            "recent": "最近抓到优先",
                            "intent": "高意向优先",
                        },
                        value=str(config.outreach.sort_by or "recent"),
                    ).props("dense outlined").classes("col-6 col-sm-2 app-input")
                    outreach_sort_by.label = "排序"

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

                ui.label(
                    f"间隔留空 = 用配置里的 {int(config.outreach.min_delay)}-{int(config.outreach.max_delay)} 秒；"
                    "调小跑得快，但抖音更容易要求验证。"
                ).classes("text-caption text-grey-7 q-mt-xs")

                outreach_status = ui.label("").classes("text-caption")

                # ---- 停手横幅：触达被风控/登录挡住时，必须一眼看到 + 知道怎么办 ----
                # 之前这些情况只有一行日志，网页上一片安静，
                # 用户既不知道发生了什么，也不知道该干什么（以为要扫码，其实不用）。
                with ui.element("div").classes("w-full q-mt-sm q-pa-md").style(
                    "background:#fdecec; border:1px solid #e05252; border-radius:8px;"
                ) as stop_box:
                    stop_box.set_visibility(False)
                    with ui.row().classes("w-full items-center q-gutter-sm"):
                        stop_text = ui.label("").classes("col")
                        ui.space()
                        stop_login_btn = ui.button("扫码登录", on_click=_do_login).props(
                            "unelevated color=negative"
                        )
                        stop_login_btn.set_visibility(False)
                        stop_dismiss = ui.button("知道了", on_click=lambda: stop_box.set_visibility(False)).props(
                            "flat dense"
                        )
                        stop_dismiss.set_visibility(False)

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

                    # ---- 撞验证码时的等待条 ----
                    # 撞上验证码 → 飞书通知你 + 停在这儿等你
                    with ui.element("div").classes("w-full q-mt-sm q-pa-md").style(
                        "background:#fff4e5; border:1px solid #f0a04b; border-radius:8px;"
                    ) as captcha_box:
                        captcha_box.set_visibility(False)
                        with ui.row().classes("w-full items-center q-gutter-sm"):
                            captcha_text = ui.label("").classes("col")
                            ui.space()
                            # 自己把窗口叫出来：平时窗口藏在屏幕外看不见，
                            # 点这个按钮它会回到屏幕中间，你自己操作验证。
                            ui.button("打开浏览器窗口", on_click=_do_show_window).props(
                                "outline color=warning"
                            )
                            ui.button("我验证好了，继续", on_click=_do_resume).props(
                                "unelevated color=warning"
                            )

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
                    "captcha_wait": "等验证", "captcha_resumed": "已继续",
                    "captcha_timeout": "验证超时",
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
                        # 只有真限流才会 >0（要本人验证不算限流）
                        bits.append(f"🛑 账号限流冷却剩 {cooldown} 分钟")
                    if p.get("waiting_human"):
                        bits.append("⏸ 等你人工验证")
                    if p.get("sent"):
                        bits.append(f"已留言 {p['sent']}")
                    if p.get("dry"):
                        bits.append(f"干跑 {p['dry']}")
                    if p.get("failed"):
                        bits.append(f"失败 {p['failed']}")
                    if p.get("skipped"):
                        bits.append(f"跳过 {p['skipped']}")
                    console_state.text = "　｜　".join(bits)

                    # 撞验证码时的等待条：显示/隐藏 + 文案
                    waiting = bool(p.get("waiting_human"))
                    captcha_box.set_visibility(waiting)
                    if waiting:
                        reason = p.get("captcha_reason") or ""
                        captcha_text.text = (
                            f"⏸ 撞上验证码，已暂停等你人工验证。{reason}\n"
                            "点「打开浏览器窗口」把它叫到屏幕中间，你自己把短信/拖拽图形过掉——"
                            "验证完它会自动接着跑，不用再点「继续」。\n"
                            "（点「我验证好了，继续」也行；超过 "
                            f"{captcha_wait_minutes} 分钟没人管就结束本轮）"
                        )

                    # ---- 停手横幅（限流 / 要验证 / 登录失效）----
                    # 这三种不是「等一下就好」，必须说清楚到底怎么了、该干什么。
                    stop = p.get("risk_stop") or {}
                    kind = stop.get("kind") or ""
                    # ⚠️ 词汇统一成 verify / limit / login（以前这里写的是 "captcha"，
                    # 跟后端对不上 → verify 停手时横幅压根不显示）
                    show_stop = kind in STOP_KINDS and not p.get("running")
                    stop_box.set_visibility(show_stop)
                    if show_stop:
                        stop_login_btn.set_visibility(kind == "login")
                        stop_dismiss.set_visibility(True)
                        reason = stop.get("reason") or ""
                        if kind == "login":
                            stop_text.text = (
                                "🔑 登录态失效，触达跑不动了（搜不到人也发不了评论）。\n"
                                f"原因：{reason}\n"
                                "点右边「扫码登录」→ 用抖音 App 扫一下 → 回来再点「开始触达」。"
                            )
                        elif kind == "verify":
                            stop_text.text = (
                                "⏸ 抖音要求验证本人操作，但这次没人处理，本轮已经结束了。\n"
                                f"原因：{reason}\n"
                                "这不是账号被限流（账号是好的），验证一下就能继续：\n"
                                "1. 重新点「开始触达」\n"
                                "2. 撞上验证时，点「打开浏览器窗口」把它叫到屏幕中间\n"
                                "3. 用抖音 App 收短信 / 扫码把验证过掉，"
                                "过完它会自动接着跑"
                            )
                        elif kind == "search":
                            stop_text.text = (
                                "🔍 抖音**网页版搜索**被限流了，已停手。\n"
                                f"原因：{reason}\n"
                                "你的账号是好的，手机上也搜得到——限流的是网页版的搜索接口，\n"
                                "它不报错、不提示，就是给你一个空的搜索结果。\n"
                                "扫码没用，也不用换号。怎么办：\n"
                                "1. 别反复重试搜索，等 30 分钟到几小时它自己会恢复\n"
                                "2. 系统已经记下每个线索的抖音 ID，下次不用再搜，"
                                "直接开主页留言（所以搜不到也不耽误触达）\n"
                                "3. 急着要的话，用抖音 App 手动发一条（App 端不受影响）"
                            )
                        else:
                            stop_text.text = (
                                "🛑 账号被限流（软风控），已停手。\n"
                                f"原因：{reason}\n"
                                "评论发出去了、也没报错，但页面上看不到——抖音把它静默吞了。\n"
                                "扫码没用，这不是登录问题。怎么办：\n"
                                "1. 冷却期内别再发（还剩 "
                                f"{cooldown} 分钟）\n"
                                "2. 用抖音 App 手动发一条试试：能发出去 = 等冷却完再跑；"
                                "也发不出去 = 账号确实被限流，等抖音解封（几小时到一天）\n"
                                "3. 还是不行就换个抖音号"
                            )

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
                    _refresh_auto_status()

                # 页面打开时加载一次
                ui.timer(0.6, _load_outreach_views, once=True)
