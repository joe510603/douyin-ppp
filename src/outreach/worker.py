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
import queue
import random
import threading
import time
from typing import Callable, Optional

from .browser import Browser, Session
from .copywriter import Copywriter
from .douyin_api import (
    Douyin, DouyinError, KIND_LIMIT, KIND_LOGIN, KIND_SEARCH, KIND_VERIFY,
    LeadNotFound, NoWorks, NotLoggedIn, RiskControl, SearchNotReady,
    SearchUnavailable, VERIFY_TEXTS, normalize_kind,
)
from .store import (
    STATUS_COMMENTED,
    STATUS_CONVERTED,
    STATUS_FAILED,
    STATUS_FOUND,
    STATUS_NOT_FOUND,
    STATUS_PENDING,
    STATUS_SEARCHING,
    STATUS_SKIPPED,
    OutreachStore,
)
from ..intelligence.contact_extractor import extract_contacts

# 触达状态 → 飞书表「触达状态」列的文案
FEISHU_STATUS = {
    STATUS_PENDING: "待触达",
    STATUS_SEARCHING: "搜索中",
    STATUS_FOUND: "已定位",
    STATUS_COMMENTED: "已留言",
    STATUS_FAILED: "失败",
    STATUS_SKIPPED: "已跳过",
    STATUS_NOT_FOUND: "搜不到",
    STATUS_CONVERTED: "已留资",
}


def _is_transient_error(err: BaseException) -> bool:
    """这个错误是不是「偶发的」？

    偶发的错误**不该**把线索永久踢出队列——
    待触达队列只挑 ``outreach_status='pending'``，
    一旦标成 failed，这条线索就再也不会被处理。

    实测（2026-11-13）：一次搜索限流 + 几次页面超时 / 连接被掐断，
    就把 100 多条**完全有效**的线索判了死刑（failed 有 141 条，pending 只剩 50 条）。
    而这些号在手机上全都是能搜到的。

    判据（按可靠度排）：
    - 异常类型是 TimeoutError（Playwright 的和内建的都算）
    - 报错文本里有超时 / 连接被掐断的痕迹
      （net::ERR_、ERR_CONNECTION、Timeout）
    """
    if type(err).__name__ == "TimeoutError":
        return True
    msg = str(err)
    for kw in ("Timeout", "timeout", "net::ERR_", "ERR_CONNECTION",
               "ERR_TIMED_OUT", "ERR_EMPTY_RESPONSE", "Connection reset"):
        if kw in msg:
            return True
    return False


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


