"""触达执行器 —— 找意向客户 → 在他作品下留言

这一层是**同步**的，跑在专用线程里（Playwright sync API 不能跨线程用，
而主进程是 asyncio 架构）。

流程（每条线索）：
    1. 按抖音号在抖音搜到这个人（精确匹配 unique_id / short_id）
    2. 拉他主页最近的作品，挑一条最近发过的
    3. 按弹幕内容和车型生成一条像真人写的留言
    4. 打开作品 → 填评论框 → 发送
    5. 回查：评论区里真的出现了才算「已留言」（抖音会静默删营销评论）
    6. 回写状态：本地 leads.outreach_status + 飞书表「触达状态」

安全阀（都在这里，不散落）：
    - 干跑默认开：不发送、不回写
    - 每日上限 + 单轮上限
    - 两条之间随机间隔，每 N 条长休息
    - 同一抖音号 N 天内不重复
    - 连续异常 N 次停手
    - 撞到验证码/登录失效立即停
"""

from __future__ import annotations

import json
import pathlib
import random
import time
from typing import Callable, Optional

from .browser import Browser, Session
from .copywriter import Copywriter
from .douyin_api import Douyin, DouyinError, NoWorks, NotLoggedIn, RiskControl
from .store import (
    STATUS_COMMENTED,
    STATUS_FAILED,
    STATUS_FOUND,
    STATUS_PENDING,
    STATUS_SEARCHING,
    STATUS_SKIPPED,
    OutreachStore,
)

# 触达状态 → 飞书表「触达状态」列的文案
FEISHU_STATUS = {
    STATUS_PENDING: "待触达",
    STATUS_SEARCHING: "搜索中",
    STATUS_FOUND: "已定位",
    STATUS_COMMENTED: "已留言",
    STATUS_FAILED: "失败",
    STATUS_SKIPPED: "已跳过",
}


def lead_to_copywriter_input(lead: dict) -> dict:
    """把 leads 行转成 copywriter 认识的字段名

    ⚠️ 关键：模板选择靠的是**弹幕原文**，不是「判定依据」。
    ``intent_reason`` 是"命中信号：价格询问、金融补贴"这种汇总，
    拿它选模板会答非所问——客户问提车时间却回"看到你问价格"。
    真正的原文在 ``evidence``（JSON 数组）。
    """
    danmaku = ""
    raw = lead.get("evidence") or ""
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                danmaku = " / ".join(str(x) for x in items if x)
        except (ValueError, TypeError):
            danmaku = raw
    else:
        danmaku = str(raw or "")

    return {
        "douyin_id": lead.get("display_id") or "",
        "nickname": lead.get("nickname") or "",
        "car_model": lead.get("car_model") or "",
        "store": lead.get("store") or "",
        "intent": lead.get("intent_level") or "",
        "score": lead.get("intent_score") or 0,
        "danmaku": danmaku,
        "evidence": "",
        "live_room": lead.get("monitor_name") or "",
    }


