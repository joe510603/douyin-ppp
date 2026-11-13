"""抖音网页端操作：按抖音号找人 → 拉他最近的作品 → 在作品下发一条留言。

实现思路（也是这个工具最关键的取舍）：
- 不逆向签名（a_bogus/X-Bogus 天天变），而是「让页面自己发请求」：
  用 Playwright 打开搜索页/主页/视频页，页面自己带签名调内部接口，
  我们只监听响应（page.on("response")）把 JSON 抓下来。
  这样接口怎么变签名都不用管，页面能出来我们就能拿到数据。
- 写操作（发评论）走 UI：点评论框 → 输入 → 发送，然后监听 publish 响应确认结果。
  比直接构造请求稳，也更接近真人操作。
"""
from __future__ import annotations

import json
import pathlib
import random
import time
import urllib.parse
from typing import Any, Callable, Optional


from playwright.sync_api import Page, Response

DOUYIN = "https://www.douyin.com"

# ================================================================
# 风控 / 验证信号
# ================================================================
# 「要人来验证」→ 停下叫人
# 「账号被限制」→ 直接停，扫码没用
#
# 踩过的坑（2026-11-11 实测）：抖音发评论时会弹**「接收短信验证码」**对话框
# 「为确保是本人操作抖音账号，请输入当前手机号 xxx 收到的短信验证码」。
# 这个弹窗**不在**常见的验证码关键词里 → 之前完全没被识别，
# 于是流程一路走下去，最后被误判成「账号被限流（软风控）」。
#
# 教训：抖音要「本人操作验证」时可能用**短信**而不是图形验证码，
# 关键词必须覆盖弹窗的**实际文案**，不能只认「安全验证」四个字。
VERIFY_TEXTS = (
    "请完成安全验证",
    "安全验证",
    "验证码控件",
    "接收短信验证码",
    "为确保是本人操作",
    "请输入验证码",
    "获取验证码",
    "短信验证码",
)

# 账号被限制的信号——这些不是「人来验证一下」能解决的
#
# ⚠️ 这里的词要**明确表示限制**。「请稍后再试」原来也在表里，
# 但它太泛了——网络抖一下、页面加载慢，页面都可能写这句，
# 判成限流会直接锁 120 分钟 + 发一条「扫码没用」的错误通知。
# 误判「限流」的代价太大，宁可漏判也不能乱判。
LIMIT_TEXTS = (
    "操作频繁",
    "访问频繁",
    "账号存在异常",
    "环境异常",
)

RISK_TEXTS = VERIFY_TEXTS + LIMIT_TEXTS

# ================================================================
# 停手的类型（统一词汇表）
# ================================================================
#   verify：平台要人工验证（短信/扫码/图形）→ 叫人，停下等他
#   limit ：账号被限制 → 直接停，扫码没用
#   login ：登录态没了 → 叫人扫码
#   search：**搜索接口**被限流（账号是好的！）→ 停手，但不用扫码
#
# 之前 worker 用「captcha」、异常用「verify」，两套词 → 日志里出现过
# 「触发风控：抖音要求验证本人操作」紧跟着「账号被限流（软风控）」这种自相矛盾。
# 现在统一成一种写法。
#
# ⚠️ search 必须单独一种，不能塞进 limit（2026-11-12）：
# 抖音 web 端搜索接口会**静默限流**——不报错、不提示，就是给你一个空的
# user_list。账号本身完全正常（浏览、发评论都不受影响，手机 App 也能搜到）。
# 把它当「账号被限流」会锁 120 分钟冷却 + 发一条「扫码没用」的错误通知，
# 方向完全错：这里该做的是**少搜索、等一等**，不是扫码。
KIND_VERIFY = "verify"
KIND_LIMIT = "limit"
KIND_LOGIN = "login"
KIND_SEARCH = "search"


def normalize_kind(kind: str) -> str:
    """把各种写法统一成 verify / limit / login / search"""
    k = (kind or "").strip().lower()
    if k in ("verify", "captcha", "验证码", "验证"):
        return KIND_VERIFY
    if k in ("login", "登录"):
        return KIND_LOGIN
    if k in ("search", "搜索", "搜索接口"):
        return KIND_SEARCH
    return KIND_LIMIT


class DouyinError(Exception):
    pass


class NotLoggedIn(DouyinError):
    pass


class RiskControl(DouyinError):
    """验证码 / 操作频繁 / 账号异常：必须停下来，不能硬刚。

    ``kind`` 决定怎么处理：
    - ``verify``：要人工验证（短信验证码 / 图形验证码）→ 叫人，停下等他
    - ``limit``：账号被限制 → 直接停，扫码没用

    把它放在异常里而不是靠调用方重新匹配关键词，
    是为了**只有一套判据**——两处判据迟早会漂。
    """

    def __init__(self, message: str, kind: str = "limit"):
        super().__init__(message)
        self.kind = kind


class NoWorks(DouyinError):
    """这个人没有公开作品（或作品全私密），没法留言。属于「跳过」，不是故障。"""


class LeadNotFound(DouyinError):
    """这个人搜不到（抖音号改名 / 注销 / 被隐藏）。属于「跳过」，**不是故障**。

    为什么单独一个类型（2026-11-12 用户反馈）：
    「连续搜不到人」被算成「连续 N 次异常」→ 3 条就停手。
    但搜不到人本来就是**大量存在的正常情况**（线索池里很多号早就失效了），
    拿它当异常等于因为「正常的坏数据」把整轮掐断，效率极低。

    它**不计入**连续异常计数，所以不会触发「连续 N 次异常，主动停手」。
    真出故障（网络断了、页面打不开、风控）照样会停。
    """


class SearchUnavailable(DouyinError):
    """搜索接口返回不了任何结果 —— 限流或故障。

    跟 ``LeadNotFound`` 的区别（本轮最关键的一处区分，2026-11-12）：

    - ``LeadNotFound``：搜索接口**正常返回了用户**，只是目标不在里面
      → 说明这个人真的改名/注销了 → 正常业务结果，跳过，不算故障
    - ``SearchUnavailable``：搜索接口**一个用户都没返回**
      → 接口坏了 / 被限流 → 真故障，该停手

    ⚠️ 别跟「要求验证」搞混：抖音要你过拼图验证才能搜人时，
    响应里会带 ``search_nil_info.search_nil_type="verify_check"``，
    那种情况 ``_search`` 会先抛 ``RiskControl(kind=verify)``
    （走「叫人过验证码 → 验证完自动接着跑」那条路），
    根本到不了这个异常。

    为什么还要区分：
    用户抽查「999999101、999999102、999999103」，说手机上都能搜到。
    实际上 web 端搜索这边要么被限流、要么被要求验证——
    抖音不报错、不弹提示，就是给你一个空的 user_list
    （实测 status_code=0、cursor=10、user_list=[]），首页推荐流完全正常。

    之前这两种情况都当成「搜不到」，于是：
    - 被限流时，程序以为「这些人都失效了」，接着往下跑 → 纯空转
    - 真正失效的号，反而混在限流里看不出是哪回事

    所以判断口径是：**接口有没有返回用户**（而不是有没有搜到目标）。
    """


class SearchNotReady(DouyinError):
    """搜索**动作根本没在页面上跑起来** —— 这不是限流

    为什么要有这个类型（2026-11-13 实测）：
    「搜不到用户」一直有两种含义被混为一谈：
      a) 抖音被限流 → 真的停手
      b) **页面压根没发起搜索**（搜索框没渲染出来 / 回车没触发 / 点了 tab 但接口没对上）
         → 这跟限流毫无关系，是**我们自己没把搜索做出来**

    怎么分清（靠证据，不靠猜）：
      · 抓到了 ``discover/search`` 响应、里面 ``user_list`` 是空的 → 抖音限流（a）
      · **一个**用户列表响应都没抓到 → 搜索动作没跑起来（b）

    证据来自 2026-11-13 的现场日志：
      12:36:58 判「搜索接口被限流」（探针搜「小米」也空）
      12:38:31 同样的搜索 **16.7 秒就成功了**
    —— 抖音的限流窗口活不过 90 秒，却被当成了铁证，
    于是白等 90~120 秒慢档，还把线索退回队列。

    处理：退回 pending 下一轮再试，**不切慢档、不算异常停手**
    （真限流才值得退避；这是偶发的自己做坏了）。
    """