class _FeishuStatusPusher:
    """后台线程写飞书状态，**不阻塞**触达主流程

    为什么必须异步（2026-11-12 实测）：
    飞书多维表格走的是 lark-cli **子进程**，一次 patch_fields 实测要 **1.4 秒**；
    而每条线索要回写 3 次状态（searching / found / commented）。
    同步写 = 每条线索白等 4.2 秒，整个浏览器流程被飞书的网络往返卡着 ——
    这 4.2 秒跟抖音一点关系都没有，纯粹是在等飞书。

    改成后台线程排队写：
    - 本地 SQLite 状态照旧**同步**落库（那是本地的，亚毫秒级）
    - 飞书那边晚几秒无所谓 —— 多机同步本来就是靠**回拉**对齐的，
      不靠推送的实时性
    """

    def __init__(self, cfg, log: Callable[[str], None]):
        self._cfg = cfg
        self._log = log
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="feishu-status", daemon=True)
        self._thread.start()

    def push(self, record_id: str, fields: dict) -> None:
        """排队写一条状态，立刻返回，不等网络"""
        if not record_id or not fields:
            return
        # ⚠ 顺序很重要：**先入队、再起线程**。
        #   反过来的话，写入器初始化失败的分支会先 drain 掉队列
        #   （那时队列还是空的，什么都没 drain 到），
        #   随后这里才 put → 那个待写项永远留在队列里，
        #   _pending 也永远差 1，flush 会一直等到超时。
        with self._lock:
            self._pending += 1
        self._q.put((record_id, dict(fields)))
        self._ensure_thread()

    def _loop(self) -> None:
        from ..notify.bitable import get_bitable_writer
        try:
            writer = get_bitable_writer(self._cfg)
        except Exception as e:
            self._log(f"  ⚠️ 飞书写入器初始化失败，状态只写本地：{str(e)[:80]}")
            self._drain()
            return
        while True:
            try:
                item = self._q.get(timeout=0.3)
            except queue.Empty:
                if self._stopping:
                    return
                continue
            record_id, fields = item
            try:
                _run_coro_blocking(writer.patch_fields(record_id, fields), timeout=90)
            except Exception as e:
                self._log(f"  ⚠️ 飞书状态回写失败（不影响触达）：{str(e)[:80]}")
            finally:
                with self._lock:
                    self._pending -= 1
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def _drain(self) -> None:
        """写入器起不来时把队列清掉，别让 _pending 永远挂着"""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending -= 1

    def flush(self, timeout: float = 30.0) -> bool:
        """等队列排空（轮次结束时调用，别让状态丢在半路）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._pending <= 0:
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self._stopping = True


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
        # 手上正在处理的那条线索（轮次异常结束时靠它退回队列）
        self._current_lead: Optional[dict] = None
        # 本轮临时覆盖的两条之间间隔（秒）；None = 用配置里的
        self._delay_min: Optional[float] = None
        self._delay_max: Optional[float] = None
        # 连续「干干净净」的条数（用来把慢档撤掉，回到快档）
        self._clean_streak = 0
        # 飞书状态回写：后台线程写，不阻塞触达（见 _FeishuStatusPusher）
        self._feishu = _FeishuStatusPusher(self.cfg, self.log)
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
        """触达停手了 → 飞书通知你

        **任何**让触达停下来的情况都要通知，不能只有验证码才发。
        之前软风控停了却一声不吭，用户在网页上只看到一行日志，
        既不知道发生了什么，也不知道该干什么。

        三种情况给三种指引：
        - verify：要人工验证 → 叫人 + 停下等
        - login：登录态没了 → 叫人扫码
        - limit：账号被限流 → 扫码没用，得等解封

        走哪个通道
        ----------
        **群机器人优先，私聊兜底。**

        2026-11-12 实测：lark-cli 私聊消息**发得出去**（返回 ok=true），
        但飞书私聊会话在手机上**不产生推送**——消息躺在那儿没人知道。
        群消息才会推，而且 @ 人会触发强提醒。
        所以「要提醒我」= 群里发一张红卡片 + @你；
        群机器人没配或发失败时，才退回私聊（总比一声不吭强）。
        """
        kind = normalize_kind(kind)
        n = self.cfg.notify
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

        # ---- 主通道：飞书群机器人（红卡片 + @人，手机会强提醒）----
        if getattr(n, "risk_webhook_enabled", False) and (
                n.risk_webhook_url or "").strip():
            try:
                from ..notify.feishu import FeishuNotifier

                notifier = FeishuNotifier(
                    webhook_url=n.risk_webhook_url,
                    secret=getattr(n, "risk_webhook_secret", "") or "",
                )
                ok, err = _run_coro_blocking(
                    notifier.send_risk_card(
                        title=title,
                        reason=reason,
                        steps=steps,
                        at_open_id=(getattr(n, "risk_at_open_id", "") or "").strip(),
                        who=who,
                    ),
                    timeout=30,
                )
            except Exception as e:
                ok, err = False, str(e)
            if ok:
                self.log("  📣 已在飞书群里提醒你（红卡片 + @你）")
                return True
            self.log(f"  （群机器人没发出去：{err}，改用私聊）")

        # ---- 兜底：飞书私聊 ----
        if not getattr(n, "lark_im_enabled", False):
            self.log("  （没配飞书通知渠道，只在页面上提示）")
            return False
        try:
            from ..notify.lark_im import get_im_notifier

            im_notifier = get_im_notifier(self.cfg)
        except Exception as e:
            self.log(f"  （通知器初始化失败：{e}）")
            return False
        if not im_notifier.is_configured:
            self.log("  （没配 lark_im_open_id，发不出去）")
            return False

        text = (
            f"{title}\n\n"
            f"原因：{reason}\n"
            + (f"卡在：{who}\n" if who else "")
            + "\n"
            + steps
        )
        try:
            ok, err = _run_coro_blocking(im_notifier.send_text(text), timeout=30)
        except Exception as e:
            self.log(f"  （飞书通知失败：{e}）")
            return False
        if ok:
            self.log("  📱 已飞书私聊通知你")
        else:
            self.log(f"  （飞书通知失败：{err}）")
        return ok

    def _notify_resumed(self, lead: Optional[dict] = None) -> bool:
        """验证过了 → 主动告诉用户一声「已经恢复，接着发」

        为什么必须有这个通知
        ------------------
        2026-11-13 用户反馈「图片验证出来后，手动过了，但是没反应了」。
        查日志发现程序**其实恢复了**（09:54:32 自动检测到验证码已过，继续跑），
        但用户肉眼看过去就是「什么都没发生」：

        - 恢复只有一行内部日志，没人在意；
        - 恢复后紧接着就撞上**搜索接口限流**，一条也没发出去；
        - 然后进入慢档，每条要等 102 秒 → 屏幕上就是一直不動。

        所以恢复必须**主动说一声**。让人知道：程序还活着、在跑、
        以及为什么短时间内看不到留言（搜索限流，线索不会丢）。
        """
        n = self.cfg.notify
        lines = [
            "验证已经过了，触达**自动接着跑**，不用再点「继续」。",
        ]
        if lead:
            lines.append(f"**正在重试**：抖音号 {lead.get('display_id') or ''}")
        lines.append(
            "如果接下来几分钟没看到留言，多半是抖音网页版搜索接口在限流"
            "（不报错、就是搜不出人）。系统会自动退避重试，**线索不会丢**。"
        )
        if not (getattr(n, "risk_webhook_enabled", False)
                and (n.risk_webhook_url or "").strip()):
            self.log("  （没配群机器人，就不打扰你了）")
            return False
        try:
            from ..notify.feishu import FeishuNotifier

            notifier = FeishuNotifier(
                webhook_url=n.risk_webhook_url,
                secret=getattr(n, "risk_webhook_secret", "") or "",
            )
            ok, err = _run_coro_blocking(
                notifier.send_info_card(
                    title="✅ 抖音验证已通过，触达继续",
                    lines=lines,
                ),
                timeout=30,
            )
        except Exception as e:
            ok, err = False, str(e)
        if ok:
            self.log("  📣 已在飞书群里告诉你「验证已过，继续发」")
        else:
            self.log(f"  （群机器人没发出去：{err}）")
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

    def _release_current_lead(self) -> None:
        """把「手上正在处理」的那条线索退回待触达

        为什么需要
        ----------
        轮次异常结束（撞验证超时 / 进程被杀 / 抛异常）时，
        手上那条会停在「搜索中」或「已定位」——
        这俩都不是终态，而待触达队列只挑 ``pending``，
        所以这条线索就**永远没人管了**（实测：卡了 3 条出不来）。

        正常跑完的轮次不受影响：那条线索那时已经是
        已留言 / 已跳过 / 失败 这些终态，不会被误改。
        """
        lead = getattr(self, "_current_lead", None)
        if not lead:
            return
        self._current_lead = None
        try:
            row = self.st.get_lead(lead["id"])
        except Exception:
            return
        if not row:
            return
        if row.get("outreach_status") in (STATUS_SEARCHING, STATUS_FOUND):
            self._set_status(lead, STATUS_PENDING, "上一轮中断，重新排队")
            self.log(f"  ↩ 中断时手上这条已退回队列（{lead.get('display_id')}）")

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
        lo, hi, why = self._pick_delay()
        sec = random.randint(lo, hi)
        self.log(f"  ⏳ 等 {sec}s 再找下一个（{why}）")
        self._sleep(sec)

    # ================================================================
    # 自适应间隔：平时快，撞风控自动退
    # ================================================================

    def _is_slow_mode(self) -> bool:
        """现在是不是「慢档」（撞过风控、还在退避期里）"""
        try:
            return bool(self.st.speed_state().get("slow"))
        except Exception:
            return False

    def _enter_slow_mode(self, reason: str) -> None:
        """撞了风控 → 切到慢档并记住一段时间

        为什么撞了风控反而要**变慢**：
        抖音开始要验证，说明频率已经踩线了。这时候继续快等于加速把号搞废。
        慢档持续到 slow_delay_minutes 之后（或连续几条都干净了）才回快档。
        """
        o = self.cfg.outreach
        minutes = int(o.slow_delay_minutes or 60)
        try:
            until = self.st.set_slow_delay(minutes, reason)
        except Exception as e:
            self.log(f"  ⚠️ 记不住慢档状态: {str(e)[:60]}")
            return
        self._clean_streak = 0
        self.log(
            f"  🐢 间隔切到慢档（{int(o.slow_delay_min or 90)}-{int(o.slow_delay_max or 120)}s），"
            f"到 {until} 为止"
        )

    def _maybe_exit_slow_mode(self) -> None:
        """连续 N 条都干干净净 → 撤掉慢档，回到快档"""
        o = self.cfg.outreach
        need = int(o.slow_delay_clear_after or 5)
        if self._clean_streak < need or not self._is_slow_mode():
            return
        try:
            self.st.clear_slow_delay()
        except Exception:
            return
        self._clean_streak = 0
        self.log(
            f"  🐇 连续 {need} 条都干净了，间隔回到快档"
            f"（{int(o.fast_delay_min or 20)}-{int(o.fast_delay_max or 30)}s）"
        )

    def _pick_delay(self) -> tuple[int, int, str]:
        """决定这一条要等多久

        优先级（从高到低）：
        1. **网页上当场调的间隔** → 用它（用户显式指定，优先级最高）。
           但当前是慢档时至少不低于慢档下限 —— 用户调快也得被风控兜住。
        2. **自适应**：
           · 慢档（刚撞过验证码/软风控）→ slow_delay_*
           · 否则 → fast_delay_*
        3. 都不是 → 配置里的 min_delay / max_delay
        """
        o = self.cfg.outreach
        slow = self._is_slow_mode()
        u_lo, u_hi = self._delay_min, self._delay_max
        if u_lo is not None and u_hi is not None:
            lo, hi = int(u_lo), int(u_hi)
            if slow:
                lo = max(lo, int(o.slow_delay_min or 90))
                hi = max(hi, int(o.slow_delay_max or 120))
            return max(1, lo), max(1, hi), "网页指定的间隔"
        if getattr(o, "adaptive_delay", True):
            if slow:
                return (int(o.slow_delay_min or 90), int(o.slow_delay_max or 120),
                        "慢档（撞过风控，自动退避）")
            return (int(o.fast_delay_min or 20), int(o.fast_delay_max or 30),
                    "快档（自适应）")
        return int(o.min_delay or 45), int(o.max_delay or 120), "配置里的间隔"

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

    def _set_status(
        self, lead: dict, status: str, error: str = "", synced_status: str = ""
    ) -> None:
        """回写本地 + 飞书

        干跑时**完全不改状态**（本地也不改）。
        干跑只是验证「能不能找到人、能不能挑到作品、话术长什么样」，
        一旦把状态写成 searching/found，这条线索就会卡在队列外——
        回拉也不会把它改回 pending（本地相对上次同步的值变了，走「跳过」分支），
        下一轮真跑时这些线索就再也不会被处理。
        多机共用一张飞书表时更危险：干跑会把另一台机器写的真实触达状态盖掉。

        ``synced_status``：把「上次同步进飞书的状态」一起对齐（见 store 里的说明）。
        只在「跳过、留给人工验证」这种**希望人工能放回队列**的状态上用。
        """
        if self._dry:
            return

        try:
            self.st.set_outreach_status(
                lead["id"], status, error, synced_status=synced_status
            )
        except Exception as e:
            self.log(f"  ⚠️ 本地状态回写失败: {e}")

        try:
            self._push_feishu_status(lead, status)
        except Exception as e:
            # 飞书写失败不影响主流程继续跑
            self.log(f"  ⚠️ 飞书状态回写失败: {e}")

    def _synced_marker(self, lead: dict, outreach_status: str) -> str:
        """生成「上次同步进飞书的状态」标记（处理状态|触达状态）

        回拉判断「本地这次有没有自己改过」全靠这个标记。
        跳过是**留给人工验证**的状态：人工在飞书表里把它改回「待触达」，
        回拉必须能采纳 —— 所以标记要跟当前值一致，
        否则回拉永远认为「本地改过」，人工的改动永远不生效。
        """
        from ..notify.bitable import STATUS_LABELS

        st = STATUS_LABELS.get(lead.get("status") or "", "待处理")
        return f"{st}|{FEISHU_STATUS.get(outreach_status, outreach_status)}"

    def _push_feishu_status(self, lead: dict, status: str) -> None:
        """把「触达状态」写回飞书多维表格（**异步**，不阻塞触达）

        复用 notify.bitable 的写入器，用 patch_fields 只改这一个字段——
        不能用整体替换（会把销售改过的「处理状态」清掉）。

        ⚠ 为什么不是直接同步调：飞书走 lark-cli 子进程，实测一次 1.4 秒，
        每条线索要写 3 次 → 每条白等 4.2 秒。改成后台线程排队写。
        """
        record_id = lead.get("bitable_record_id") or ""
        if not record_id:
            return
        text = FEISHU_STATUS.get(status, status)
        self._feishu.push(record_id, {"触达状态": text})

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
        t_run = time.time()

        sent_today = self.st.sent_today()
        if not dry and sent_today >= daily_limit:
            msg = f"今天已发 {sent_today} 条，达到每日上限 {daily_limit}，收工"
            self.log(msg)
            return {"ok": True, "stopped": msg, "total": 0, "sent": 0,
                    "failed": 0, "skipped": 0, "daily_limit_reached": True}

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
        # 每条线索「撞验证后重试」的次数（lead_id → 次数）
        # 用来给「验证过了立刻回头重试」加个上限，防止
        # 「验证→重试→又弹验证→再验证」变成死循环。
        lead_retries: dict[int, int] = {}
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

                idx = 0
                while idx < len(picked):
                    if self.stop_reason:
                        break
                    # 连续模式：新进来的线索插队（见 _promote_hot_leads）
                    if getattr(o, "auto_continuous", False):
                        self._promote_hot_leads(picked, idx)
                    lead = picked[idx]
                    idx += 1
                    # 记下「手上正在处理哪条」——
                    # 轮次异常结束时靠它把这条退回队列（见 _release_current_lead）
                    self._current_lead = lead
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

                        # 撞过风控 → 间隔切慢档并记住（见 _enter_slow_mode）
                        # 抖音开始要验证/限流，说明频率踩线了，
                        # 下一轮继续按快档跑等于加速把号搞废。
                        self._enter_slow_mode(f"{kind}: {str(e)[:60]}")

                        # 验证码要人工过：叫人 + 把窗口叫回来 + 停下等他。
                        # 注意这跟「账号被限流」是两回事：
                        # 限流是平台不给过了，验证码是平台要你证明你是人。
                        if kind == KIND_VERIFY:
                            self._bring_window_back()
                            self._notify_risk(KIND_VERIFY, str(e), lead)
                            if self._wait_for_human(str(e)):
                                # 人工验证完了 → **回头重试这一条**，不是跳到下一条。
                                #
                                # 为什么（2026-11-13 用户反馈「触发了验证的抖音号好像被直接跳过了」）：
                                #   验证码就是在**搜这个人**的时候弹的——人已经找到门口了，
                                #   验证一过就该马上去敲他的门。跳到下一条 = 这个人白排一次队，
                                #   还得再等下一轮（队列里几十个人，轮到他可能是一小时后）。
                                try:
                                    # 搜索阶段这条路径以前**没清**验证状态：
                                    # 粘住的话，下一条线索刚开跑就被自己的旧状态拦死
                                    # （跟发送阶段 2026-11-11 踩的是同一个坑）。
                                    dy.clear_verify()
                                except Exception:
                                    pass
                                self._set_status(lead, STATUS_PENDING, "验证码已人工处理，重新排队")
                                self.stop_reason = ""
                                self._stop_kind = ""
                                self._notify_resumed(lead)
                                retries = lead_retries.get(lead["id"], 0) + 1
                                lead_retries[lead["id"]] = retries
                                max_retry = max(1, int(getattr(o, "verify_retry_max", 2) or 2))
                                if retries <= max_retry:
                                    # idx 在上面已经 +1 过，这里 -1 退回来 → 回头处理同一条
                                    idx -= 1
                                    self.log(f"↩ 验证已通过，回头重试这条（{retries}/{max_retry}）")
                                else:
                                    self.log(
                                        f"↩ 验证已通过，但这条重试 {retries} 次仍撞验证，"
                                        f"留在队列里下轮接着来"
                                    )
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
                        # 「这一次没搜到」≠「这个人没了」：
                        #   抖音一次只给 6~10 个人，目标常在第 2、3 页；
                        #   而且搜索结果是概率性的（同号 18:36 搜不到、18:50 搜得到）。
                        # 实测（2026-11-13）：抽查 4 个「搜不到」的号，3 个当场就搜到了。
                        # 所以先标成 not_found —— 留在队列里、排在一堆正常线索**后面**，
                        # 等正常线索做完了再回头重试。
                        #
                        # 但「永远可重试」也有代价：失效的抖音号每轮都被捞出来重搜，
                        # 白占名额还永远不消失。所以记一个**累计次数**：
                        # 搜够 N 次还搜不到 → 多半这人真搜不到了（改名/注销/被隐藏），
                        # 跳过（终态），留给人工验证（用户 2026-11-13 指示）。
                        nf_count = 0 if self._dry else self.st.bump_not_found(lead["id"])
                        nf_threshold = int(getattr(o, "not_found_skip_threshold", 3) or 3)
                        if nf_count >= nf_threshold:
                            note = f"连续 {nf_count} 次搜不到这个人，已跳过待人工验证"
                            self.log(f"  🚫 {note}")
                            # 同步标记对齐到当前值 —— 回拉据此判断「本地改过没」。
                            # 不对齐的话，销售在飞书表里把它改回「待触达」永远不生效，
                            # 而「跳过 → 人工验证 → 放回队列」正是这个状态存在的意义。
                            self._set_status(
                                lead, STATUS_SKIPPED, note,
                                synced_status=self._synced_marker(lead, STATUS_SKIPPED),
                            )
                            self.st.add_touch(
                                lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
                                display_id=lead["display_id"], nickname=lead.get("nickname") or "",
                                status="skipped", error=note,
                            )
                            self._emit(type="skipped", lead=lead, error=note)
                        else:
                            self._set_status(lead, STATUS_NOT_FOUND, str(e))
                            self.st.add_touch(
                                lead_id=lead["id"], record_id=lead.get("bitable_record_id") or "",
                                display_id=lead["display_id"], nickname=lead.get("nickname") or "",
                                status="not_found", error=str(e),
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
                    except SearchNotReady as e:
                        # 搜索**动作没在页面上跑起来** —— 这不是限流！
                        #
                        # 为什么不能按限流处理（2026-11-13 现场日志）：
                        #   12:36:58  判「搜索接口被限流」（探针搜「小米」也空）
                        #   12:38:31  同样的搜索 **16.7 秒就成功了**
                        # 抖音的限流窗口活不过 90 秒，把它当铁证 →
                        #   · 白等 90~120 秒慢档
                        #   · 线索退回 pending
                        #   · 一轮下来少触达一批人
                        #
                        # 而「动作没跑起来」是**我们自己没把搜索做出来**
                        #（页面没渲染完 / 回车没触发 / 点 tab 没点上），
                        # 退避对它一点用没有。
                        #
                        # 所以：不切慢档，退回 pending 下一轮再试。
                        # 但仍然计入连续异常 —— 连续多次都做不出搜索，
                        # 说明浏览器/页面出了真问题，该停下来等人看一眼。
                        consecutive_errors += 1
                        consecutive_not_found = 0
                        summary["skipped"] += 1
                        self.log(f"  🔁 {e}（不是限流，下轮再试）")
                        self._set_status(
                            lead, STATUS_PENDING,
                            f"搜索没跑起来，下轮重试：{str(e)[:80]}",
                        )
                        self._emit(type="failed", lead=lead, error=str(e))
                        if consecutive_errors >= max_err:
                            self.stop_reason = (
                                f"连续 {consecutive_errors} 条都做不出搜索"
                                f"（搜索动作没跑起来，不是限流），主动停手"
                            )
                            self._stop_kind = KIND_SEARCH
                            self.log(f"🛑 {self.stop_reason}")
                            self._emit(type="risk", lead=lead,
                                       error=self.stop_reason, kind=KIND_SEARCH)
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
                        # 搜索接口被限流 → 切慢档。
                        # 为什么：限流就是「你搜太频繁了」的信号，这时候继续快档
                        # 等于拿限流去撞限流，越撞越死（实测：每分钟空撞一轮，
                        # 一小时下来把 100 多条线索全判了死刑）。
                        self._enter_slow_mode(f"search: {str(e)[:60]}")
                        # ⚠ 这里**不能**把线索标成 failed。
                        #
                        # 搜索接口被限流是**平台侧的故障**，跟这个人有没有效毫无关系
                        # （同一个人换个时间可能就搜得到，限流是随机窗口）。
                        # 而待触达队列只挑 outreach_status='pending'——
                        # 一旦标成 failed，这条线索就**永久出局**，再也不会被触达。
                        # 每撞一次限流就悄悄吃掉一批线索，等于静默的数据损失
                        # （实测 2026-11-13：「测试昵称F」只是撞上一次搜索限流就被标 failed）。
                        #
                        # 所以设回 pending，下一轮接着试。
                        # 30 天去重靠 outreach_records，不靠这个状态，所以不会重复打扰。
                        self._set_status(
                            lead, STATUS_PENDING,
                            f"搜索接口被限流，稍后重试：{str(e)[:80]}",
                        )
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
                        if _is_transient_error(e):
                            # 偶发的（页面超时、连接被掐断）→ 留在队列里下轮再试，
                            # 别因为一次网络抖动就把这条线索判死刑。
                            self._set_status(
                                lead, STATUS_PENDING,
                                f"临时故障，稍后重试：{str(e)[:80]}",
                            )
                        else:
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
                        if _is_transient_error(e):
                            self._set_status(
                                lead, STATUS_PENDING,
                                f"临时故障，稍后重试：{type(e).__name__}: {str(e)[:60]}",
                            )
                        else:
                            self._set_status(
                                lead, STATUS_FAILED,
                                f"{type(e).__name__}: {str(e)[:120]}",
                            )
                        self._emit(type="failed", lead=lead, error=str(e))
                        if consecutive_errors >= max_err:
                            self.stop_reason = f"连续 {consecutive_errors} 次异常，主动停手"
                            break
                    else:
                        if summary.get("sent"):
                            sent_in_run += 1
                            # 干干净净的一条 → 计数够了就把慢档撤掉
                            self._clean_streak += 1
                            self._maybe_exit_slow_mode()

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
            # 轮次异常结束（撞验证超时、进程被杀、抛异常）时，
            # 手上那条线索会停在「搜索中 / 已定位」——这俩都不是终态，
            # 而待触达队列只挑 pending → 这条线索就**永远没人管了**。
            # 实测（2026-11-13）：有 3 条卡在 searching/found 里出不来。
            self._release_current_lead()
            self._page = None
            self._dy = None
            # 飞书状态是后台线程写的 → 收尾时等它排空，
            # 别让「已留言」这种关键状态丢在半路（多机同步要靠它）
            if not self._feishu.flush(timeout=30.0):
                self.log("  ⚠️ 飞书状态还有没写完的（不影响本轮结果）")

        note = self.stop_reason or ("干跑" if dry else "")
        self.st.finish_run(
            run_id, sent=summary["sent"], failed=summary["failed"],
            skipped=summary["skipped"], note=note, kind=self._stop_kind,
        )
        summary["stopped"] = self.stop_reason
        # 停手的**种类**要结构化带出去（不是靠文案猜）——
        # 自动调度器靠它判断「是等人验证」还是「只是这轮跑完了」，
        # 决定要不要暂停自动触达。靠解析文案迟早会漂。
        summary["stop_kind"] = self._stop_kind
        elapsed = time.time() - t_run
        done = len(picked) or 1
        self.log(
            f"\n本轮结束：发 {summary['sent']}　干跑 {summary['dry']}　"
            f"失败 {summary['failed']}　跳过 {summary['skipped']}"
            + (f"　停止原因：{self.stop_reason}" if self.stop_reason else "")
        )
        self.log(
            f"⏱ 总耗时 {elapsed/60:.1f} 分钟　平均每条 {elapsed/done:.0f} 秒"
            f"（含条间等待；每条的明细看上面「⏱ 找人/发送/回查」）"
        )
        self._emit(type="done", summary=summary)
        return summary

    # ================================================================
    # 单条线索
    # ================================================================

    def _promote_hot_leads(self, picked: list[dict], idx: int) -> int:
        """把「新进来的线索」换到当前这条前面（连续模式）

        为什么需要
        ----------
        用户要求（2026-11-12）：「包括有新进的也会自动触达」。

        一轮开始时挑好的名单是个**快照**，而一轮要跑十几分钟。
        期间新抓到的线索——尤其是刚在直播间问价的那种——
        要等到下一轮才被处理，转化窗口就过了。

        所以每处理一条之前重新看一眼队列：
        有**比当前这条更新**、且这轮还没碰过的人，就把他换到最前面先做。

        换下来的那条不丢，排到队尾，本轮照样会处理到（总数不变）。

        代价：每条一次 DB 查询（毫秒级）。
        换来的是：热线索几分钟内被触达，而不是等一整轮。
        """
        o = self.cfg.outreach
        try:
            fresh = self.st.pending_leads(
                intent_levels=list(o.intent_levels or []),
                stores=list(o.stores or []),
                min_score=float(getattr(o, "min_score", 0) or 0),
                sort_by=str(o.sort_by or "recent"),
            )
        except Exception as e:
            # ⚠ 这里不能静默：查询失败 = 插队功能整个失效，
            # 而外面看起来一切正常（只是新线索没被提前）。
            # 记一条日志，别让这种问题无声无息。
            self.log(f"  ⚠️ 插队检查失败（不影响本轮继续）: {str(e)[:80]}")
            return 0

        current = picked[idx] if idx < len(picked) else {}
        cur_hit = str(current.get("last_hit_at") or "")
        in_batch = {str(ld.get("display_id") or "") for ld in picked}
        dedupe_days = int(o.dedupe_days or 30)

        for ld in fresh:
            did = str(ld.get("display_id") or "")
            if not did or did in in_batch:
                continue
            # 30 天内真的发出去过的不再打扰
            try:
                if self.st.recent_touch_days(did, dedupe_days):
                    continue
            except Exception:
                pass
            # 只提升「比当前这条更新」的——把老的换上来没意义
            hit = str(ld.get("last_hit_at") or "")
            if cur_hit and hit and hit <= cur_hit:
                continue
            # 换上去：**总数不变**（轮次上限不能被插队撑破）
            # 插入到当前这条前面，被挤下来的顺延到本轮后面；
            # 队尾那条本轮不做（它还是 pending，下轮会被重新挑中）。
            picked.insert(idx, ld)
            picked.pop()
            self.log(
                f"  ⏫ 新进来的线索插到前面：{did}　{ld.get('nickname') or ''}"
            )
            return 1
        return 0

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

    def _harvest_profile_contact(self, info: dict, lead: dict) -> None:
        """从抖音主页简介里补联系方式

        为什么要有这一步（用户 2026-11-13 要求）：
        有些客户**不在弹幕里留电话**，直接把自己的联系方式写在**个人简介**里。
        弹幕里没有联系方式 ≠ 这个人没法联系 —— 只看弹幕会漏掉一大半。

        合并规则（跟弹幕来源的联系方式放一起）：
        - 简介里的**同类**联系方式**不覆盖**弹幕里已有的
          （弹幕是客户当场说的，更贴近这次问价；简介只负责「补」）
        - 简介里的**不同类**（弹幕给了电话、简介给了微信）→ 追加，两个都留着

        简介本身如果是广告引流（"加微信了解"那种），**照样记**：
        那个微信号是他本人的，是真是同行让销售自己看内容判断，
        程序替他们做主反而会漏掉真客户。
        """
        signature = str(info.get("signature") or "").strip()
        if not signature:
            return
        c = extract_contacts([signature])
        if not c.has_contact:
            return
        try:
            changed, m_type, m_value = self.st.set_contact(
                lead["id"], c.contact_type, c.value, prefer_new_same_type=False)
        except Exception as e:
            self.log(f"  （写联系方式失败：{str(e)[:60]}）")
            return
        if not changed:
            # 联系方式本来就有（而且类型一样）→ 不用动，也不用同步飞书
            self.log(f"  📞 简介里有联系方式，但已有的是同类型，保留原来的：{m_value}")
            return
        lead["contact_type"] = m_type
        lead["contact_value"] = m_value
        hint = "（像是引流内容，销售自己判断）" if c.is_spam else ""
        self.log(f"  📞 简介里有联系方式：{m_value}{hint}")
        # 同步飞书「联系方式」列（异步写，不阻塞触达）
        record_id = lead.get("bitable_record_id") or ""
        if record_id:
            self._feishu.push(record_id, {"联系方式": m_value})

    def _handle_one(self, dy: Douyin, lead: dict, summary: dict) -> None:
        """处理一条线索：找人 → 挑作品 → 生成话术 → 发送 → 回查"""
        o = self.cfg.outreach
        dry = self._dry

        # ---- 逐阶段计时 ----
        #
        # 为什么加这个：一轮触达要十几分钟，光看「⏳ 等 62s」这种日志
        # 根本看不出时间花在哪。以前只能靠翻日志猜，现在每条都直接报出来。
        t_lead = time.time()
        _stage = [t_lead]

        def _mark(name: str) -> None:
            now = time.time()
            self.log(f"     ⏱ {name} {now - _stage[0]:.1f}s")
            _stage[0] = now

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
        # 简介里的联系方式（有人习惯把电话写在简介里，不在弹幕里说）
        self._harvest_profile_contact(info, lead)
        _mark("找人")

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
        _mark("挑作品")

        # c. 生成话术
        c = self.cw.generate(self._as_copywriter_lead(lead))
        if not c.get("ok"):
            raise DouyinError(f"文案不合格：{c.get('reason')}")
        text = c["text"]
        self.log(f"  ✍️ [{c['template_id']}] {text}")
        _mark("生成话术")

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
        _mark("发送")

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
            _mark("回查")

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
        self.log(f"     ⏱ 本条合计 {time.time() - t_lead:.1f}s")

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
            # ⚠ slow_mo=0：以前是 120ms，也就是**每个** Playwright 动作都硬等
            #   120 毫秒。一条线索要走 40~60 个动作，光这一项就白等 5~7 秒。
            #   那个 120 是均匀延迟，既不像真人（真人快慢不均），
            #   又白白吃掉每条好几秒 —— 真人节奏靠代码里那些随机等待体现，
            #   不靠这个均匀延迟。
            with Browser(profile_dir, headless=headless, slow_mo=0,
                         window_mode=mode,
                         block_media=block_media) as (_c, page):
                yield page

        return _ctx()