class OutreachWorker:
    """触达任务执行器（同步，跑在专用线程）"""

    def __init__(
        self,
        cfg,
        db_path: str | pathlib.Path,
        log: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.cfg = cfg
        self.log = log or (lambda m: print(m))
        self.on_event = on_event or (lambda e: None)
        self.st = OutreachStore(db_path)
        # copywriter 吃的是 dict 配置，这里把 OutreachConfig 转成它认的形状
        self.cw = Copywriter(self._copywriter_cfg(), log=self.log)
        self.stop_reason = ""
        self._dry = True
        # 连续「发出去但回查不到」的次数（软风控信号）
        self._silent_blocks = 0

    # ================================================================
    # 配置适配
    # ================================================================

    def _copywriter_cfg(self) -> dict:
        """把 OutreachConfig 转成 Copywriter 认识的 dict"""
        o = self.cfg.outreach
        return {
            "comment": {
                "max_chars": o.max_chars,
                "salutation": o.salutation,
                "salutation_pool": list(o.salutation_pool or []),
                "templates_file": o.templates_file,
                "allow_contact_words": o.allow_contact_words,
            },
            "dm": {"max_chars": 120},
        }

    # ================================================================
    # 工具
    # ================================================================

    def _emit(self, **kw) -> None:
        try:
            self.on_event(kw)
        except Exception:
            pass

    def _recycle_page(self) -> None:
        """把当前页清空，释放视频解码占的内存"""
        page = getattr(self, "_page", None)
        if page is None:
            return
        try:
            cur = page.url or ""
            if cur.startswith("about:"):
                return
            page.goto("about:blank", timeout=8000)
        except Exception:
            pass

    def _sleep(self, sec: float) -> None:
        """可被 stop_reason 打断的等待"""
        end = time.time() + sec
        while time.time() < end:
            if self.stop_reason:
                return
            time.sleep(min(1.0, end - time.time()))

    def _sleep_between(self) -> None:
        """两条之间的等待"""
        self._recycle_page()
        o = self.cfg.outreach
        if self._dry:
            sec = random.randint(3, 6)
            self.log(f"  ⏳ 干跑，等 {sec}s 看下一条")
            self._sleep(sec)
            return
        lo = int(o.min_delay or 45)
        hi = max(lo, int(o.max_delay or 120))
        sec = random.randint(lo, hi)
        self.log(f"  ⏳ 等 {sec}s 再找下一个（模拟真人节奏）")
        self._sleep(sec)

    def _pick_work(self, works: list[dict]) -> Optional[dict]:
        """挑一条留言的作品：优先最近 recent_days 天内发的，退而取最新一条"""
        if not works:
            return None
        days = int(self.cfg.outreach.recent_days or 120)
        now = time.time()
        fresh = [w for w in works if now - float(w.get("create_time") or 0) <= days * 86400]
        if not fresh:
            self.log(f"  ℹ️ 近 {days} 天没有新作品，用最新的一条")
            fresh = works[:3]
        fresh.sort(key=lambda w: float(w.get("create_time") or 0), reverse=True)
        return fresh[0] if fresh else None

    # ================================================================
    # 状态回写
    # ================================================================

    def _set_status(self, lead: dict, status: str, error: str = "") -> None:
        """回写本地 + 飞书

        干跑时**完全不改状态**（本地也不改）。
        干跑只是验证「能不能找到人、能不能挑到作品、话术长什么样」，
        一旦把状态写成 searching/found，这条线索就会卡在队列外——
        回拉也不会把它改回 pending（本地相对上次同步的值变了，走「跳过」分支），
        下一轮真跑时这些线索就再也不会被处理。
        多机共用一张飞书表时更危险：干跑会把另一台机器写的真实触达状态盖掉。
        """
        if self._dry:
            return

        try:
            self.st.set_outreach_status(lead["id"], status, error)
        except Exception as e:
            self.log(f"  ⚠️ 本地状态回写失败: {e}")

        try:
            self._push_feishu_status(lead, status)
        except Exception as e:
            # 飞书写失败不影响主流程继续跑
            self.log(f"  ⚠️ 飞书状态回写失败: {e}")

    def _push_feishu_status(self, lead: dict, status: str) -> None:
        """把「触达状态」写回飞书多维表格

        复用 notify.bitable 的写入器，用 patch_fields 只改这一个字段——
        不能用整体替换（会把销售改过的「处理状态」清掉）。
        """
        record_id = lead.get("bitable_record_id") or ""
        if not record_id:
            return
        from ..notify.bitable import get_bitable_writer

        writer = get_bitable_writer(self.cfg)
        if not writer.is_configured:
            return
        text = FEISHU_STATUS.get(status, status)
        import asyncio

        asyncio.run(writer.patch_fields(record_id, {"触达状态": text}))

    # ================================================================
    # 主流程
    # ================================================================

    def run(
        self,
        limit: Optional[int] = None,
        dry_run: Optional[bool] = None,
        only: Optional[list[str]] = None,
        use_session: bool = True,
    ) -> dict:
        """跑一轮触达

        Args:
            limit: 本轮最多处理几条（默认取配置 max_per_run）
            dry_run: 干跑（默认取配置 dry_run，**默认是 True**）
            only: 只处理指定的抖音号
            use_session: 复用常驻浏览器（省掉每次重开窗口的十几秒）
        """
        o = self.cfg.outreach
        dry = bool(o.dry_run if dry_run is None else dry_run)
        self._dry = dry
        self._silent_blocks = 0
        per_run = int(limit or o.max_per_run or 10)
        daily_limit = int(o.daily_limit or 30)

        sent_today = self.st.sent_today()
        if not dry and sent_today >= daily_limit:
            msg = f"今天已发 {sent_today} 条，达到每日上限 {daily_limit}，收工"
            self.log(msg)
            return {"ok": True, "stopped": msg, "total": 0, "sent": 0, "failed": 0, "skipped": 0}

        # ---- 1) 取名单 ----
        self.log("① 读取待触达线索…")
        leads = self.st.pending_leads(
            intent_levels=list(o.intent_levels or []),
            stores=list(o.stores or []),
            min_score=float(o.min_score if hasattr(o, "min_score") else 0),
        )
        dedupe_days = int(o.dedupe_days or 30)

        if only:
            only_set = {str(x).strip().lower() for x in only if str(x).strip()}
            leads = [ld for ld in leads if str(ld.get("display_id") or "").lower() in only_set]
            self.log(f"   只处理指定的 {len(leads)} 条")

        picked: list[dict] = []
        skipped = 0
        for ld in leads:
            if self.st.recent_touch_days(ld["display_id"], dedupe_days):
                skipped += 1
                continue
            picked.append(ld)
            if len(picked) >= per_run:
                break

        budget = per_run if dry else min(per_run, max(0, daily_limit - sent_today))
        picked = picked[:budget]

        self.log(
            f"   待触达 {len(leads)} 条 → 本次挑 {len(picked)} 条"
            f"（跳过 {skipped} 条：{dedupe_days} 天内已触达）；"
            f"今日已发 {sent_today}/{daily_limit}"
        )
        self._emit(type="start", total=len(picked), dry_run=dry)

        summary = {
            "ok": True, "total": len(picked), "sent": 0, "failed": 0,
            "skipped": skipped, "dry": 0, "dry_run": dry, "stopped": "",
        }
        if not picked:
            self.log("没有要处理的线索，结束")
            return summary

        run_id = self.st.start_run(dry)
        consecutive_errors = 0
        sent_in_run = 0
        max_err = int(o.fail_stop_threshold or 3)

        profile = pathlib.Path(
            pathlib.Path(self.cfg.app.db_path).parent
            if hasattr(self.cfg.app, "db_path")
            else "data"
        ).parent
        profile_dir = pathlib.Path(o.profile_dir or "data/outreach_profile")
        if not profile_dir.is_absolute():
            profile_dir = pathlib.Path.cwd() / profile_dir
        shots = profile_dir.parent / "outreach_shots"

        # ---- 2) 开浏览器 ----
        session = Session if use_session else None
        reused = bool(session and session.alive())
        self.log("② 复用常驻浏览器…" if reused else "② 启动浏览器（窗口在屏幕外）…")

        try:
            with self._page_ctx(profile_dir, bool(o.headless), session) as page:
                self._page = page
                dy = Douyin(page, shot_dir=shots, log=self.log)
                if not dy.cookies_ok():
                    self.stop_reason = "未登录：浏览器 profile 里没有 sessionid，先扫码登录"
                    self.log(f"❌ {self.stop_reason}")
                    self.st.finish_run(run_id, note=self.stop_reason)
                    summary.update({"ok": False, "stopped": self.stop_reason, "need_login": True})
                    self._emit(type="need_login")
                    return summary

                for idx, lead in enumerate(picked, 1):
                    if self.stop_reason:
                        break
                    self.log(
                        f"\n[{idx}/{len(picked)}] {lead['display_id']}　{lead.get('nickname') or ''}"
                        f"　{lead.get('car_model') or '车型未知'}　{lead.get('intent_level') or ''}"
                    )
                    try:
                        self._handle_one(dy, lead, summary)
                        consecutive_errors = 0
                    except RiskControl as e:
                        self.stop_reason = f"触发风控：{e}"
                        self.log(f"🛑 {self.stop_reason}，立即停止")
                        self._set_status(lead, STATUS_FAILED, str(e))
                        summary["failed"] += 1
                        self._emit(type="risk", lead=lead, error=str(e))
                        break
                    except NotLoggedIn as e:
                        self.stop_reason = f"登录失效：{e}"
                        self.log(f"🛑 {self.stop_reason}")
                        self._set_status(lead, STATUS_FAILED, str(e))
                        summary.update({"ok": False, "need_login": True})
                        break
                    except NoWorks as e:
                        # 没作品是正常情况（大量普通用户），记账但不打断节奏
                        summary["skipped"] += 1
                        self.log(f"  ⏭️ {e}")
                        self._set_status(lead, STATUS_SKIPPED, str(e))
                        self._emit(type="skipped", lead=lead, error=str(e))
                    except DouyinError as e:
                        consecutive_errors += 1
                        summary["failed"] += 1
                        self.log(f"  ❌ {e}")
                        self._set_status(lead, STATUS_FAILED, str(e))
                        self.st.add_touch(
                            lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
                            display_id=lead["display_id"], nickname=lead.get("nickname") or "",
                            status="failed", error=str(e),
                        )
                        self._emit(type="failed", lead=lead, error=str(e))
                        if consecutive_errors >= max_err:
                            self.stop_reason = f"连续 {consecutive_errors} 次异常，主动停手（防止把号跑废）"
                            self.log(f"🛑 {self.stop_reason}")
                            break
                    except Exception as e:
                        consecutive_errors += 1
                        summary["failed"] += 1
                        self.log(f"  ❌ 未预期错误：{type(e).__name__}: {e}")
                        self._set_status(lead, STATUS_FAILED, f"{type(e).__name__}: {e}")
                        self._emit(type="failed", lead=lead, error=str(e))
                        if consecutive_errors >= max_err:
                            self.stop_reason = f"连续 {consecutive_errors} 次异常，主动停手"
                            break
                    else:
                        if summary.get("sent"):
                            sent_in_run += 1

                    # 节奏控制
                    if idx < len(picked) and not self.stop_reason:
                        rest_every = int(o.rest_every or 0)
                        if rest_every and sent_in_run and sent_in_run % rest_every == 0:
                            rest = random.randint(int(o.rest_min or 120), int(o.rest_max or 240))
                            self.log(f"  ☕ 已发 {sent_in_run} 条，休息 {rest}s")
                            self._sleep(rest)
                        else:
                            self._sleep_between()

        finally:
            self._page = None

        note = self.stop_reason or ("干跑" if dry else "")
        self.st.finish_run(
            run_id, sent=summary["sent"], failed=summary["failed"],
            skipped=summary["skipped"], note=note,
        )
        summary["stopped"] = self.stop_reason
        self.log(
            f"\n本轮结束：发 {summary['sent']}　干跑 {summary['dry']}　"
            f"失败 {summary['failed']}　跳过 {summary['skipped']}"
            + (f"　停止原因：{self.stop_reason}" if self.stop_reason else "")
        )
        self._emit(type="done", summary=summary)
        return summary

    # ================================================================
    # 单条线索
    # ================================================================

    def _handle_one(self, dy: Douyin, lead: dict, summary: dict) -> None:
        """处理一条线索：找人 → 挑作品 → 生成话术 → 发送 → 回查"""
        o = self.cfg.outreach
        dry = self._dry

        # a. 找人
        self._set_status(lead, STATUS_SEARCHING)
        info = dy.resolve(lead["display_id"], nickname=lead.get("nickname"))
        dy.check_risk()
        if not info:
            raise DouyinError("没搜到这个人（抖音号可能已改名/注销/被平台隐藏）")

        lead["nickname"] = info.get("nickname") or lead.get("nickname")
        lead["sec_uid"] = info.get("sec_uid")
        self._set_status(lead, STATUS_FOUND)
        self.log(f"  ✓ 定位到 {lead['nickname']}（作品 {info.get('aweme_count')} 条）")

        # b. 挑作品
        if not info.get("aweme_count"):
            raise NoWorks("该用户没有公开作品（主页作品数为 0）")
        works = dy.fetch_works(lead["sec_uid"], limit=20)
        dy.check_risk()
        work = self._pick_work(works)
        if not work:
            raise NoWorks("该用户没有可用的公开作品")
        self.log(
            f"  ✓ 选中作品 {work['aweme_id']}　{(work.get('desc') or '')[:24]}"
        )

        # c. 生成话术
        c = self.cw.generate(self._as_copywriter_lead(lead))
        if not c.get("ok"):
            raise DouyinError(f"文案不合格：{c.get('reason')}")
        text = c["text"]
        self.log(f"  ✍️ [{c['template_id']}] {text}")

        if dry:
            self.st.add_touch(
                lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
                display_id=lead["display_id"], nickname=lead.get("nickname") or "",
                sec_uid=lead["sec_uid"], aweme_id=work["aweme_id"],
                aweme_desc=work.get("desc"), comment_text=text,
                template_id=c["template_id"], status="dry_run",
            )
            summary["dry"] += 1
            self._emit(type="dry", lead=lead, text=text, work=work)
            self.log("  🧪 干跑，不发送、不回写")
            return

        # d. 发送
        res = dy.post_comment(work["aweme_id"], text)
        if not res.get("ok"):
            raise DouyinError(f"发送失败：{res.get('msg')}（code={res.get('status_code')}）")

        touch_id = self.st.add_touch(
            lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
            display_id=lead["display_id"], nickname=lead.get("nickname") or "",
            sec_uid=lead["sec_uid"], aweme_id=work["aweme_id"],
            aweme_desc=work.get("desc"), comment_text=text,
            template_id=c["template_id"], status="sent",
        )
        self.log(f"  ✅ 已留言（cid={res.get('comment_id')}）")

        # e. 回查：抖音会静默删/屏蔽营销评论，
        #    「点完发送没报错」不等于「评论真的在」。
        need_verify = bool(o.verify_after_post) and bool(res.get("unconfirmed"))
        if need_verify:
            self.log("  🔍 回查评论是否真的在页面上…")
            try:
                v = dy.check_comment(
                    work["aweme_id"], text, nickname=None, scrolls=int(o.verify_scrolls or 1)
                )
            except Exception as e:
                v = {"found": None, "total": 0}
                self.log(f"  （回查出错，按已发处理：{str(e)[:60]}）")

            if v.get("found") is False:
                if res.get("comment_id"):
                    # 抖音收到了（有 cid）但没展示：多半是作者开了评论审核，
                    # 或者被判广告删了。这不算发送失败，记「待审核」，过一阵再回查。
                    self.st.mark_verified(touch_id, False, "评论已提交但未展示（作者审核/被判广告）")
                    self._set_status(lead, STATUS_COMMENTED, "已提交，评论区暂未展示")
                    summary["unshown"] = summary.get("unshown", 0) + 1
                    self.log("  ⚠️ 已提交但暂时不展示（大概率作者开了评论审核），稍后 verify 会再查")
                    self._emit(type="unshown", lead=lead, text=text)
                else:
                    self.st.mark_verified(touch_id, False, "无发送回执且回查不到，判定未发出")
                    self._set_status(lead, STATUS_PENDING, "评论未发出，待重发")
                    summary["failed"] += 1
                    self.log("  ⚠️ 评论没发出去（无回执），已退回「待触达」")

                    # 抖音的**软风控**：页面不弹验证码、不提示「操作频繁」，
                    # 只是把评论静默吞掉——发送按钮点了、没报错、回查也没有。
                    # 单次可能是偶发，连续出现就是账号被限流了，
                    # 必须停手，不能继续发把号搞废。
                    self._silent_blocks += 1
                    thr = int(o.silent_block_threshold or 2)
                    self.log(f"  ⚠️ 静默拦截计数 {self._silent_blocks}/{thr}")
                    if self._silent_blocks >= thr:
                        raise RiskControl(
                            f"连续 {self._silent_blocks} 条评论发出去但页面看不到，"
                            f"判定账号被限流（软风控），已停手。冷却 "
                            f"{int(o.risk_cooldown_minutes or 120)} 分钟后再试"
                        )
                    self._emit(type="failed", lead=lead, error="回查未出现在评论区")
                    return
            if v.get("found"):
                self.st.mark_verified(touch_id, True, "发完回查确认在页面上")
                self.log(f"  ✓ 回查确认在页面上（读到 {v.get('total')} 条评论）")

        self._set_status(lead, STATUS_COMMENTED)
        summary["sent"] += 1
        self._emit(type="sent", lead=lead, text=text, res=res)

    @staticmethod
    def _as_copywriter_lead(lead: dict) -> dict:
        """兼容旧调用点"""
        return lead_to_copywriter_input(lead)

    # ================================================================
    # 浏览器
    # ================================================================

    def _page_ctx(self, profile_dir: pathlib.Path, headless: bool, session):
        """拿一个可用的页面：有常驻浏览器就复用，没有就自己开一个"""
        from contextlib import contextmanager

        # 窗口摆屏幕外：看不见但不是 headless（抖音对无头检测更严，风控风险高）
        offscreen = bool(getattr(self.cfg.outreach, "hide_window", True))

        @contextmanager
        def _ctx():
            if session is not None and session.alive():
                page = session.page
                try:
                    page.goto("about:blank")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                yield page
                return
            with Browser(profile_dir, headless=headless, slow_mo=120,
                         offscreen=offscreen) as (_c, page):
                yield page

        return _ctx()