class Douyin:
    def __init__(self, page: Page, shot_dir: str | pathlib.Path | None = None,
                 log: Callable[[str], None] = print):
        self.page = page
        self.log = log
        self.shot_dir = pathlib.Path(shot_dir) if shot_dir else None
        if self.shot_dir:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
        # 抖音的风控裁决（来自 passport/safe/query_decision/）
        #   account_flow = "verify" → 要本人验证（短信/扫码），不是被限流
        #   account_flow = "block"  → 账号被限制
        # 这是平台自己给的结论，比我们猜页面文案准得多。
        #
        # ⚠️ 必须带时间戳：这个状态是「粘」的——
        # 人工验证通过之后如果不清掉，下一条线索会在**搜索阶段**
        # 就被自己的旧状态拦死（实测 2026-11-11：验证完第一条，第二条直接停）。
        self.verify_flow = ""
        self.verify_flow_at = 0.0
        self.verify_ways: list[str] = []
        # ---- 搜索接口的健康状态 ----
        # 抖音 web 端搜索会**静默限流**：不报错、不提示，就是给你一个空的
        # user_list（实测 status_code=0、cursor=10、user_list=[]）。
        # 所以「搜某人返回 0 个用户」有两种完全不同的含义，
        # 得靠探针（搜一个一定搜得到的词）才能分清。
        #
        # ⚠️ 这里存的是**三态**，不是布尔（2026-11-13）：
        #   ok        = 接口正常
        #   throttled = 抖音真限流（回话了，只是用户列表是空的）
        #   not_run   = 搜索动作压根没跑起来（**不是限流**，是自己没做成）
        # 以前只有布尔，把 not_run 也当成 throttled →
        # 白等 90~120 秒慢档，还白白丢掉一批线索。
        self._search_health_at = 0.0
        self._search_health_state = "ok"
        self._search_health_reason = ""
        # 最近一次搜索返回的用户（批量缓存 sec_uid 用）
        self.last_search_users: list[dict] = []
        # 主页作品缓存（sec_uid → 作品列表）
        # 已知 sec_uid 时「定位」和「挑作品」合成一次开页面，省一半时间
        self._works_cache: dict[str, list[dict]] = {}
        try:
            page.on("response", self._on_decision)
        except Exception:
            pass

    def clear_verify(self) -> None:
        """人工验证通过后调用：清掉「要验证」状态

        不清的话，这个状态会一直挂着，下一条线索刚开跑就被拦死。
        """
        self.verify_flow = ""
        self.verify_flow_at = 0.0
        self.verify_ways = []

    def _verify_pending(self) -> bool:
        """当前是不是「平台刚判了要验证」

        只认**刚刚**的裁决（60 秒内）——过了这个窗口它就是过期状态，
        不该再拦后面的动作。
        """
        if self.verify_flow != "verify":
            return False
        return (time.time() - self.verify_flow_at) < 60

    def _on_decision(self, r: Response) -> None:
        """盯抖音的风控裁决接口

        实测（2026-11-11）：发评论时抖音返回
            account_flow = "verify"
            verify_ways = [mobile_sms_verify, qr_code_verify]
        也就是「要证明是本人操作」，**不是**账号被限流。
        之前没看这个接口，只靠页面文案猜，才误判成限流。
        """
        try:
            if "safe/query_decision" not in (r.url or ""):
                return
            j = json.loads(r.text())
        except Exception:
            return
        d = j.get("data") or {}
        flow = str(d.get("account_flow") or "")
        if flow:
            self.verify_flow = flow
            self.verify_flow_at = time.time()
        ways = []
        for w in (d.get("verify_ways") or []):
            v = w.get("verify_way") or ""
            if v:
                ways.append(v)
        if ways:
            self.verify_ways = ways

    # ------------------------------------------------------------------ 基础
    def _shot(self, tag: str) -> str | None:
        if not self.shot_dir:
            return None
        p = self.shot_dir / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        try:
            self.page.screenshot(path=str(p), full_page=False)
            return str(p)
        except Exception:
            return None

    def _body_text(self) -> str:
        try:
            return self.page.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    def check_risk(self) -> None:
        """每次都过一遍风控信号；命中就抛 RiskControl，由调度器整体停机。"""
        url = self.page.url or ""
        if "/verify" in url or "captcha" in url:
            self._shot("risk_captcha")
            raise RiskControl("跳转到安全验证页", kind="verify")
        # 平台自己的裁决最准：account_flow=verify 就是「要本人验证」
        # 只认刚判的（60 秒内），过期的裁决不该继续拦后面的动作
        if self._verify_pending():
            self._shot("risk_verify")
            ways = "、".join(self.verify_ways) if self.verify_ways else ""
            raise RiskControl(
                "抖音要求验证本人操作才能发评论"
                + (f"（可用的验证方式：{ways}）" if ways else ""),
                kind=KIND_VERIFY)
        txt = self._body_text()
        for kw in VERIFY_TEXTS:
            if kw in txt:
                self._shot("risk_verify")
                raise RiskControl(f"页面要求人工验证（{kw}）", kind="verify")
        for kw in LIMIT_TEXTS:
            if kw in txt:
                self._shot("risk_text")
                raise RiskControl(f"页面提示：{kw}", kind="limit")
        # 抖音页面常驻一个空的 #captcha_container 壳子，count>0 不代表真有验证码。
        # 只有「占位够大 + 里面有文字」才算真验证码。
        #
        # ⚠️ 但光靠这一条不够（2026-11-12 实测踩到）：
        # 搜索被要求验证时，验证码本体在**跨域 iframe** 里
        # （rmc.bytedance.com/verifycenter/captcha/v2），
        # 父页面的 innerText 是**空的** → 「里面有文字」永远不成立
        # → 验证码明明弹出来了，程序却说「没有风控信号」，接着往下跑，
        #    最后把「搜不到人」当成「这些人全失效了」。
        #
        # 所以还得看 iframe 元素本身（它在父页面里，看得到 src 和尺寸）。
        try:
            real_captcha = self.page.evaluate(
                """() => {
                  // ① 容器里有文字（老规则）
                  const sels = ['#captcha_container','.captcha_verify_container','.captcha-verify-container'];
                  for (const s of sels) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    if (r.width > 120 && r.height > 60 && (el.innerText || '').trim().length > 0) return true;
                  }
                  // ② 可见的验证码 iframe（跨域，父页面拿不到它的文字）
                  for (const f of document.querySelectorAll('iframe')) {
                    const src = f.getAttribute('src') || '';
                    // 先排除「无验证码」壳子：抖音页面**常驻**一个
                    // rc-verifycenter/rmc-nocaptcha 的 iframe（实测 display:none、0x0），
                    // 它只是验证框架的空壳，不是真的验证码。
                    // 不排除的话，它哪天被渲染成可见的就会误判成「要验证」，
                    // 触达会莫名其妙停下来叫人。
                    if (/nocaptcha/i.test(src)) continue;
                    if (!/verifycenter\\/captcha|captcha\\/v\\d|verify_center|verifycenter/i.test(src)) continue;
                    const r = f.getBoundingClientRect();
                    if (r.width < 60 || r.height < 60) continue;
                    const cs = getComputedStyle(f);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    return true;
                  }
                  return false;
                }"""
            )
        except Exception:
            real_captcha = False
        if real_captcha:
            self._shot("risk_captcha_dom")
            # ⚠️ 必须带 kind：验证码是「要人来验一下」，
            # 不是「账号被限流」。不带 kind 的话，
            # post_comment 的重试逻辑会把它当限流直接抛掉，
            # 走不到「叫人 + 等人工验证 + 回查」那套流程。
            raise RiskControl("出现验证码控件", kind=KIND_VERIFY)

    def cookies_ok(self) -> bool:
        try:
            names = {c["name"] for c in self.page.context.cookies(f"{DOUYIN}/")}
            return "sessionid" in names or "sessionid_ss" in names
        except Exception:
            return False

    def ensure_login(self) -> None:
        if not self.cookies_ok():
            raise NotLoggedIn("没有登录态（cookie 里没有 sessionid），先跑 `python -m dtouch login` 扫码")

    def _goto(self, url: str, wait: str = "domcontentloaded", timeout: int = 45000,
              retries: int = 2) -> None:
        """跳页面，失败重试

        为什么要重试：抖音偶尔会返回 net::ERR_NETWORK_CHANGED
        （本地网络抖动 / 采集器的 WebSocket 太多时更容易碰上）。
        一次失败就放弃的话，整条线索就白跑了。
        """
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self.page.goto(url, wait_until=wait, timeout=timeout)
                return
            except Exception as e:
                last = e
                if attempt >= retries:
                    break
                self.log(f"  （页面打不开，重试 {attempt + 1}/{retries}）")
                self.page.wait_for_timeout(2000)
        if last is not None:
            raise last

    @staticmethod
    def _parse_jsonish(raw: str) -> list[dict]:
        """抖音的 stream 接口返回的是「多个 JSON 拼在一起」（NDJSON），
        直接 json.loads 会报 Extra data，所以用 raw_decode 一段段切。"""
        out: list[dict] = []
        if not raw:
            return out
        dec = json.JSONDecoder()
        i, n = 0, len(raw)
        while i < n:
            while i < n and raw[i] in " \r\n\t":
                i += 1
            if i >= n:
                break
            try:
                obj, end = dec.raw_decode(raw, i)
                i = end
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                i += 1
        return out

    # 抓响应：先挂监听 → 执行动作 → 边等边解析，直到某条响应满足 expect
    def _capture(self, url_part, action: Callable[[], None], timeout: int = 9000,
                 expect: Callable[[dict], bool] | None = None, settle: int = 600,
                 mid_hook: Optional[Callable[[], None]] = None,
                 pager: Optional[Callable[[], None]] = None,
                 found: Optional[Callable[[list], bool]] = None,
                 page_budget: int = 12000,
                 evidence: Optional[dict] = None,
                 abort_if: Optional[Callable[[], bool]] = None) -> list[dict]:
        """执行 action，同时抓指定接口的响应

        url_part: 匹配 URL 的子串，也可以传一组子串（任一命中就算）
        mid_hook: action 之后、开始等待响应之前执行的回调
                  （用来做「等页面稳定 → 点某个 tab」这类事）
        pager: 首屏出来之后，用来「翻出下一页」的回调（往下滚）
        found: 判断是不是已经找到目标了（参数是已抓到的所有响应）
        page_budget: 翻页最多花多少毫秒
        evidence: 把「到底发生了什么」的证据写进这个 dict（给上层判断用）
        abort_if: 每轮等待前问一次，返回 True 就**立刻放弃等待**、返回已抓到的
                  （用在「动作压根没跑起来」时，别白等满 timeout）
        """
        parts = (url_part,) if isinstance(url_part, str) else tuple(url_part)
        hits: list[Response] = []
        parsed: list[dict] = []
        done: list[bool] = [False]
        cursor: list[int] = [0]

        if evidence is not None:
            evidence.update({"hits": 0, "user_hits": 0, "empty_bodies": 0,
                             "statuses": [], "parsed": 0})

        def on_resp(r: Response) -> None:
            try:
                if any(p in r.url for p in parts):
                    hits.append(r)
            except Exception:
                pass

        def drain() -> None:
            while cursor[0] < len(hits):
                r = hits[cursor[0]]
                cursor[0] += 1
                raw = ""
                try:
                    raw = r.text()
                except Exception:
                    pass
                try:
                    parsed.extend(self._parse_jsonish(raw))
                except Exception:
                    pass
                if evidence is not None:
                    evidence["hits"] += 1
                    try:
                        evidence["statuses"].append(r.status)
                    except Exception:
                        pass
                    if not (raw or "").strip():
                        # 抖音限流最典型的表现：HTTP 200 但**响应体是空的**
                        evidence["empty_bodies"] += 1
                    if any("user_list" in j for j in parsed if isinstance(j, dict)):
                        evidence["user_hits"] += 1
                if expect and any(expect(j) for j in parsed):
                    done[0] = True

        self.page.on("response", on_resp)
        try:
            action()
            # 预热钩子：比如「等搜索结果页出来 → 点『用户』tab」
            if mid_hook:
                try:
                    mid_hook()
                except Exception as e:
                    self.log(f"  （预热动作没成功：{str(e).splitlines()[0][:50]}）")
            end = time.time() + timeout / 1000
            while time.time() < end:
                # 提前放弃：动作压根没跑起来就别白等满 timeout
                # （实测：搜索没跑起来 → 白等 40 秒 → 被误判成「限流」）
                if abort_if is not None:
                    try:
                        if abort_if():
                            break
                    except Exception:
                        pass
                self.page.wait_for_timeout(250)
                drain()
                if done[0]:
                    break
            self.page.wait_for_timeout(settle)
            drain()

            # ---- 翻页：目标常常不在第一页 ----
            #
            # 抖音一次只给 6~10 个用户，后面还有（has_more=1）。
            # 实测（2026-11-13）：999999111 在 cursor=30 那页才出现，
            # 以前只等第一页就下结论「这个人搜不到」→ 线索被永久跳过，
            # 而用户抽查发现这些人**全都搜得到**。
            #
            # 所以首屏之后继续往下滚，逼抖音把下一页吐出来，
            # 直到找到目标 / 抖音说没有下一页 / 翻页预算用完。
            if pager is not None:
                page_end = time.time() + page_budget / 1000
                while time.time() < page_end:
                    if found is not None and found(parsed):
                        break
                    if self._no_more_pages(parsed):
                        break
                    pager()
                    self.page.wait_for_timeout(900)
                    drain()
            if evidence is not None:
                evidence["parsed"] = len([j for j in parsed if j])
            return [j for j in parsed if j]
        finally:
            try:
                self.page.remove_listener("response", on_resp)
            except Exception:
                pass

    def _no_more_pages(self, parsed: list[dict]) -> bool:
        """抖音说「没有下一页了」吗

        只看**用户列表**那个响应的 has_more —— 综合 tab 的响应里
        也有 has_more，但它跟用户列表不是一回事。
        """
        for j in parsed or []:
            if not isinstance(j, dict):
                continue
            if "user_list" in j and j.get("has_more") in (False, 0):
                return True
        return False

    def _scroll_more_users(self) -> None:
        """在搜索结果页往下滚，逼抖音加载下一页用户"""
        try:
            self.page.mouse.wheel(0, 3000)
        except Exception:
            pass

    # ------------------------------------------------------------ 找人（抖音号）
    def _click_user_tab(self, timeout: int = 12000) -> bool:
        """点搜索结果页的「用户」tab

        为什么必须点（2026-11-11 实测踩到）：

        抖音搜索框按回车后，默认落在「**综合**」tab，返回的是**视频结果**：
        - 请求 general/search/stream（响应体是空的，读不到）
        - 请求 general/search/single?search_channel=aweme_general（214KB，但里面没有 user_list）

        用户列表要**点一下「用户」tab**才会发另一个请求：
            /aweme/v1/web/discover/search/?search_channel=aweme_user_web
        那个接口的响应里**顶层就有 user_list**，才是我们要的东西。

        不点 tab → 一个用户都搜不到，日志全是「搜索无用户结果」。
        （当时 10 条线索全部搜不到，看起来像账号被限流，其实是 tab 没点。）

        tab 的 class 是哈希化的（每次构建都会变），所以只能靠**精确文字 + 可见**来定位。
        """
        end = time.time() + timeout / 1000
        while time.time() < end:
            try:
                clicked = self.page.evaluate(
                    """() => {
                      // 优先用抖音自己的 tab class（'semi-tabs-tab' 是稳定的）
                      const named = document.querySelectorAll(
                        '[class*="semi-tabs-tab"], [class*="search-tab"], [class*="tab-item"]');
                      for (const el of named) {
                        if ((el.innerText || '').trim() === '用户') { el.click(); return true; }
                      }
                      // 兜底：精确文本 + 没有子元素 + 可见
                      for (const el of document.querySelectorAll('div,span,a,li')) {
                        if ((el.innerText || '').trim() !== '用户') continue;
                        if (el.children.length !== 0) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 1 || r.height < 1) continue;
                        el.click();
                        return true;
                      }
                      return false;
                    }"""
                )
            except Exception:
                clicked = False
            if clicked:
                return True
            self.page.wait_for_timeout(400)
        return False

    # 抖音在搜索结果里给「为什么搜不到」的结构化说明。
    #
    # 2026-11-12 实测（这是本轮最关键的发现）：
    #   搜索接口不返回用户时，响应里带着
    #       "search_nil_info": {"search_nil_type": "verify_check", ...}
    #   verify_check = **抖音要求先完成安全验证（拼图滑块）才能搜人**。
    #
    # 也就是说这根本不是「限流」，也不是「这些人全失效了」，
    # 而是「你得先过个验证码」。页面会弹一个拼图滑块
    # （rmc.bytedance.com/verifycenter/captcha/v2，跨域 iframe）。
    #
    # 为什么之前一直没认出来：
    #   · 页面正文里没有「安全验证」几个字（验证码在 iframe 里）→ 关键词匹配全落空
    #   · 接口 status_code=0、不报错 → 看起来像「就是没结果」
    # 于是程序一路当成「搜不到人」，把整批线索判成失效。
    #
    # 用平台自己的裁决，比猜页面文案准得多（同 passport/safe/query_decision 那个道理）。
    SEARCH_NIL_VERIFY = "verify_check"

    @staticmethod
    def _search_nil_type(data: list[dict]) -> str:
        """从搜索响应里读 ``search_nil_info.search_nil_type``"""
        for j in data or []:
            if not isinstance(j, dict):
                continue
            nil = j.get("search_nil_info")
            if isinstance(nil, dict):
                t = str(nil.get("search_nil_type") or "").strip()
                if t:
                    return t
        return ""

    # 搜索动作在页面上「真的执行了」的判据：URL 跳到了搜索结果页
    _SEARCH_URL_MARK = "/search/"

    def _type_in_search_box(self, q: str, timeout: int = 3000) -> bool:
        """在**当前页面**的搜索框里输入 q 并回车

        返回 False = 这一页没有可用的搜索框（调用方去回首页）。

        ⚠ 为什么不能直接用 ``.first``（2026-11-13 实测）：
        搜索结果页上有**两个**匹配 ``input[placeholder*="搜索"]`` 的元素 ——
        一个是可见的（"搜索你感兴趣的内容"，392×36），
        另一个是**隐藏的**（尺寸 0×0，是搜索弹层里的那个，placeholder 就叫「搜索」）。
        DOM 顺序一变，``.first`` 就会挑中隐藏的那个，
        ``wait_for(visible)`` 等满 8 秒 → 白等 + 这次搜索直接废掉。
        所以用 ``:visible`` 只挑真正看得见的那个。
        """
        try:
            box = self.page.locator('input[placeholder*="搜索"]:visible').first
            box.wait_for(state="visible", timeout=timeout)
            box.click()
            self.page.wait_for_timeout(random.randint(200, 500))
            box.fill(q)
            self.page.wait_for_timeout(random.randint(200, 500))
            self.page.keyboard.press("Enter")
            return True
        except Exception as e:
            self.log(f"  （这页的搜索框用不了：{str(e).splitlines()[0][:50]}）")
            return False

    def _wait_search_url(self, url_before: str, q: str = "",
                         timeout: float = 8.0) -> bool:
        """等 URL 跳到搜索结果页 —— 证明搜索动作**真的执行了**

        为什么必须有这个判定（2026-11-13 用户亲眼看到的现象）：
        「日志写着『搜索：测试昵称A』，浏览器上却根本没有搜索动作」。
        没有这个判定，代码会白等满 40 秒超时，
        然后把「没搜到」当成「被限流」——
        而其实是自己没把搜索做出来（首页没渲染完 / 回车没触发）。

        抖音搜索完会跳到 ``/jingxuan/search/{关键词}``，
        所以「URL 里有 /search/」就是搜索动作执行了的证据。

        ⚠️ 光比「URL 变了」不够：**搜同一个词**时 URL 可能一模一样
        （探针固定搜「小米」，连着两次就会撞上），
        所以再补一条「URL 里含这个词」的判据。
        """
        needle = urllib.parse.quote(str(q or ""), safe="")
        deadline = time.time() + timeout
        while time.time() < deadline:
            now = self.page.url or ""
            if self._SEARCH_URL_MARK in now:
                if now != url_before:
                    return True
                # URL 没变 → 可能是搜了同一个词，看 URL 里有没有这个词
                if needle and needle in now:
                    return True
            self.page.wait_for_timeout(200)
        return False

    def _search(self, q: str, expect_hit: bool = True,
                want_ids: Optional[list[str]] = None,
                evidence: Optional[dict] = None) -> list[dict]:
        """像真人一样用搜索框搜。

        实测（2026-11）：直接 goto /search/{q}?type=user 页面是空的（SPA 路由不认），
        只有在首页搜索框里输入并回车，跳到 /jingxuan/search/{q} 才会出结果。

        ⚠️ 抖音搜索默认落在「综合」tab（返回视频），
        用户列表**必须点一下「用户」tab**才会请求
            /aweme/v1/web/discover/search/?search_channel=aweme_user_web
        那个接口的响应里顶层就有 ``user_list``。
        （2026-11-11 实测：不点 → 10 条线索全部「搜索无用户结果」，
        看着像账号被限流，其实是 tab 没点、接口对不上。）

        ``expect_hit=False`` 是**探针模式**：不等「搜到目标」，
        只要接口吐出任何一个用户就算成功（用来判断接口死没死）。

        ``want_ids`` = 目标人的抖音号。传了它就会**往下翻页**去找，
        直到找到目标 / 抖音说没下一页了（抖音一次只给 6~10 个人，
        目标常常在第 2、3 页）。

        ``evidence`` = 把「到底发生了什么」写进这个 dict：
          · ``search_ran``  搜索动作在页面上真的执行了吗
          · ``user_hits``   抓到了几个**用户列表**响应（>0 = 抖音回话了）
          · ``hits``        抓到了几个搜索相关响应（>0 但 user_hits=0 = tab 没点上）
        上层靠它分清「真限流」和「我们自己没把搜索做出来」——
        这两件事以前被混为一谈，代价是白等 90 秒 + 白白丢掉一批线索。
        """
        ev: dict = evidence if evidence is not None else {}

        def do_search() -> None:
            """在页面上真的执行一次搜索"""
            url_before = self.page.url or ""
            # 先试**当前页**的搜索框：
            #   · 少加载一次首页 = 快 3~5 秒
            #   · 也少一次「首页还没渲染完、搜索框点不着」的机会
            #     （实测搜索没跑起来时，浏览器上看起来就是「根本没搜」）
            #
            # ⚠ 但浏览器刚启动时页面是 about:blank（连 douyin.com 都不是），
            #   在那儿等搜索框纯属白等 8 秒 —— 直接去首页。
            cur = self.page.url or ""
            if cur and "douyin.com" in cur:
                if self._type_in_search_box(q) and self._wait_search_url(url_before, q, 8):
                    ev["search_ran"] = True
                    return
                # ⚠ 输入进去了、页面却没跳 → 在**当前页**再敲一次
                #   （抖音搜索框有时要再敲一下才触发）。
                #   这比「回首页重搜」快一个数量级 —— 回首页实测要 80+ 秒。
                if self._type_in_search_box(q) and self._wait_search_url(url_before, q, 6):
                    ev["search_ran"] = True
                    return
            # 当前页不行 → 回首页重搜一次
            #
            # ⚠ 只等「响应头」（commit），**不等整页资源**：
            #   实测 2026-11-13，等 domcontentloaded 要 80+ 秒
            #   （首页很重，而且机器上同时跑着直播检测的十几个浏览器），
            #   而搜索框是首屏元素，commit 之后 1~2 秒就点得到。
            #   这一步是触达吞吐的大头：省下 70 秒 × 每一条 = 一轮省 20+ 分钟。
            try:
                self._goto(f"{DOUYIN}/", wait="commit", timeout=20000, retries=0)
            except Exception as e:
                self.log(f"  （回首页失败：{str(e).splitlines()[0][:50]}）")
                ev["search_ran"] = False
                return
            self.page.wait_for_timeout(random.randint(1500, 2500))
            url_before = self.page.url or ""
            if self._type_in_search_box(q, timeout=8000):
                ev["search_ran"] = self._wait_search_url(url_before, q, 10)
            else:
                ev["search_ran"] = False

        def hit(j: dict) -> bool:
            """响应到了就算数，**不强求「搜到目标」**

            ⚠ 这里原来要求「必须搜到目标」才停，于是两种最常见的情况
              每次都要白等满 20 秒超时：
                · 这个人抖音号已经失效（结果里根本没有他）
                · 搜索接口被限流（user_list 是空的）
              实测 2026-11-12 干跑：一条线索两次搜索白烧 40 秒，
              而整条线索的其它部分加起来才 6 秒。

            现在改成：**用户 tab 的响应一回来就停**
            （那个响应带 user_list 字段，空的就是空结果 —— 那是最终答案），
            目标在不在里面，等拿到数据之后另外判。

            ⚠ 只认 user_list 这个键（不认其它形状）：
              免得「综合 tab」那个早到的响应把等待提前截断，
              反而错过「用户 tab」里真正的结果。
            """
            if "user_list" in j:
                return True
            if not expect_hit:
                return bool(self._extract_users([j]))
            return False

        # discover/search = 用户 tab 的接口（user_list 在顶层）
        # general/search  = 综合 tab 的接口（兜底，偶尔也能捞到 user_info）
        #
        # ⚠ timeout 必须给足余量（2026-11-12 实测）：
        #   抖音的**用户列表接口响应要 20 秒**（实测：请求 +6.2s 发出，
        #   响应 +26.2s 才回来；同一次会话里其它搜索接口都是 6 毫秒返回）。
        #   原来超时正好是 20000ms → 响应在临界点回来，经常**刚好错过**
        #   → 被判成「搜不到人」。
        #
        #   这个代价还不止是慢：搜到人之后会把这一页十几个用户的 sec_uid
        #   一起缓存下来，后面十几条线索就能直接开主页、完全不用搜。
        #   搜失败 → 缓存不起来 → 每条每轮都重复付这 20 秒（死循环）。
        # 目标人的抖音号（要翻页找的就是它）
        want = {str(x).strip().lower() for x in (want_ids or []) if str(x or "").strip()}

        def _hit_target(parsed: list[dict]) -> bool:
            """目标是不是已经在结果里了"""
            if not want:
                return False
            for u in self._extract_users(parsed):
                ids = {str(u.get("unique_id") or "").lower(),
                       str(u.get("short_id") or "").lower()}
                if want & ids:
                    return True
            return False

        data: list[dict] = []
        for attempt in range(2):
            ev.update({"search_ran": False, "hits": 0, "user_hits": 0,
                       "empty_bodies": 0, "statuses": [], "parsed": 0})
            data = self._capture(
                ("discover/search", "general/search"),
                do_search, timeout=40000, expect=hit, settle=1500,
                mid_hook=self._click_user_tab,
                pager=self._scroll_more_users if want else None,
                found=_hit_target if want else None,
                page_budget=15000,
                evidence=ev,
                # 搜索动作压根没跑起来 → 别白等满 40 秒
                abort_if=lambda: not ev.get("search_ran"),
            )
            users = self._extract_users(data)
            if users:
                break
            if ev.get("user_hits"):
                # 抖音回话了，只是用户列表是空的 → **这是答案**，不是故障
                break
            # 一个用户列表响应都没有 —— 这**不是限流**：
            #   · search_ran=False → 页面压根没发起搜索（动作没跑起来）
            #   · hits>0           → 只回了综合 tab 的响应（「用户」tab 没点上）
            # 这两种都是**我们自己没把搜索做出来**，重做一遍就好。
            if attempt == 0:
                self.log("  （没拿到用户列表，重做一次搜索）")
                continue
            break
        # 首屏没命中时再多等一会儿（分几段推过来的）
        if not self._extract_users(data):
            self.page.wait_for_timeout(1500)

        # ---- 平台自己说「要验证」→ 直接按验证处理，别当成搜不到 ----
        #
        # 这是 2026-11-12 那个 bug 的根因：
        # 抖音要求先过拼图验证才能搜人，接口就回一个空 user_list，
        # 还贴心地在 search_nil_info 里写明「verify_check」。
        # 以前没人读这个字段 → 程序以为「这些人全失效了」→ 一路空转。
        nil_type = self._search_nil_type(data)
        if nil_type == self.SEARCH_NIL_VERIFY:
            self._shot("search_needs_verify")
            raise RiskControl(
                "抖音要求先完成安全验证才能搜索（搜索结果为空是因为触发了验证）",
                kind=KIND_VERIFY,
            )
        return data

    # 探测搜索接口用的「金丝雀」关键词。
    # 必须是一个**一定搜得到用户**的词，而且不会撞上任何一条待触达线索。
    # 「小米」是汽车经销商场景里最稳的选择（小米SU7/小米汽车/小米之家…）。
    SEARCH_CANARY = "小米"

    # 探测结果的复用窗口（秒）。
    # 一次失败别马上又探一次——探针本身也是一次搜索，
    # 搜索次数本身就是被限流的资源。
    SEARCH_HEALTH_TTL = 300

    # 「接口被限流」这个结论**过期得更快**。
    #
    # 抖音的限流是概率性的：实测同一条线索连着搜两次，
    # 第一次成功、第二次就空了（2026-11-12）。也就是说它可能几十秒后就恢复，
    # 没必要为了一次限流就放弃 5 分钟的搜索。
    SEARCH_UNHEALTHY_TTL = 60

    def _health_ttl(self) -> int:
        """探针结论的有效期：正常的结论留久一点，被限流的结论过期快"""
        return (self.SEARCH_HEALTH_TTL
                if self._search_health_state == "ok"
                else self.SEARCH_UNHEALTHY_TTL)

    def _search_known_throttled(self) -> bool:
        """已知搜索接口此刻是废的（探针结论还在有效期内）

        用来在**搜索之前**就短路 —— 明知搜了也是白等 20~40 秒，
        不如直接告诉 worker「搜索被限流了」。

        ⚠ 只有**真限流**才短路。
        「搜索动作没跑起来」（not_run）不算限流 —— 实测它 90 秒后就自己好了，
        当成限流去退避纯属白白浪费一轮触达。
        """
        if self._search_health_state != "throttled" or not self._search_health_at:
            return False
        return (time.time() - self._search_health_at) < self.SEARCH_UNHEALTHY_TTL

    def _probe_search_health(self, force: bool = False) -> bool:
        """探测搜索接口是不是还活着（返回 True=接口正常）

        为什么需要探针（2026-11-12 用户反馈的关键）：
        「搜某人 → 返回 0 个用户」有两种完全不同的含义，从单次搜索里**分不出来**：

        a) 这个抖音号真的不存在（改名/注销了）→ 正常业务结果，跳过就行
        b) 搜索接口被限流了，一个用户都吐不出来 → 真故障，该停手

        抖音被限流时**不报错、不弹提示**，就是静默给一个空的 user_list。
        所以之前两种情况混在一起，分不清「这批人都失效了」还是「接口坏了」。

        解法：再搜一个**一定搜得到**的词（金丝雀）。
          · 金丝雀能搜到        → 接口是好的 → 目标不在结果里 → LeadNotFound（跳过）
          · 金丝雀回了个空列表  → **真限流** → SearchUnavailable（停手）
          · 金丝雀压根没搜起来  → **不是限流**（我们自己没做成）→ SearchNotReady

        ⚠️ 最后这一档是 2026-11-13 补的（现场证据）：
          12:36:58 判「搜索接口被限流」（探针搜「小米」也空）
          12:38:31 同样的搜索 **16.7 秒就成功了**
        抖音的限流窗口活不过 90 秒，把它当铁证 →
        白等 90~120 秒慢档 + 线索退回队列 + 少触达一批人。

        ⚠️ 探针结果要缓存：探针自己也是一次搜索，
        每条搜不到的线索都探一次 = 搜索次数翻倍 = 更快撞上限流。
        """
        if not force and (time.time() - self._search_health_at) < self._health_ttl():
            return self._search_health_state == "ok"
        canary = self.SEARCH_CANARY
        ev: dict = {}
        try:
            data = self._search(canary, expect_hit=False, evidence=ev)
            users = self._extract_users(data)
            healthy = bool(users)
        except RiskControl:
            # 探针也撞上「要验证」→ 那是**要人过验证码**，
            # 不是「接口坏了」。往上抛，走人工验证那条路。
            raise
        except Exception as e:
            self.log(f"  ⚠️ 探测搜索接口失败：{str(e).splitlines()[0][:80]}")
            healthy = False
            ev = {}
        self._search_health_at = time.time()
        if healthy:
            self._search_health_state = "ok"
            self._search_health_reason = ""
            self.log(f"  🔎 探测：搜「{canary}」能搜到 {len(users)} 个用户 → 搜索接口正常")
        elif ev.get("user_hits"):
            # 抖音给了用户列表响应，只是里面是空的 → **真限流**
            self._search_health_state = "throttled"
            self._search_health_reason = "throttled"
            self.log(f"  🔎 探测：搜「{canary}」返回了**空的用户列表** → 搜索接口被限流了")
        else:
            # 一个用户列表响应都没抓到 → **不是限流**，是搜索动作没跑起来
            self._search_health_state = "not_run"
            self._search_health_reason = "not_run"
            self.log(f"  🔎 探测：搜「{canary}」**压根没发起搜索请求**"
                     f"（不是限流，是搜索动作没跑起来）")
        return healthy

    def resolve(self, unique_id: str, nickname: str | None = None) -> dict | None:
        """抖音号/短 ID → {sec_uid, nickname, unique_id, aweme_count, signature}。

        搜不到抖音号时（改名/只填了昵称/号本身失效），用表格里的昵称再试一次，
        但**认人必须靠抖音号**——昵称只能用来扩大搜索池。
        （详见 ``_resolve_once`` 里那段注释。）
        """
        dy_id = str(unique_id or "").strip().lstrip("@")
        info = self._resolve_once(dy_id)
        if info:
            return info

        nick = self._clean_nickname(nickname)
        if not nick:
            return None
        self.log(f"  ↪ 抖音号没搜到，改用昵称再搜一次：{nick}")
        info = self._resolve_once(nick, by_nickname=True, want_dy=dy_id)
        if info:
            return info

        # 两条路都没找到 → 默认**就到这里**（2026-11-13 用户拍板砍掉第三次搜索）
        #
        # 第三次就是「把第 1 次原样重做一遍」，只为了对付抖音的概率性。
        # 实测当天它触发 2 次，**0 次**救回过人，每次却要赔上 20~40 秒
        # （抖音的用户列表接口自己就要 20 秒）。
        # 而搜不到的人会被直接跳过（终态），下一轮不会再碰他 ——
        # 真需要再给他一次机会，人工在飞书里改回「待触达」即可。
        #
        # （抖音的概率性是**跨轮**的：同一个号 18:36 搜不到、18:50 搜得到。
        #  那是「下一轮再试」能救的场景，不是「同一轮里 20 秒后再试」。）
        #
        # 想打开：配置 search_retry=True。
        if self._search_retry_enabled():
            self.log("  ↪ 昵称也没搜到，用抖音号再搜一次（抖音搜索结果不稳定）")
            return self._resolve_once(dy_id)
        return None

    # 抖音的昵称字段本身可能就是**截断显示**的（"测试昵称C…"）。
    _NICK_ELLIPSIS = ("...", "…", "..")

    @classmethod
    def _clean_nickname(cls, nickname: str | None) -> str:
        """把昵称末尾的省略号去掉

        为什么（2026-11-12 实测）：
        抖音给的是**截断昵称**（"测试昵称C…"，3 个字符）。
        拿它去搜索等于搜「测试昵称C…」这个带省略号的词 —— 永远搜不到，
        实测就是这么被判成「这个人搜不到」的。
        去掉省略号后搜「测试昵称C」，抖音的模糊匹配能命中「测试昵称C在迁徙」。

        ⚠ 但去掉省略号之后，昵称**不再唯一**——
        不同的人会撞同一个截断昵称。所以昵称只能用来「扩大搜索池」，
        认人还得靠抖音号（见 ``_resolve_once``）。
        """
        s = str(nickname or "").strip()
        changed = True
        while changed:
            changed = False
            for tail in cls._NICK_ELLIPSIS:
                if s.endswith(tail):
                    s = s[: -len(tail)].strip()
                    changed = True
        return s

    def _search_retry_enabled(self) -> bool:
        """抖音号搜索失败后要不要重试一次（抖音的结果是概率性的）"""
        try:
            from ..config import get_config

            return bool(getattr(get_config().outreach, "search_retry", True))
        except Exception:
            return True

    def _resolve_once(self, unique_id: str, by_nickname: bool = False,
                      want_dy: str = "") -> dict | None:
        self.ensure_login()
        q = str(unique_id or "").strip().lstrip("@")
        if not q:
            return None
        # ---- 已知接口此刻是废的 → 别再搜一次 ----
        #
        # 抖音搜索被限流时，**一次搜索要白等 20~40 秒**（实测响应要 20 秒，
        # 而被限流时可能连响应都不给）。
        # 探针的结论已经在缓存里了，就别明知是白等还去搜 ——
        # 直接告诉 worker「搜索被限流」，让它按该停手的方式停手。
        if self._search_known_throttled():
            raise SearchUnavailable(
                f"搜索接口被限流（探针结论还在有效期内，这次不搜了）：{q}"
            )
        self.log(f"  搜索：{q}")
        # 传目标抖音号 → 第一页没命中就往下翻页找
        # （抖音一次只给 6~10 个人，目标常常在第 2、3 页；
        #   实测 999999111 在 cursor=30 那页才出现）
        want = [want_dy] if (by_nickname and want_dy) else [q]
        data = self._search(q, want_ids=want)
        users = self._extract_users(data)
        # 留着这一页的用户，worker 会顺手把 sec_uid 存下来
        # （抖音一次搜索返回一页十几个用户，每个都带 sec_uid，
        #   存下来后面那些线索就不用再搜了）
        self.last_search_users = users
        text = self._body_text()
        if not users and "登录后即可搜索" in text:
            raise NotLoggedIn("搜索需要登录：页面上是「登录后即可搜索」")
        self.check_risk()

        if not users:
            # 一个用户都没有 → 先探一下接口死活，再决定是「人不在」还是「接口坏了」
            #
            # 这一步是本轮最关键的区分（2026-11-12）：
            # 抖音 web 端搜索被限流时**不报错**，就是给你一个空的 user_list。
            # 不探一下就会把「接口被限流」误当成「这些人都失效了」，
            # 然后一路空转下去；反过来把真失效的号当成限流，
            # 也会停错手、发错通知。
            healthy = self._probe_search_health()
            if not healthy:
                # ⚠️ 探针的结论要**按证据**分两种（2026-11-13）：
                #   throttled = 抖音回话了、用户列表是空的 → **真限流**
                #   not_run   = 压根没抓到用户列表响应 → 搜索动作没跑起来，
                #               这跟限流无关（实测 90 秒后同样的搜索就成功了）
                # 以前两种都抛 SearchUnavailable → 切慢档 90~120 秒、
                # 线索退回队列，白白少触达一批人。
                if self._search_health_state == "not_run":
                    raise SearchNotReady(
                        f"搜索动作没跑起来（页面没发起搜索请求）：搜「{q}」"
                    )
                raise SearchUnavailable(
                    f"搜索接口返回空（疑似被抖音限流）：搜「{q}」一个用户都没返回，"
                    f"探针搜「{self.SEARCH_CANARY}」同样为空"
                )
            # 接口是好的 → 目标真的不在结果里
            if by_nickname:
                self.log(f"  ⚠️ 昵称也没搜到：{q}")
            else:
                self.log(f"  ⚠️ 没搜到抖音号 {q}（搜索接口正常，就是没有这个人）")
            return None

        ql = q.lower()
        hit_user = None

        def _ids_of(u: dict) -> set[str]:
            """这个人的抖音号（unique_id 是自定义号，short_id 是自动号，两个都算）"""
            return {str(u.get("unique_id") or "").lower(),
                    str(u.get("short_id") or "").lower()}

        # 1) 直接按搜索词匹配抖音号（抖音号搜索走这条路）
        for u in users:
            if ql and ql in _ids_of(u):
                hit_user = u
                break

        # 2) 昵称搜索：搜索词是昵称，但**认人得靠抖音号**
        #
        # 为什么不能只认昵称（2026-11-12 实测）：
        # 抖音给的是**截断昵称**（"测试昵称C…"，3 个字符），不同的人会撞同一个昵称。
        # 实测搜「测试昵称C…」返回了一个 unique_id=999999112 的人，
        # 而我们要找的是 unique_id=999999106 —— **认错人了**。
        # 认错人 = 拿客户的号给陌生人发广告，比跳过还糟。
        if not hit_user and by_nickname:
            want = str(want_dy or "").strip().lower()
            if want:
                for u in users:
                    if want in _ids_of(u):
                        hit_user = u
                        break
                if not hit_user:
                    self.log(
                        f"  ⚠️ 昵称搜到 {len(users)} 个候选，"
                        f"但没有一个的抖音号对得上 {want_dy}"
                        f"（不乱认人，跳过）"
                    )
            else:
                # 没有抖音号可对（纯昵称线索），只能认昵称
                for u in users:
                    if str(u.get("nickname") or "").strip() == q:
                        hit_user = u
                        break
        if not hit_user:
            if by_nickname:
                self.log(f"  ⚠️ 昵称也没搜到：{q}")
            else:
                self.log(f"  ⚠️ 没搜到抖音号 {q}"
                         + (f"（共 {len(users)} 个候选，均不匹配）" if users else "（搜索无用户结果）"))
            return None
        if not hit_user.get("sec_uid"):
            self.log("  ⚠️ 搜到用户但没有 sec_uid，跳过")
            return None
        # 昵称搜索命中时，务必记一下到底是靠哪个号对上的——
        # 事后翻日志要能确认「认的人是不是那个人」。
        if by_nickname and want_dy:
            self.log(
                f"  ✓ 昵称搜索里找到抖音号对得上的人："
                f"{hit_user.get('unique_id') or hit_user.get('short_id')}"
            )
        return {
            "sec_uid": hit_user.get("sec_uid"),
            "nickname": hit_user.get("nickname"),
            "unique_id": hit_user.get("unique_id") or hit_user.get("short_id"),
            "signature": hit_user.get("signature"),
            "aweme_count": hit_user.get("aweme_count"),
        }

    def resolve_by_sec_uid(self, sec_uid: str) -> dict | None:
        """已知 sec_uid → 直接开主页（**跳过搜索**）

        为什么值得单独一条路（2026-11-12）：
        抖音 web 端搜索接口会**静默限流**——不报错、不提示，
        就是给你一个空的 user_list（实测 status_code=0、cursor=10、user_list=[]）。
        但主页 ``/user/{sec_uid}`` 一点都不受影响。

        所以「记住上次定位到的 sec_uid」价值很大：
          · 少一次搜索 = 少一次被限流的机会
          · 搜索被限流时，这条线索照样能触达

        顺带把主页作品一起抓了（下面挑作品就不用再开一次页面）。
        """
        sec_uid = str(sec_uid or "").strip()
        if not sec_uid:
            return None
        self.ensure_login()
        self.log(f"  已有 sec_uid，跳过搜索，直接开主页：{sec_uid[:20]}…")

        def _check_alive() -> None:
            """主页一打开就判一下账号还在不在

            早点抛出去，别白等十几秒的接口超时。
            """
            body = self._body_text()
            if any(w in body for w in ("该用户已注销", "账号已注销", "用户不存在",
                                       "作品不存在", "暂时无法查看")):
                raise LeadNotFound("抖音号已失效（主页提示用户不存在/已注销）")

        works = self._fetch_works_page(sec_uid, after_open=_check_alive)
        # 存进缓存：下面挑作品时直接用，不用再开一次主页
        self._works_cache[sec_uid] = works
        nickname = ""
        aweme_count = None
        for w in works:
            if not nickname and w.get("author_nickname"):
                nickname = w["author_nickname"]
            if aweme_count is None and w.get("author_aweme_count") is not None:
                aweme_count = w["author_aweme_count"]
        # 个人简介：这条快路径**不搜索**，所以手上没有简介，得单独去取
        signature = ""
        pinfo = self.fetch_profile_info(sec_uid)
        if pinfo:
            signature = pinfo.get("signature") or ""
            if not nickname:
                nickname = pinfo.get("nickname") or ""
            # 作品接口的 author 里**没有** aweme_count（实测那个字段压根不在），
            # 所以这条快路径的作品数一直是 None，日志里显示「作品 None 条」。
            # 拿老接口的补上——只认正的，0 不采信（宁可 None 也不要误判成「没作品」
            # 而错杀一个其实有作品的人）。
            if aweme_count is None and (pinfo.get("aweme_count") or 0) > 0:
                aweme_count = pinfo["aweme_count"]
        if not signature:
            # 老接口调不通 → 退到主页 DOM（那份是截断的，好歹能捞到没被吃掉的）
            signature = self._read_profile_signature_dom()
        return {
            "sec_uid": sec_uid,
            "nickname": nickname,
            "unique_id": "",
            "signature": signature,
            "aweme_count": aweme_count,
        }

    # 抖音的**老** web 接口：拿用户资料（含完整个人简介）
    #
    # 为什么不用主页 DOM（2026-11-13 实测）：
    #   DOM 里那份简介被平台**截断 + 掩码**了 ——
    #   同一个号，DOM 是「…171专业000施工...」（末尾省略号），
    #   这个接口给的是「…171专业000施工52👍🏻222」（完整原文）。
    #   号码被吃掉的话，这个功能就白做了。
    #
    # 为什么不用新接口（/aweme/v1/web/user/profile/other/）：
    #   它要 a_bogus 签名，没签直接回 {"status_code":0,"status_msg":"blocked"}。
    #   老接口不要签名，实测连着调 25 条 0 失败、每次 0.2 秒左右。
    _OLD_USER_INFO_API = (
        "https://www.iesdouyin.com/web/api/v2/user/info/"
        "?sec_uid={sec}&aid=1128&_signature=_02B4Z6wo00f01"
    )

    def fetch_profile_info(self, sec_uid: str) -> dict | None:
        """拿用户资料：昵称 / **完整个人简介** / 抖音号 / 作品数

        为什么必须单独去取（2026-11-13 实测）：
        - 作品接口（``/aweme/v1/web/aweme/post/``）的 ``author`` 里
          **没有简介字段**：实测 author 只有 nickname / sec_uid / uid 等，
          ``signature_extra`` 和 ``profile_signature_components`` 都是 null。
        - 主页 DOM 里那份是**截断**的（见上面那个常量上面的注释）。
        - 搜索路径倒是能拿到完整简介（搜索响应 ``user_list[].user_info.signature``，
          跟这个接口给的**一模一样**），但「已知 sec_uid 直接开主页」这条快路径
          根本不搜索 → 手上没有简介。

        拿不到就返回 None，**不抛异常**：
        简介只是补充信息，拿不到不该让整条线索的触达失败。
        """
        sec_uid = str(sec_uid or "").strip()
        if not sec_uid:
            return None
        try:
            resp = self.page.request.get(
                self._OLD_USER_INFO_API.format(sec=sec_uid), timeout=20000)
            j = json.loads(resp.text())
        except Exception as e:
            self.log(f"  （取个人简介失败：{str(e)[:60]}）")
            return None
        u = j.get("user_info") or j.get("user") or {}
        if not isinstance(u, dict) or not u:
            return None
        return {
            "sec_uid": u.get("sec_uid") or sec_uid,
            "nickname": (u.get("nickname") or "").strip(),
            "unique_id": u.get("unique_id") or u.get("short_id") or "",
            "signature": (u.get("signature") or "").strip(),
            "aweme_count": u.get("aweme_count"),
        }

    def _read_profile_signature_dom(self) -> str:
        """从主页 DOM 里读个人简介（**兜底**用）

        为什么不把它当主路径：抖音在这份 DOM 里**截断 + 掩码**了简介，
        号码基本读不出来。只有老接口调不通时才用它，
        好歹能捞到没被吃掉的微信 / QQ。
        """
        try:
            txt = self.page.evaluate(
                """() => {
                  const box = document.querySelector('[data-e2e="user-info"]');
                  if (!box || !box.lastElementChild) return '';
                  let t = (box.lastElementChild.innerText || '').trim();
                  t = t.replace(/\\s*更多\\s*$/, '');
                  return t.trim();
                }"""
            ) or ""
        except Exception:
            return ""
        return str(txt).strip()

    @staticmethod
    def _extract_users(data: list[dict]) -> list[dict]:
        """把各种形状里的用户抽出来。实测 stream 里是
        {"data":[{"type":4,"user_list":[{"user_info":{...}}]}]}。"""
        users: list[dict] = []

        def take(bucket) -> None:
            for u in (bucket or []):
                if isinstance(u, dict):
                    info = u.get("user_info") if isinstance(u.get("user_info"), dict) else u
                    if isinstance(info, dict) and info.get("sec_uid"):
                        users.append(info)

        for j in data:
            if not isinstance(j, dict):
                continue
            take(j.get("user_list"))
            for it in (j.get("data") or []):
                if not isinstance(it, dict):
                    continue
                take(it.get("user_list"))
                info = it.get("user_info") or it.get("user")
                if isinstance(info, dict) and info.get("sec_uid"):
                    users.append(info)
        # 同一用户可能被多段响应重复返回
        seen, uniq = set(), []
        for u in users:
            if u.get("sec_uid") not in seen:
                seen.add(u.get("sec_uid"))
                uniq.append(u)
        return uniq

    # ------------------------------------------------------------ 拉作品列表
    def fetch_works(self, sec_uid: str, limit: int = 20) -> list[dict]:
        """拉某人主页的作品列表。

        如果 ``resolve_by_sec_uid`` 已经开过这个主页，作品是现成的，直接用
        （省一次页面加载）。否则走主路径：页面自己会调
        /aweme/v1/web/aweme/post/，监听响应拿 aweme_list；
        兜底：接口没出来/为空时，从主页 DOM 的 /video/ 链接里捡
        （必须过滤掉 source=Baiduspider 这类 SEO / 推荐位的噪音链接）。
        """
        cached = self._works_cache.get(sec_uid)
        if cached is not None:
            return cached[:limit]
        works = self._fetch_works_page(sec_uid)
        self._works_cache[sec_uid] = works
        return works[:limit]

    def _fetch_works_page(self, sec_uid: str, after_open=None) -> list[dict]:
        """真的去开一次主页抓作品（``fetch_works`` 的底层实现）

        ``after_open``：页面打开后立刻执行的回调（用来判断账号还在不在），
        在里面抛异常可以**提前中断**，省掉等接口的十几秒。
        """
        self.ensure_login()
        url = f"{DOUYIN}/user/{sec_uid}"

        def do_open() -> None:
            self._goto(url)
            self.page.wait_for_timeout(700)
            if after_open:
                after_open()
            # 主页作品是懒加载的，滚一下能让接口早点发
            try:
                self.page.mouse.wheel(0, 1400)
            except Exception:
                pass

        data = self._capture(
            "/aweme/v1/web/aweme/post/",
            do_open,
            timeout=15000,
            expect=lambda j: bool(j.get("aweme_list")),
            # ⚠ settle 是「响应到了之后再多等一会儿，把迟到的响应也捞进来」。
            #   原来 2000ms —— 每条线索白等 2 秒。
            #   作品列表是一次性返回的，等 800ms 足够。
            settle=800,
        )
        if not any((j.get("aweme_list") or []) for j in data):
            # 再滚一次给它机会（有的号挂载慢）
            data += self._capture(
                "/aweme/v1/web/aweme/post/",
                lambda: self.page.mouse.wheel(0, 1800),
                timeout=6000,
                expect=lambda j: bool(j.get("aweme_list")),
                settle=600,
            )
        self.check_risk()
        works: list[dict] = []
        for j in data:
            for a in (j.get("aweme_list") or []):
                try:
                    author = a.get("author") or {}
                    works.append({
                        "aweme_id": a.get("aweme_id"),
                        "desc": (a.get("desc") or "").strip(),
                        "create_time": a.get("create_time") or 0,
                        "is_top": bool(a.get("is_top")),
                        "digg": (a.get("statistics") or {}).get("digg_count"),
                        "comment": (a.get("statistics") or {}).get("comment_count"),
                        "duration": (a.get("video") or {}).get("duration"),
                        "type": a.get("aweme_type"),
                        # 作者昵称 / 作品总数：已知 sec_uid 直接开主页时，
                        # 就靠这两个字段把「定位到谁」补齐（不用再搜一次）
                        "author_nickname": (author.get("nickname") or "").strip(),
                        "author_aweme_count": author.get("aweme_count"),
                    })
                except Exception:
                    continue
        # 去重 + 按时间倒序（置顶的排在时间序列里，不特殊对待）
        seen, uniq = set(), []
        for w in works:
            if w["aweme_id"] and w["aweme_id"] not in seen:
                seen.add(w["aweme_id"])
                uniq.append(w)
        uniq.sort(key=lambda x: x.get("create_time") or 0, reverse=True)
        if uniq:
            return uniq

        # ---- DOM 兜底（接口空 / 没捕获到）----
        # 只认主页自己的作品网格：div[data-e2e="user-post-list"] 里的 ul[scroll-list] > li > a。
        # 页脚有一堆 source=Baiduspider 的推荐视频，绝不能拿来发评论（那是别人的作品）。
        dom: list[dict] = []
        try:
            rows = self.page.evaluate(
                """() => {
                  const out = [];
                  const box = document.querySelector('[data-e2e="user-post-list"]')
                          || document.querySelector('ul[scroll-list]');
                  if (!box) return out;
                  for (const a of box.querySelectorAll('a[href*="/video/"]')) {
                    const href = a.getAttribute('href') || '';
                    const aid = href.split('/video/')[1] ? href.split('/video/')[1].split('?')[0] : '';
                    const txt = (a.innerText || '').trim().split('\\n').filter(Boolean);
                    if (aid) out.push([aid, txt.length ? txt[txt.length - 1].slice(0, 80) : '']);
                  }
                  return out;
                }"""
            ) or []
        except Exception:
            rows = []
        seen2 = set()
        for aid, desc in rows:
            if aid and aid.isdigit() and aid not in seen2:
                seen2.add(aid)
                dom.append({"aweme_id": aid, "desc": desc or "", "create_time": 0})
        if dom:
            self.log(f"  接口没给作品，从作品网格拿到 {len(dom)} 个")
            return dom
        return []

    # ------------------------------------------------------------ 发留言
    def _scan_input(self):
        """页面上已经存在的可编辑评论框（Draft.js 版或 textarea 版）。"""
        loc = self.page.locator(
            '[data-e2e="comment-input"] [contenteditable="true"], '
            '.DraftEditor-root [contenteditable="true"], '
            '[class*="comment-input"] [contenteditable="true"], '
            '[contenteditable="true"], textarea'
        )
        try:
            n = min(loc.count(), 8)
        except Exception:
            return None
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def _wait_comment_ready(self, timeout: int = 14000) -> bool:
        """等评论区渲染出来（兼容 /video/ → /note/ 的跳转与「加载中」占位）。

        注意：光等「元素出现」不够——/note/ 跳转过程中 eval 会抛
        Execution context destroyed，元素也可能来自上一页的残留。
        所以要求 URL 连续两次不变 + 正文不再是「加载中」+ 评论区节点存在。
        """
        deadline = time.time() + timeout / 1000
        last_url = ""
        stable = 0
        while time.time() < deadline:
            try:
                cur = self.page.url
                ok = self.page.evaluate(
                    """() => {
                        const body = (document.body.innerText || '');
                        const loading = body.includes('加载中') && body.length < 400;
                        const n = document.querySelectorAll(
                          '[class*="comment-input"], .comment-mainContent, [data-e2e="comment-list"]'
                        ).length;
                        return {loading, n};
                    }""")
                stable = stable + 1 if cur == last_url else 1
                last_url = cur
                if ok.get("n") and not ok.get("loading") and stable >= 2:
                    return True
            except Exception:
                stable = 0
            # ⚠ 轮询间隔 250ms（原来 700ms）：
            #   评论区通常 1~2 秒就挂出来，700ms 一轮意味着光等它就多花 0.5~1 秒。
            #   这里只是在轮询「好了没有」，不是模拟人的操作节奏，
            #   调快不影响「像不像真人」——真人感来自那些随机等待。
            self.page.wait_for_timeout(250)
        return False

    def _has_visible_comment_input(self) -> bool:
        try:
            return bool(self.page.evaluate(
                """() => {
                    const els = document.querySelectorAll('[class*="comment-input"]');
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 40 && r.height > 10) return true;
                    }
                    return false;
                }"""))
        except Exception:
            return False

    def _open_comment_panel(self) -> bool:
        """图文页/部分视频页的评论区是收起的，点右侧评论图标展开。"""
        for sel in ('[data-e2e="feed-comment-icon"]', '[data-e2e="comment-icon"]',
                    '[class*="commentIcon"]', '[class*="comment-icon"]'):
            try:
                loc = self.page.locator(sel)
                if not loc.count():
                    continue
                el = loc.first
                if not el.is_visible():
                    continue
                el.click()
                self.page.wait_for_timeout(random.randint(500, 900))
                self.log(f"  展开评论区：click:{sel}")
                return True
            except Exception:
                continue
        return False

    def _find_input(self):
        """找评论输入框。

        2026-11 实测：视频页默认只渲染一个「留下你的精彩评论吧」的占位条
        （div.comment-input-inner-container），必须先点它，Draft.js 编辑器
        （.DraftEditor-root [contenteditable="true"]）才会长出来。
        """
        # 实测坑：图文作品（/video/{id} 会 302 到 /note/{id}）默认把评论区收起来，
        # DOM 里元素存在但宽高都是 0（is_visible=false），必须先点右侧评论图标展开。
        if self._scan_input() is None and not self._has_visible_comment_input():
            self._open_comment_panel()
        # 视频页/图文页的评论区渲染时机差很多（刚开浏览器时更慢），给 16 秒预算反复找。
        deadline = time.time() + 16
        tries = 0
        while time.time() < deadline:
            tries += 1
            el = self._scan_input()
            if el is not None:
                return el, "existing"
            # 每 3 轮再点一次评论图标：首屏没渲染完时第一次点多半点空
            if tries % 3 == 1:
                self._open_comment_panel()
            for sel in (".comment-input-inner-container", '[class*="comment-input-container"]',
                        '[class*="comment-input"]'):
                try:
                    loc = self.page.locator(sel)
                    if not loc.count():
                        continue
                    box = loc.first
                    if not box.is_visible():
                        continue
                    box.click()
                    self.page.wait_for_timeout(random.randint(350, 650))
                    el = self._scan_input()
                    if el is not None:
                        return el, f"click:{sel}"
                except Exception:
                    continue
            if time.time() < deadline:
                self.page.wait_for_timeout(random.randint(600, 1000))
        return None, None

    def _send_button_point(self) -> list[float] | None:
        """发送键是个红色纸飞机图标（path fill=#FE2C55），没有文字，
        所以按坐标点它；找不到就返回 None（外面会退回按回车）。"""
        try:
            return self.page.evaluate(
                """() => {
                  const root = document.querySelector('.commentInput-right-ct')
                            || document.querySelector('[class*="comment-"]') || document;
                  for (const p of root.querySelectorAll('path')) {
                    if ((p.getAttribute('fill') || '').toUpperCase() !== '#FE2C55') continue;
                    let el = p;
                    for (let i = 0; i < 6 && el; i++) {
                      const r = el.getBoundingClientRect();
                      if (el.tagName !== 'svg' && el.tagName !== 'path'
                          && r.width >= 14 && r.height >= 14) {
                        return [r.x + r.width / 2, r.y + r.height / 2];
                      }
                      el = el.parentElement;
                    }
                  }
                  return null;
                }"""
            )
        except Exception:
            return None

    def _input_text(self, el=None) -> str:
        """实时读评论框里的内容。元素句柄在面板重渲染后会失效，所以每次重新查。"""
        try:
            return (self.page.evaluate(
                """() => {
                    const ed = document.querySelector('.DraftEditor-root [contenteditable="true"]')
                            || document.querySelector('[class*="comment-input"] [contenteditable="true"]')
                            || document.querySelector('.comment-input-inner-container [contenteditable="true"]');
                    return ed ? (ed.innerText || '') : '';
                }"""
            ) or "").strip()
        except Exception:
            try:
                return (el.inner_text() or "").strip() if el is not None else ""
            except Exception:
                return ""

    def post_comment(self, aweme_id: str, text: str) -> dict:
        """在指定作品下发一条评论。返回 {ok, status_code, msg, comment_id, shot}。"""
        self.ensure_login()
        url = f"{DOUYIN}/video/{aweme_id}"
        self.log(f"  打开视频：{aweme_id}")
        # 坑：/video/{id} 遇到图文作品会 302 到 /note/{id}，Playwright 会抛
        # net::ERR_ABORTED。这不是真的失败，页面其实在跳，等它落稳就行。
        for attempt in (1, 2):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                break
            except Exception as e:
                msg = str(e).splitlines()[0][:70]
                if attempt == 2:
                    self.log(f"  （打开作品失败：{msg}）")
                    return {"ok": False, "status_code": -1, "msg": f"打不开作品页：{msg}"}
                self.log(f"  （页面跳转被打断：{msg}，等 2 秒重开）")
                self.page.wait_for_timeout(2000)
        # 图文作品会被 302 到 /note/{id}，正文先显示「加载中」，
        # 必须等评论区真的挂出来再动手，否则会误判「没找到评论输入框」。
        if not self._wait_comment_ready(timeout=16000):
            self.log("  （评论区没在 16 秒内挂出来，再刷新一次）")
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            self._wait_comment_ready(timeout=10000)
        self.page.wait_for_timeout(random.randint(300, 700))
        self.check_risk()

        body = self._body_text()
        if any(k in body for k in ("作品不存在", "内容不见了", "该作品已删除")):
            return {"ok": False, "status_code": -1, "msg": "作品不存在/已删除"}
        for kw in ("作者已关闭评论", "评论功能已关闭", "暂不支持评论", "该作品暂时无法评论"):
            if kw in body:
                return {"ok": False, "status_code": -1, "msg": kw}

        # 有些版本会弹「打开抖音APP」或登录浮层，先尝试关掉
        for sel in ('[data-e2e="modal-close"]', ".dy-account-close", 'div[class*="close"]'):
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=800)
                    break
            except Exception:
                pass

        el, how = self._find_input()
        if el is None:
            self._shot("no_input")
            return {"ok": False, "status_code": -1, "msg": "没找到评论输入框"}
        self.log(f"  评论框：{how}")

        try:
            el.click()
        except Exception:
            el.click(force=True)
        self.page.wait_for_timeout(random.randint(250, 600))
        # 输入（逐字敲，节奏像人）
        self.page.keyboard.type(text, delay=random.randint(25, 55))
        self.page.wait_for_timeout(random.randint(250, 600))
        # 输入框偶尔没抢到焦点，确认一下真进去了，没进去就整段插入
        entered = self._input_text(el)
        if not entered:
            try:
                el.click(force=True)
                self.page.keyboard.insert_text(text)
                self.page.wait_for_timeout(500)
            except Exception:
                pass

        def _click_send() -> str:
            """点发送键。优先级：真实元素 click > 坐标点 > 文字按钮 > 回车。
            只调用一次，绝不重复点（重复点会发出两条评论）。"""
            # ① 拿到红色纸飞机 svg 的可点祖先元素，直接 click 元素本身
            #    （比坐标稳：坐标会因面板动画/滚动而偏，元素 click 由浏览器自己算位置）
            try:
                handle = self.page.evaluate_handle(
                    """() => {
                      const root = document.querySelector('.commentInput-right-ct')
                                || document.querySelector('[class*="comment-input"]') || document;
                      for (const p of root.querySelectorAll('path')) {
                        if ((p.getAttribute('fill') || '').toUpperCase() !== '#FE2C55') continue;
                        let el = p;
                        for (let i = 0; i < 6 && el; i++) {
                          const r = el.getBoundingClientRect();
                          if (el.tagName !== 'svg' && el.tagName !== 'path'
                              && r.width >= 14 && r.height >= 14) return el;
                          el = el.parentElement;
                        }
                      }
                      return null;
                    }"""
                )
                box = handle.as_element()
                if box is not None:
                    box.click(force=True, timeout=3000)
                    return "element"
            except Exception as e:
                self.log(f"  （元素点击没成功：{str(e).splitlines()[0][:60]}）")
            # ② 退回坐标点
            pt = self._send_button_point()
            if pt:
                self.page.mouse.click(pt[0], pt[1])
                return "click"
            # ③ 文字按钮
            for btn_sel in ('button:has-text("发送")', '[class*="send"]'):
                try:
                    b = self.page.locator(btn_sel)
                    for i in range(min(b.count(), 4)):
                        if b.nth(i).is_visible() and b.nth(i).is_enabled():
                            b.nth(i).click()
                            return "button"
                except Exception:
                    continue
            # ④ 回车
            self.page.keyboard.press("Enter")
            return "enter"

        def send_once(use_enter: bool = False) -> str:
            """发一次，返回实际用的方式。绝不重复点同一个键（会发出两条）。"""
            if use_enter:
                self.page.keyboard.press("Enter")
                return "enter"
            how_used = _click_send()
            self.log(f"  发送方式：{how_used}")
            return how_used

        data: list = []
        for attempt in (1, 2):
            data = self._capture("/comment/publish", send_once, timeout=9000,
                                 expect=lambda j: "status_code" in j)
            if data:
                # 拿到明确回执就以回执为准，别被页面上的弹窗带偏
                break
            # 没有回执：先判断平台是不是要人工验证 / 限流。
            # （顺序很重要：以前这里先 check_risk，导致
            #   「抖音要求验证」的信号把「其实已发出去」的情况也误伤了。）
            self.check_risk()
            # 没有 publish 响应：先看这条评论是不是其实已经出现了
            if self._comment_appeared(text, timeout=4000):
                self.log("  ✓ 没抓到响应，但评论已出现在页面")
                return {"ok": True, "status_code": 0,
                        "msg": "未捕获响应，但评论已出现在页面", "verified": True}
            if not self._input_text():
                # 框空了但也没看见评论：可能在审核，当作「已发出但未确认」
                self.log("  ⚠️ 输入框已清空但评论尚未出现（可能在审核）")
                return {"ok": True, "status_code": 0, "unconfirmed": True,
                        "msg": "输入框已清空，评论暂未在页面出现（可能审核中）"}
            if attempt == 1:
                self.log("  ⚠️ 第一次点击没发出去，换回车再试一次")
                try:
                    el.click()
                except Exception:
                    pass
                self.page.wait_for_timeout(random.randint(300, 700))

        if not data:
            shot = self._shot("comment_unconfirmed")
            return {"ok": False, "status_code": -2,
                    "msg": "两次都没抓到发送响应，评论也没出现在页面", "shot": shot}

        status_code, msg, cid = 0, "", None
        for j in data:
            if "status_code" in j:
                status_code = j.get("status_code")
                msg = j.get("status_msg") or ""
                c = j.get("comment") or {}
                cid = c.get("cid") or c.get("comment_id")
                break
        ok = status_code == 0
        if not ok:
            # 被拒了：看看是不是「要人工验证」——那种情况人是可以解决的，
            # 必须叫人来，不能当成「发不出去」然后误判成账号限流。
            self.check_risk()
            shot = self._shot("comment_fail")
            return {"ok": False, "status_code": status_code, "msg": msg,
                    "comment_id": cid, "shot": shot}
        return {"ok": True, "status_code": status_code, "msg": msg, "comment_id": cid}

    def _comment_appeared(self, text: str, timeout: float = 8000) -> bool:
        """在页面/评论区里找刚发出的那段文字，找到才算真发出去。"""
        key = (text or "").strip()
        if not key:
            return False
        probe = key[:14]
        step = 500
        waited = 0.0
        while waited <= timeout:
            try:
                hit = self.page.evaluate(
                    """(probe) => {
                        // ⚠️ 不能拿 document.body.innerText 判断：刚敲进去的文字还在输入框里，
                        // 会恒判「已出现」。只看评论区容器，并扣掉编辑器里的文字。
                        const ed = document.querySelector('.DraftEditor-root [contenteditable="true"]');
                        const edText = ed ? (ed.innerText || '') : '';
                        const box = document.querySelector('[data-e2e="comment-list"], .comment-mainContent, div[class*="commentList"], div[class*="comment-mainContent"]');
                        let t = box ? (box.innerText || '') : (document.body.innerText || '');
                        if (!box && edText) t = t.split(edText).join('');
                        if (t.includes(probe)) return true;
                        for (const n of document.querySelectorAll('[class*="commentItem"], [data-e2e="comment-item"]')) {
                            if ((n.innerText || '').includes(probe)) return true;
                        }
                        return false;
                    }""", probe)
                if hit:
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(step)
            waited += step
        return False

    def check_comment(self, aweme_id: str, text: str, nickname: str | None = None,
                      scrolls: int = 6) -> dict:
        """回查：这条评论到底有没有出现在评论区里。

        「点完发送没报错」≠「评论真的存在」——抖音会静默过滤营销话术。
        做法和人工核对一样：打开视频 → 打开评论区 → 往下滚几屏把评论都加载出来
        → 在接口返回里找我们那段文字（找不到再看页面文字）。
        返回 {"found": bool, "total": int, "source": "api"/"dom"/"-"}
        """
        key = (text or "").strip()[:12]
        if not key or not aweme_id:
            return {"found": False, "total": 0, "source": "-"}

        got: list[dict] = []

        def on_resp(r: Response) -> None:
            try:
                if "comment/list" in r.url:
                    got.append(r)
            except Exception:
                pass

        def walk(o, out: list) -> None:
            """评论可能嵌在 comments / data / 多层里，递归挖出来。"""
            if isinstance(o, dict):
                u = o.get("user") or {}
                if isinstance(o.get("text"), str) and "cid" in o:
                    out.append((u.get("nickname") or "", o["text"]))
                for v in o.values():
                    walk(v, out)
            elif isinstance(o, list):
                for v in o:
                    walk(v, out)

        rows: list[tuple[str, str]] = []
        self.page.on("response", on_resp)
        try:
            self._goto(f"{DOUYIN}/video/{aweme_id}")
            # 图文作品会 302 到 /note/，评论区默认收起，先等页面稳、再展开面板，
            # 否则 comment/list 根本不会发出来，会误判「评论不存在」。
            self._wait_comment_ready(timeout=14000)
            self._open_comment_panel()
            for sel in ('[data-e2e="comment-icon"]', 'span:has-text("评论")',
                        'div[class*="commentIcon"]'):
                try:
                    loc = self.page.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=2000)
                        break
                except Exception:
                    continue
            self.page.wait_for_timeout(random.randint(600, 1000))
            # 滚评论列表，把分页都拉出来
            for _ in range(max(1, scrolls)):
                try:
                    self.page.evaluate(
                        """() => {
                          const box = document.querySelector('[data-e2e="comment-list"], .comment-mainContent, div[class*="commentList"]');
                          if (box) box.scrollTop = box.scrollHeight;
                          else window.scrollBy(0, 1800);
                        }"""
                    )
                except Exception:
                    pass
                self.page.wait_for_timeout(random.randint(500, 800))
            for r in got:
                try:
                    walk(r.json(), rows)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"  （回查失败：{str(e).splitlines()[0][:80]}）")
        finally:
            try:
                self.page.remove_listener("response", on_resp)
            except Exception:
                pass

        # 按 (昵称, 文字) 去重
        seen = set()
        uniq: list[tuple[str, str]] = []
        for u, t in rows:
            k = (u, t)
            if k in seen:
                continue
            seen.add(k)
            uniq.append((u, t))

        found = any(key in t for _, t in uniq)
        nick_ok = None
        if found and nickname:
            nick_ok = any((key in t) and (nickname in u) for u, t in uniq)
        if not found:
            # 接口没抓到就退回看页面文字
            try:
                dom = self.page.evaluate("() => document.body.innerText") or ""
            except Exception:
                dom = ""
            if key in dom:
                return {"found": True, "total": len(uniq), "source": "dom", "nickname_ok": None}
        return {"found": found, "total": len(uniq),
                "source": "api" if uniq else "-", "nickname_ok": nick_ok}

    # ------------------------------------------------------------------ 私信
    _DM_BAD_HINTS = ("发送失败", "操作频繁", "访问频繁", "对方未关注", "已限制", "账号异常", "私信功能已关闭")

    def _dm_panel_text(self) -> str:
        """私信面板里可见的文字。

        抖音有两种私信形态：右侧常驻面板（componentsRightPanel）和浮层小窗（popup）。
        容器找不到时直接退回 body.innerText——宁可多抓，也别把「已发出」误判成失败。
        """
        try:
            return self.page.evaluate(
                r"""() => {
                  const ed = document.querySelector('.messageMsgInputcontainer [contenteditable="true"]')
                         || document.querySelector('[data-placeholder="发送消息"]');
                  let p = ed;
                  for (let i = 0; i < 10 && p; i++) {
                    const c = (p.className || '').toString();
                    if (c.includes('componentsRightPanel') || c.includes('popupContainer')) break;
                    p = p.parentElement;
                  }
                  if (p && p.innerText && p.innerText.replace(/[\u200b\s]/g, '').length > 0) return p.innerText || '';
                  return document.body.innerText || '';
                }"""
            ) or ""
        except Exception:
            return ""

    def open_dm(self, sec_uid: str):
        """打开某人的私信面板，返回输入框元素（失败返回 None 并给出原因）。

        抖音主页上有两个「私信」按钮，第一个是 0×0 隐藏的，
        直接 locator('button:has-text("私信")').first.click() 会一直等可见性超时。
        """
        self._goto(f"{DOUYIN}/user/{sec_uid}")
        self.page.wait_for_timeout(random.randint(3500, 5200))
        self.check_risk()

        loc = self.page.locator('button:has-text("私信")')
        picked = None
        for i in range(min(loc.count(), 6)):
            b = loc.nth(i)
            try:
                if b.is_visible():
                    picked = b
                    break
            except Exception:
                continue
        if picked is None:
            self._shot("no_dm_button")
            return None, "主页上没找到可见的「私信」按钮（可能对方关闭了私信）"
        try:
            picked.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            picked.click(timeout=5000)
        except Exception:
            picked.click(force=True)
        self.log("  已点开私信面板")

        deadline = time.time() + 15
        while time.time() < deadline:
            for sel in ('.messageMsgInputcontainer [contenteditable="true"]',
                        '[data-placeholder="发送消息"]',
                        '[data-slate-editor="true"]'):
                try:
                    cand = self.page.locator(sel)
                    for i in range(min(cand.count(), 4)):
                        el = cand.nth(i)
                        if el.is_visible():
                            return el, ""
                except Exception:
                    continue
            self.page.wait_for_timeout(500)
        self._shot("no_dm_input")
        return None, "私信输入框没渲染出来（无头模式下常见，必须用有头模式跑）"

    def send_dm(self, sec_uid: str, text: str) -> dict:
        """给指定用户发一条私信。返回 {ok, msg, shot}。

        平台限制：对方回复或关注你之前，只能发送一条文字消息。
        所以调用方必须自己保证「同一个人只发一次」。
        """
        self.ensure_login()
        el, why = self.open_dm(sec_uid)
        if el is None:
            return {"ok": False, "msg": why}

        # 私信编辑器是 Slate，点一下必须真的拿到焦点，否则 keyboard.type 会打进虚空。
        # 所以要显式校验 document.activeElement，没焦点就再点/整段插入，最多试 3 轮。
        def _editor_text() -> str:
            try:
                return self.page.evaluate(
                    """() => { const e = document.querySelector('.messageMsgInputcontainer [contenteditable="true"]')
                                     || document.querySelector('[data-placeholder="发送消息"]');
                        return e ? (e.innerText || '') : ''; }"""
                ) or ""
            except Exception:
                return ""

        def _focused() -> bool:
            try:
                return bool(self.page.evaluate(
                    """() => { const a = document.activeElement;
                        return !!a && a.getAttribute && a.getAttribute('data-slate-editor') === 'true'; }"""
                ))
            except Exception:
                return False

        typed = ""
        for attempt in range(1, 4):
            try:
                el.click(timeout=4000)
            except Exception:
                try:
                    el.click(force=True)
                except Exception:
                    pass
            self.page.wait_for_timeout(random.randint(400, 800))
            if not _focused():
                # 坐标点一下编辑器中心，比 force click 更接近真人
                try:
                    box = el.bounding_box()
                    if box:
                        self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        self.page.wait_for_timeout(400)
                except Exception:
                    pass
            try:
                self.page.keyboard.type(text, delay=random.randint(25, 55))
            except Exception:
                pass
            self.page.wait_for_timeout(random.randint(500, 900))
            typed = _editor_text().strip()
            if typed:
                break
            self.log(f"  ⚠️ 私信输入第 {attempt} 次没进去，重试")
        if not typed:
            self._shot("dm_type_fail")
            return {"ok": False, "msg": "私信内容没输进去"}

        before = self._dm_panel_text()

        # 私信发送键是输入行右侧那枚红色按钮（svg.messageMsgInputpublishBtn.e2e-send-msg-btn）。
        # 实测「点一次」偶尔会落空（面板刚渲染完、按钮还没接管点击），所以这里做三层兜底：
        # ①静默点击 svg ②按按钮坐标真鼠标点 ③回车。每次点完都看输入框有没有被清空。
        def _send_once() -> str:
            for sel in (".messageMsgInputinputAction:has(svg.e2e-send-msg-btn)",
                        "svg.e2e-send-msg-btn"):
                try:
                    b = self.page.locator(sel).first
                    if not b.count():
                        continue
                    b.click(force=True, timeout=1500)
                    self.page.wait_for_timeout(1200)
                    if not _editor_text().strip():
                        return "click"
                except Exception:
                    continue
            try:
                box = self.page.locator("svg.e2e-send-msg-btn").first.bounding_box()
                if box:
                    self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    self.page.wait_for_timeout(1200)
                    if not _editor_text().strip():
                        return "mouse"
            except Exception:
                pass
            try:
                self.page.keyboard.press("Enter")
                self.page.wait_for_timeout(1200)
                if not _editor_text().strip():
                    return "enter"
            except Exception:
                pass
            return ""

        # 只点一次。私信的发送是「点了就可能已经送达」的不可逆动作，
        # 绝对不能因为「没看到反馈」就再点一次——那会给客户发重复消息。
        how = _send_once()
        self.page.wait_for_timeout(1500)
        self.check_risk()

        left = _editor_text().strip()
        panel = self._dm_panel_text()
        head = text[:12]
        bad = [w for w in self._DM_BAD_HINTS if w in panel and w not in before]

        if bad:
            shot = self._shot("dm_fail")
            return {"ok": False, "msg": f"私信被拦：{bad[0]}", "shot": shot}
        # 点过发送键就认为「已尝试投递」，不再二次点击；
        # 页面没清空也只记「未确认」，由 pipeline 决定怎么留痕。
        if how or not left:
            self.log(f"  ✓ 私信已发出（{how or 'input-cleared'}）")
            return {"ok": True, "msg": "已发送"}

        return {"ok": True, "msg": "已发送（未确认：输入框没清空，按不重发处理）"}
