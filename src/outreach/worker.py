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

import asyncio
import json
import pathlib
import random
import threading
import time
from typing import Callable, Optional

from .browser import Browser, Session
from .copywriter import Copywriter
from .douyin_api import (
    Douyin, DouyinError, KIND_LIMIT, KIND_LOGIN, KIND_SEARCH, KIND_VERIFY,
    LeadNotFound, NoWorks, NotLoggedIn, RiskControl, SearchUnavailable,
    VERIFY_TEXTS, normalize_kind,
)
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


def _run_coro_blocking(coro, timeout: float = 60.0):
    """在当前线程同步地等一个 coroutine 的结果

    为什么不能直接 asyncio.run
    --------------------------
    触达跑在 **Playwright sync API** 的线程里，而 Playwright sync API
    会在这个线程里装一个自己的 running event loop。
    在这个线程里调 asyncio.run() 会直接抛
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``，
    那个 coroutine 从未被 await —— 飞书状态回写就这么静默失败了，
    还只留下一条 RuntimeWarning。

    换一条干净的线程就没有这个问题：coroutine 本身不绑定线程，绑的是 loop。
    """
    box: dict = {}

    def _runner():
        try:
            box["ok"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — 异常要带回主线程
            box["err"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"飞书状态回写超时（{timeout}s）")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


class OutreachWorker:
    """触达任务执行器（同步，跑在专用线程）"""
    def __init__(
        self,
        cfg,
        db_path: str | pathlib.Path,
        log: Optional[Callable[[str], None]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        resume_event: Optional[threading.Event] = None,
        show_window_event: Optional[threading.Event] = None,
    ):
        self.cfg = cfg
        self.log = log or (lambda m: print(m))
        self.on_event = on_event or (lambda e: None)
        self.st = OutreachStore(db_path)
        # copywriter 吃的是 dict 配置，这里把 OutreachConfig 转成它认的形状
        self.cw = Copywriter(self._copywriter_cfg(), log=self.log)
        self.stop_reason = ""
        # 这一轮是因为什么停手的：verify / limit / login / 空
        # 跟 stop_reason 一起落库，服务重启后横幅还能恢复
        self._stop_kind = ""
        self._dry = True
        # 连续「发出去但回查不到」的次数（软风控信号）
        self._silent_blocks = 0
        # 撞验证码时用它跟主线程同步「人工验证完了，继续」
        # （worker 在专用线程里跑，主进程是 asyncio，跨不过去，只能靠 Event）
        self.resume_event = resume_event
        # 「把窗口叫到屏幕中间」的信号：网页上点按钮 → 主进程 set → 这里看到后挪窗口
        self.show_window_event = show_window_event
        # 正在等人工验证
        self.waiting_human = False
        # 本轮临时覆盖的两条之间间隔（秒）；None = 用配置里的
        self._delay_min: Optional[float] = None
        self._delay_max: Optional[float] = None
        # 当前页面对象（判断验证码是不是过了要用）
        self._dy = None

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

    # ================================================================
    # 撞验证码：叫人 + 停下等
    # ================================================================

    @staticmethod
    def _risk_kind(reason: str) -> str:
        """风控分三类，处理方式完全不同

        - ``verify``：要人工验证（短信/图形验证码）→ 叫人、停下等
        - ``login``：登录态没了 → 叫人扫码
        - ``limit``：账号被限流 → 直接停，扫码没用

        分类必须准：验证码当限流处理，人不在电脑前就白跑一轮；
        限流当验证码处理，会停在那儿等一个永远不会来的人。

        （以前这里还有个 ``_is_captcha``，只分「验证码 / 其他」。
        现在统一成三类，**只有一套判据**——两套判据迟早会漂。）
        """
        r = str(reason or "").lower()
        if any(h in r for h in ("登录", "login", "sessionid", "未登录", "扫码")):
            return KIND_LOGIN
        if any(h in r for h in VERIFY_TEXTS):
            return KIND_VERIFY
        return KIND_LIMIT

    def _notify_risk(self, kind: str, reason: str, lead: Optional[dict] = None) -> bool:
        """触达停手了 → 飞书私聊通知你

        **任何**让触达停下来的情况都要通知，不能只有验证码才发。
        之前软风控停了却一声不吭，用户在网页上只看到一行日志，
        既不知道发生了什么，也不知道该干什么。

        三种情况给三种指引：
        - verify：要人工验证 → 叫人 + 停下等
        - login：登录态没了 → 叫人扫码
        - limit：账号被限流 → 扫码没用，得等解封
        """
        kind = normalize_kind(kind)
        n = self.cfg.notify
        if not getattr(n, "lark_im_enabled", False):
            self.log("  （没开飞书私聊通知，只在页面上提示）")
            return False
        try:
            from ..notify.lark_im import get_im_notifier

            notifier = get_im_notifier(self.cfg)
        except Exception as e:
            self.log(f"  （通知器初始化失败：{e}）")
            return False
        if not notifier.is_configured:
            self.log("  （没配 lark_im_open_id，发不出去）")
            return False

        wait_min = int(getattr(self.cfg.outreach, "captcha_wait_minutes", 15) or 15)
        cool_min = int(getattr(self.cfg.outreach, "risk_cooldown_minutes", 120) or 120)

        if kind == KIND_VERIFY:
            title = "⚠️ 抖音要求验证本人操作，触达已暂停等你处理"
            steps = (
                "抖音在**发评论**或**搜索用户**时弹了**身份验证**（不是限流，账号是好的）：\n"
                "  「为保障账号安全，请先完成身份验证，以确保为本人操作」\n"
                "  （搜索时触发的话，弹的是一个**拼图滑块**——把拼图拖到缺口上）\n\n"
                "怎么办：\n"
                "1. 回到网页「抖音触达」点橙色提示条里的「打开浏览器窗口」\n"
                "   （窗口平时在屏幕右侧，点一下会跳到屏幕中间）\n"
                "2. 在那个窗口里按抖音的提示验证：\n"
                "   · **拼图滑块**——把拼图块拖到缺口上（搜索验证多半是这个）\n"
                "   · **扫码验证**——弹窗底部有「使用原设备扫码」，用抖音 App 扫\n"
                "   · 或者**短信验证码**——输入 187****52 收到的数字\n"
                "3. 验证完**自动接着跑**，不用再点「继续」\n\n"
                f"（{wait_min} 分钟内没人管就结束本轮）"
            )
        elif kind == "login":
            title = "🔑 抖音触达登录态失效了，需要你扫码"
            steps = (
                "1. 回到网页「抖音触达」点「扫码登录」\n"
                "2. 用抖音 App 扫那个窗口里的二维码\n"
                "3. 扫完回来再点「开始触达」\n\n"
                "（不扫码的话触达跑不了：搜不到人也发不了评论）"
            )
        elif kind == KIND_SEARCH:
            title = "🔍 抖音网页版搜索被限流了，触达已停手"
            steps = (
                "**你的账号没问题，不用扫码。**\n"
                "抖音**网页版的搜索接口**被限流了：它不报错、不弹提示，\n"
                "就是给你一个空的搜索结果（首页推荐流、手机 App 都正常）。\n\n"
                "怎么办：\n"
                "1. 别反复重试搜索——搜索次数越多越容易被继续限流\n"
                "2. 等 30 分钟到几小时再跑，搜索接口会自己恢复\n"
                "3. 系统已经记下了每个线索的抖音 ID，\n"
                "   下次**不用再搜**，直接开主页留言（所以搜不到也不影响触达）\n"
                "4. 急着要的话，用抖音 App 手动发一条试试（App 端不受影响）"
            )
        else:
            title = "🛑 抖音触达撞上账号限流，已停手"
            steps = (
                "**这不是验证码，扫码也没用**——是账号被限流了：\n"
                "评论发出去不报错，但页面上看不到，抖音把它静默吞了。\n\n"
                "怎么办：\n"
                "1. 立刻别再发评论（系统已经自动停了，冷却 "
                f"{cool_min} 分钟）\n"
                "2. 用抖音 App **手动**发一条评论试试：\n"
                "   · 能发出去 = 触达这边还有点问题，冷却完再试\n"
                "   · 也发不出去/看不到 = 账号确实被限流了，\n"
                "     只能等抖音自己解封（通常几小时到一天）\n"
                "3. 冷却结束后再跑，先开「干跑」看话术再开真发\n"
                "4. 还是不行就换个抖音号\n"
            )

        who = ""
        if lead:
            who = f"抖音号 {lead.get('display_id') or ''}"
        text = (
            f"{title}\n\n"
            f"原因：{reason}\n"
            + (f"卡在：{who}\n" if who else "")
            + "\n"
            + steps
        )
        try:
            ok, err = _run_coro_blocking(notifier.send_text(text), timeout=30)
        except Exception as e:
            self.log(f"  （飞书通知失败：{e}）")
            return False
        if ok:
            self.log("  📱 已飞书私聊通知你")
        else:
            self.log(f"  （飞书通知失败：{err}）")
        return ok

    def _bring_window_back(self) -> bool:
        """把浏览器窗口挪到屏幕中间并置前

        平时窗口摆在屏幕外（--window-position=-3200,-3200），
        撞验证码时你根本看不见那个验证码长什么样。
        """
        if not getattr(self.cfg.outreach, "captcha_bring_to_front", True):
            return False
        page = getattr(self, "_page", None)
        if page is None:
            return False
        try:
            from .browser import Session, bring_to_front

            ok = bring_to_front(page.context, page, Session.app_name())
            if ok:
                self.log("  🪟 浏览器窗口已挪到屏幕中间")
            return ok
        except Exception as e:
            self.log(f"  （窗口挪不动：{e}）")
            return False

    def _release_focus(self) -> None:
        """把前台还给用户

        撞验证码时我们把浏览器叫到了前面，用户验证完之后如果不管，
        浏览器就会一直占着前台——他切回自己的窗口继续干活，
        打的字却进了浏览器里。

        所以恢复之后必须把焦点还回去。
        """
        try:
            from .browser import release_focus

            if release_focus():
                self.log("  🖥 前台已还给你的窗口")
        except Exception:
            pass

    def _captcha_cleared(self) -> bool:
        """看看验证码是不是已经自己过了

        用户在浏览器里把验证做完之后，页面上的验证码就消失了——
        这时候应该**自动接着跑**，而不是让人再去网页上点一下「继续」。

        判定复用 ``Douyin.check_risk``：它抛异常 = 还有风控信号（还在等），
        不抛 = 干净了（可以继续）。
        """
        page = getattr(self, "_page", None)
        dy = getattr(self, "_dy", None)
        if page is None or dy is None:
            return False
        try:
            url = page.url or ""
            if "/verify" in url or "captcha" in url:
                return False
        except Exception:
            return False
        try:
            dy.check_risk()
            return True
        except Exception:
            # 还在风控页上（或者页面正在跳转，读不到）→ 继续等
            return False

    def _wait_for_human(self, reason: str) -> bool:
        """暂停等人工验证

        返回 True = 人工验证完了，可以接着跑；
        返回 False = 没人管（超时），结束本轮。

        实现上是个跨线程的 Event：worker 在专用线程里等，
        网页上点「我验证好了，继续」时主进程 set()，这边就醒过来。

        等待期间同时做两件事：
        1. 网页上点「打开浏览器窗口」→ 把窗口叫到屏幕中间（用户自己操作）
        2. 每隔几秒看一眼页面：验证码要是已经自己过了，就**自动接着跑**
        """
        ev = self.resume_event
        if ev is None:
            return False
        # 用 float：0.2 分钟（12 秒）这种小值也得能用，
        # int(0.2) 会变成 0，直接变成「不」。
        minutes = float(getattr(self.cfg.outreach, "captcha_wait_minutes", 15) or 0)
        if minutes <= 0:
            return False
        deadline = time.time() + minutes * 60
        ev.clear()
        self.waiting_human = True
        self._emit(type="captcha_wait", error=reason, minutes=minutes)
        self.log(f"⏸ 已暂停，等你手动验证（最多等 {minutes} 分钟）")
        self.log("   网页上点「打开浏览器窗口」可以把它叫到屏幕中间，你自己操作")
        next_probe = time.time() + 5.0
        try:
            while time.time() < deadline:
                # 先处理「打开浏览器窗口」信号。
                # 顺序很重要：不能让它被下面的 ev.wait 挡住——
                # 用户点了按钮就得马上看到窗口，不能等到超时或「继续」才响应。
                sw = self.show_window_event
                if sw is not None and sw.is_set():
                    sw.clear()
                    self._bring_window_back()
                    self.log("  🖐 你可以在那个窗口里手动过验证")

                if ev.wait(timeout=0.5):
                    self.waiting_human = False
                    self._emit(type="captcha_resumed")
                    self.log("▶ 收到「继续」信号，接着跑")
                    self._release_focus()
                    return True

                # 自动检测：验证码是不是已经自己过了 → 直接接着跑
                if time.time() >= next_probe:
                    next_probe = time.time() + 5.0
                    if self._captcha_cleared():
                        self.waiting_human = False
                        self._emit(type="captcha_resumed", auto=True)
                        self.log("🔎 检测到验证码已经过了，自动接着跑")
                        self._release_focus()
                        return True
            self.waiting_human = False
            self._emit(type="captcha_timeout")
            self.log(f"⏸ 等待超时（{minutes} 分钟没人管），结束本轮")
            return False
        finally:
            self.waiting_human = False

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
            # 干跑的间隔也不能太短！
            # 干跑虽然不发评论，但**照样要搜索用户** ——
            # 3-8 秒一次搜索，10 条下来就是 1 分钟内 10 次搜索，
            # 抖音的搜索风控扛不住（2026-11-11 实测：干跑那轮 10 条全部搜不到）。
            sec = random.randint(15, 25)
            self.log(f"  ⏳ 干跑，等 {sec}s 看下一条")
            self._sleep(sec)
            return
        # 本轮临时覆盖（网页上可以当场调），没给就用配置里的
        lo = int(self._delay_min if self._delay_min is not None
                 else (o.min_delay or 45))
        hi = max(lo, int(self._delay_max if self._delay_max is not None
                         else (o.max_delay or 120)))
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
        _run_coro_blocking(
            writer.patch_fields(record_id, {"触达状态": text}),
            timeout=90,
        )

    # ================================================================
    # 主流程
    # ================================================================

    def run(
        self,
        limit: Optional[int] = None,
        dry_run: Optional[bool] = None,
        only: Optional[list[str]] = None,
        use_session: bool = True,
        delay_min: Optional[float] = None,
        delay_max: Optional[float] = None,
        sort_by: Optional[str] = None,
    ) -> dict:
        """跑一轮触达

        Args:
            limit: 本轮最多处理几条（默认取配置 max_per_run）
            dry_run: 干跑（默认取配置 dry_run，**默认是 True**）
            only: 只处理指定的抖音号
            use_session: 复用常驻浏览器（省掉每次重开窗口的十几秒）
            delay_min / delay_max: 本轮的两条之间间隔（秒），留空用配置里的
            sort_by: 本轮排序（recent=最近抓到优先 / intent=高意向优先），
                留空用配置里的
        """
        o = self.cfg.outreach
        dry = bool(o.dry_run if dry_run is None else dry_run)
        self._dry = dry
        self._silent_blocks = 0
        # 每轮从头开始（同一个 worker 对象可能被复用跑多轮）
        self.stop_reason = ""
        self._stop_kind = ""
        # 本轮的临时间隔覆盖（网页上当场调，不写回配置）
        self._delay_min = delay_min
        self._delay_max = delay_max
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
            # 本轮临时覆盖排序方式，留空用配置里的
            sort_by=str(sort_by or o.sort_by or "recent"),
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
            "not_found": 0,
        }
        if not picked:
            self.log("没有要处理的线索，结束")
            return summary

        run_id = self.st.start_run(dry)
        consecutive_errors = 0
        # 连续「搜不到人」的条数——用来区分「几个号失效了」和「搜索整体坏了」
        consecutive_not_found = 0
        sent_in_run = 0
        max_err = int(o.fail_stop_threshold or 3)
        # 连续这么多条都搜不到人，就该怀疑是搜索坏了而不是这些人都失效了
        max_not_found = int(getattr(o, "not_found_stop_threshold", 5) or 5)

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
                # 存一份：等人工验证时要靠它判断「验证码是不是已经过了」
                self._dy = dy
                if not dy.cookies_ok():
                    self.stop_reason = "未登录：浏览器 profile 里没有 sessionid，先扫码登录"
                    self._stop_kind = KIND_LOGIN
                    self.log(f"❌ {self.stop_reason}")
                    self.st.finish_run(run_id, note=self.stop_reason,
                                       kind=self._stop_kind)
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
                    # 告诉控制台「现在正在处理第几条」，进度条才有东西可画
                    self._emit(type="current", lead=lead, index=idx,
                               total=len(picked))
                    try:
                        self._handle_one(dy, lead, summary)
                        consecutive_errors = 0
                        consecutive_not_found = 0
                    except RiskControl as e:
                        # 优先用异常自带的类型（只有一套判据），
                        # 老异常没有 kind 时再退回关键词判断。
                        kind = normalize_kind(getattr(e, "kind", "")
                                              or self._risk_kind(str(e)))
                        self.stop_reason = f"触发风控：{e}"
                        self._stop_kind = kind
                        self.log(f"🛑 {self.stop_reason}，立即停止")
                        self._emit(type="risk", lead=lead, error=str(e), kind=kind)

                        # 验证码要人工过：叫人 + 把窗口叫回来 + 停下等他。
                        # 注意这跟「账号被限流」是两回事：
                        # 限流是平台不给过了，验证码是平台要你证明你是人。
                        if kind == KIND_VERIFY:
                            self._bring_window_back()
                            self._notify_risk(KIND_VERIFY, str(e), lead)
                            if self._wait_for_human(str(e)):
                                # 人工验证完了：这条线索退回待触达，重新来
                                self._set_status(lead, STATUS_PENDING, "验证码已人工处理，重新排队")
                                self.stop_reason = ""
                                self._stop_kind = ""
                                self.log("↩ 继续处理队列")
                                continue
                            self.stop_reason = f"验证码未人工处理：{e}"
                        else:
                            # 限流/软风控：直接停，而且**必须通知人**。
                            # 以前只有验证码分支发飞书，软风控停了却一声不吭，
                            # 用户在网页上只看到一行日志，既不知道发生了什么，
                            # 也不知道该怎么办（扫码也没用，限流不是登录问题）。
                            self._notify_risk(kind, str(e), lead)
                            self._set_status(lead, STATUS_FAILED, str(e))
                            summary["failed"] += 1
                        break
                    except NotLoggedIn as e:
                        # 登录态没了 → 叫人扫码（这个是能解决的！）
                        self.stop_reason = f"登录失效：{e}"
                        self._stop_kind = KIND_LOGIN
                        self.log(f"🛑 {self.stop_reason}")
                        self._emit(type="risk", lead=lead, error=str(e), kind=KIND_LOGIN)
                        self._notify_risk("login", str(e), lead)
                        self._set_status(lead, STATUS_FAILED, str(e))
                        summary.update({"ok": False, "need_login": True})
                        break
                    except NoWorks as e:
                        # 没作品是正常情况（大量普通用户），记账但不打断节奏
                        # （搜到人了 → 搜索是通的，不算「搜不到」）
                        consecutive_not_found = 0
                        summary["skipped"] += 1
                        self.log(f"  ⏭️ {e}")
                        self._set_status(lead, STATUS_SKIPPED, str(e))
                        self._emit(type="skipped", lead=lead, error=str(e))
                    except LeadNotFound as e:
                        # 这个人搜不到 —— **正常业务结果，不是故障**。
                        #
                        # 以前它抛的是普通 DouyinError，落进「连续异常」计数，
                        # 3 条搜不到就停手。但线索池里大量抖音号本来就失效了
                        # （改名/注销/被隐藏），拿正常数据当异常，
                        # 等于因为坏数据把整轮掐断，效率极低。
                        #
                        # 所以这里**不计入** consecutive_errors，
                        # 也不会触发「连续 N 次异常，主动停手」。
                        consecutive_not_found += 1
                        summary["not_found"] = summary.get("not_found", 0) + 1
                        summary["skipped"] += 1
                        self.log(f"  ⏭️ {e}")
                        self._set_status(lead, STATUS_SKIPPED, str(e))
                        self.st.add_touch(
                            lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
                            display_id=lead["display_id"], nickname=lead.get("nickname") or "",
                            status="skipped", error=str(e),
                        )
                        self._emit(type="skipped", lead=lead, error=str(e))
                        # 但如果**连续**这么多条全都搜不到人，
                        # 那多半是搜索出问题了（接口改版 / 被限流），
                        # 不是这些人都恰好失效——这时候继续跑纯属空转。
                        if consecutive_not_found >= max_not_found:
                            self.stop_reason = (
                                f"连续 {consecutive_not_found} 条都搜不到人，"
                                f"多半是搜索出问题了（不是这些人都失效），主动停手"
                            )
                            self.log(f"🛑 {self.stop_reason}")
                            self._emit(type="risk", lead=lead, error=self.stop_reason,
                                       kind="limit")
                            break
                    except SearchUnavailable as e:
                        # 搜索接口返回空、而且**没有**「要求验证」的信号
                        # → 多半是抖音 web 端搜索被限流了。
                        #
                        # 注意区分两种「搜索为空」（2026-11-12）：
                        #   · 接口明说 verify_check（要过拼图验证）
                        #     → 上面 RiskControl 分支已经接住，叫人过验证、继续跑
                        #   · 接口啥也没说，探针也空 → 这里，接口真的被限流/故障
                        #
                        # 后者必须当真故障处理：连续几次都这样就停手，
                        # 别再拿搜索次数去撞抖音的风控。
                        # （账号本身是好的，所以**不触发账号限流冷却**——
                        #   那种情况扫码也没用，方向完全错。）
                        consecutive_errors += 1
                        consecutive_not_found = 0
                        summary["failed"] += 1
                        self.log(f"  ❌ {e}")
                        self._set_status(lead, STATUS_FAILED, str(e))
                        self._emit(type="failed", lead=lead, error=str(e))
                        if consecutive_errors >= max_err:
                            self.stop_reason = (
                                f"搜索接口连续 {consecutive_errors} 次返回空"
                                f"（疑似被抖音限流），主动停手"
                            )
                            self._stop_kind = KIND_SEARCH
                            self.log(f"🛑 {self.stop_reason}")
                            self._emit(type="risk", lead=lead,
                                       error=self.stop_reason, kind=KIND_SEARCH)
                            self._notify_risk(KIND_SEARCH, self.stop_reason, lead)
                            break
                    except DouyinError as e:
                        consecutive_errors += 1
                        # 不是 LeadNotFound → 说明人找到了、搜索是通的
                        consecutive_not_found = 0
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
                        consecutive_not_found = 0
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
            self._dy = None

        note = self.stop_reason or ("干跑" if dry else "")
        self.st.finish_run(
            run_id, sent=summary["sent"], failed=summary["failed"],
            skipped=summary["skipped"], note=note, kind=self._stop_kind,
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

    def _post_with_human_help(self, dy: Douyin, aweme_id: str, text: str,
                              lead: dict) -> dict:
        """发评论；如果抖音要人工验证，就叫人，处理完再发

        为什么要有这一层
        ----------------
        抖音发评论时可能弹「身份验证」（短信验证码 / 扫码），
        这时评论**根本没发出去**。如果只是抛异常让整轮结束，
        这条线索就白跑了——人验完了，评论还是没发。

        所以：叫人 → 等他验完 → **先回查**评论到底发出去没有：
        - 发出去了（验证通过后抖音自动提交）→ 直接算成功
        - 没发出去 → 补发一次

        为什么要先回查再补发：重复评论在抖音是明确的限流/封号风险。
        """
        for _attempt in (1, 2):
            try:
                return dy.post_comment(aweme_id, text)
            except RiskControl as e:
                if normalize_kind(getattr(e, "kind", "")) != KIND_VERIFY:
                    raise
                # 要人工验证 → 叫人
                self.stop_reason = f"触发风控：{e}"
                self._stop_kind = KIND_VERIFY
                self.log(f"🛑 {self.stop_reason}")
                self._emit(type="risk", lead=lead, error=str(e), kind=KIND_VERIFY)
                self._bring_window_back()
                self._notify_risk(KIND_VERIFY, str(e), lead)
                if not self._wait_for_human(str(e)):
                    raise DouyinError(f"验证码未人工处理：{e}") from e
                self.stop_reason = ""
                self._stop_kind = ""

                # ⚠️ 必须清掉「要验证」状态！
                # 不清的话它会一直挂着，下一条线索**刚开跑就被拦死**
                # （实测 2026-11-11：验证完第 1 条，第 2 条在搜索阶段就停了）。
                try:
                    dy.clear_verify()
                except Exception:
                    pass

                # 验证完了：这条评论到底发出去了没有？
                found = False
                try:
                    chk = dy.check_comment(aweme_id, text, nickname=None, scrolls=4)
                    found = bool(chk.get("found"))
                except Exception as e:
                    self.log(f"  （回查出错：{str(e)[:60]}）")
                if found:
                    self.log("  ✓ 验证通过，回查发现评论已经发出去了")
                    return {"ok": True, "status_code": 0, "verified": True}
                # 没发出去 → 补发一次
                self.log("  ↻ 验证通过了，但评论没发出去，补发一次")
                continue
        return {"ok": False, "status_code": -1, "msg": "验证后仍没能发出评论"}

    def _cache_sec_uids(self, dy: Douyin, lead: dict) -> None:
        """把定位到的 sec_uid 存下来（下次就不用再搜索了）

        为什么值得专门做（2026-11-12）：
        抖音 web 端搜索接口会**静默限流**——不报错、不提示，
        就是给你一个空的 user_list。首页推荐流完全正常，
        手机 App 也能搜到同一个号，只有 web 端搜索这一条路被掐了。

        搜索次数本身就是被限流的资源，所以能省就省：
          1. 目标自己的 sec_uid 存下来 → 下次直接开主页
          2. 这一页里其他用户的 sec_uid 也存下来（抖音一次返回十几个）
             → 线索池里**同名同号**的那些以后也不用搜了

        存的是「查表用的技术缓存」，不碰 outreach_status，
        所以干跑时也存（干跑要跑跟真发一样的路径才有参考价值）。
        """
        sec_uid = str(lead.get("sec_uid") or "").strip()
        if sec_uid:
            try:
                self.st.set_sec_uid(lead["id"], sec_uid, lead.get("nickname") or "")
            except Exception as e:
                self.log(f"  （缓存 sec_uid 失败：{str(e).splitlines()[0][:60]}）")
        mapping: dict[str, str] = {}
        for u in (getattr(dy, "last_search_users", None) or []):
            if not isinstance(u, dict):
                continue
            su = str(u.get("sec_uid") or "").strip()
            if not su:
                continue
            for key in (u.get("unique_id"), u.get("short_id")):
                k = str(key or "").strip()
                if k:
                    mapping.setdefault(k, su)
        if not mapping:
            return
        try:
            n = self.st.cache_sec_uid_map(mapping)
            if n:
                self.log(f"  💾 顺手缓存了 {n} 条线索的 sec_uid（以后不用再搜这些号）")
        except Exception as e:
            self.log(f"  （批量缓存 sec_uid 失败：{str(e).splitlines()[0][:60]}）")

    def _handle_one(self, dy: Douyin, lead: dict, summary: dict) -> None:
        """处理一条线索：找人 → 挑作品 → 生成话术 → 发送 → 回查"""
        o = self.cfg.outreach
        dry = self._dry

        # a. 找人
        self._set_status(lead, STATUS_SEARCHING)
        known_sec = str(lead.get("sec_uid") or "").strip()
        if known_sec:
            # 已知 sec_uid → 直接开主页，**跳过搜索**。
            #
            # 为什么（2026-11-12）：抖音 web 端搜索接口会静默限流，
            # 不报错、不提示，就是给你一个空的 user_list；
            # 但主页 /user/{sec_uid} 完全不受影响。
            # 搜索次数本身就是被限流的资源，能省就省。
            info = dy.resolve_by_sec_uid(known_sec)
        else:
            info = dy.resolve(lead["display_id"], nickname=lead.get("nickname"))
        dy.check_risk()
        if not info:
            raise LeadNotFound("没搜到这个人（抖音号可能已改名/注销/被平台隐藏）")

        lead["nickname"] = info.get("nickname") or lead.get("nickname")
        lead["sec_uid"] = info.get("sec_uid") or known_sec
        self._set_status(lead, STATUS_FOUND)
        self.log(f"  ✓ 定位到 {lead['nickname']}（作品 {info.get('aweme_count')} 条）")
        # 顺手把这次搜索拿到的 sec_uid 都存下来（下次不用再搜）
        self._cache_sec_uids(dy, lead)

        # b. 挑作品
        if info.get("aweme_count") == 0:
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
        res = self._post_with_human_help(dy, work["aweme_id"], text, lead)
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
                    self.st.mark_verified(touch_id, False, "评论已提交但未展示（作者审核/被判广告）",
                                           status="unshown")
                    self._set_status(lead, STATUS_COMMENTED, "已提交，评论区暂未展示")
                    summary["unshown"] = summary.get("unshown", 0) + 1
                    self.log("  ⚠️ 已提交但暂时不展示（大概率作者开了评论审核），稍后 verify 会再查")
                    self._emit(type="unshown", lead=lead, text=text)
                else:
                    self.st.mark_verified(touch_id, False, "无发送回执且回查不到，判定未发出",
                                           status="failed")
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

        # 窗口模式：background（看得见但在后面跑）/ offscreen（藏起来）/ visible
        mode = str(getattr(self.cfg.outreach, "window_mode", "background") or "background").strip()
        if mode not in ("background", "offscreen", "visible"):
            mode = "background"
        # 别下载视频（省流量省 CPU）。静音另有 JS 兜底，不依赖这一项。
        block_media = bool(getattr(self.cfg.outreach, "block_media", True))

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
                         window_mode=mode,
                         block_media=block_media) as (_c, page):
                yield page

        return _ctx()
