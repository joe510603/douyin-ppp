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
from typing import Any, Callable


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
LIMIT_TEXTS = (
    "操作频繁",
    "访问频繁",
    "账号存在异常",
    "请稍后再试",
    "环境异常",
)

RISK_TEXTS = VERIFY_TEXTS + LIMIT_TEXTS

# ================================================================
# 停手的三种类型（统一词汇表）
# ================================================================
#   verify：平台要人工验证（短信/扫码/图形）→ 叫人，停下等他
#   limit ：账号被限制 → 直接停，扫码没用
#   login ：登录态没了 → 叫人扫码
#
# 之前 worker 用「captcha」、异常用「verify」，两套词 → 日志里出现过
# 「触发风控：抖音要求验证本人操作」紧跟着「账号被限流（软风控）」这种自相矛盾。
# 现在统一成一种写法。
KIND_VERIFY = "verify"
KIND_LIMIT = "limit"
KIND_LOGIN = "login"


def normalize_kind(kind: str) -> str:
    """把各种写法统一成 verify / limit / login"""
    k = (kind or "").strip().lower()
    if k in ("verify", "captcha", "验证码", "验证"):
        return KIND_VERIFY
    if k in ("login", "登录"):
        return KIND_LOGIN
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
        try:
            real_captcha = self.page.evaluate(
                """() => {
                  const sels = ['#captcha_container','.captcha_verify_container','.captcha-verify-container'];
                  for (const s of sels) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    if (r.width > 120 && r.height > 60 && (el.innerText || '').trim().length > 0) return true;
                  }
                  return false;
                }"""
            )
        except Exception:
            real_captcha = False
        if real_captcha:
            self._shot("risk_captcha_dom")
            raise RiskControl("出现验证码控件")

    def cookies_ok(self) -> bool:
        try:
            names = {c["name"] for c in self.page.context.cookies(f"{DOUYIN}/")}
            return "sessionid" in names or "sessionid_ss" in names
        except Exception:
            return False

    def ensure_login(self) -> None:
        if not self.cookies_ok():
            raise NotLoggedIn("没有登录态（cookie 里没有 sessionid），先跑 `python -m dtouch login` 扫码")

    def _goto(self, url: str, wait: str = "domcontentloaded", timeout: int = 45000) -> None:
        self.page.goto(url, wait_until=wait, timeout=timeout)

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
    def _capture(self, url_part: str, action: Callable[[], None], timeout: int = 9000,
                 expect: Callable[[dict], bool] | None = None, settle: int = 600) -> list[dict]:
        hits: list[Response] = []
        parsed: list[dict] = []
        done: list[bool] = [False]
        cursor: list[int] = [0]

        def on_resp(r: Response) -> None:
            try:
                if url_part in r.url:
                    hits.append(r)
            except Exception:
                pass

        def drain() -> None:
            while cursor[0] < len(hits):
                r = hits[cursor[0]]
                cursor[0] += 1
                try:
                    parsed.extend(self._parse_jsonish(r.text()))
                except Exception:
                    pass
                if expect and any(expect(j) for j in parsed):
                    done[0] = True

        self.page.on("response", on_resp)
        try:
            action()
            end = time.time() + timeout / 1000
            while time.time() < end:
                self.page.wait_for_timeout(250)
                drain()
                if done[0]:
                    break
            self.page.wait_for_timeout(settle)
            drain()
            return [j for j in parsed if j]
        finally:
            try:
                self.page.remove_listener("response", on_resp)
            except Exception:
                pass

    # ------------------------------------------------------------ 找人（抖音号）
    def _search(self, q: str) -> list[dict]:
        """像真人一样用搜索框搜。

        实测（2026-11）：直接 goto /search/{q}?type=user 页面是空的（SPA 路由不认），
        只有在首页搜索框里输入并回车，跳到 /jingxuan/search/{q} 才会出结果。
        用户卡片来自 general/search/stream 的 type=4 user_list。
        """
        def do_search() -> None:
            self._goto(f"{DOUYIN}/")
            self.page.wait_for_timeout(random.randint(1500, 2600))
            box = self.page.locator('input[placeholder*="搜索"]').first
            box.wait_for(state="visible", timeout=15000)
            box.click()
            self.page.wait_for_timeout(random.randint(200, 500))
            box.fill(q)
            self.page.wait_for_timeout(random.randint(200, 500))
            self.page.keyboard.press("Enter")

        def hit(j: dict) -> bool:
            return any(str(u.get("unique_id") or "") == q or str(u.get("short_id") or "") == q
                       for u in self._extract_users([j]))

        data = self._capture("general/search", do_search, timeout=15000,
                             expect=hit, settle=1500)
        # 首屏没命中时再多等一会儿（stream 是分几段推过来的）
        if not self._extract_users(data):
            self.page.wait_for_timeout(2500)
        return data

    def resolve(self, unique_id: str, nickname: str | None = None) -> dict | None:
        """抖音号/短 ID → {sec_uid, nickname, unique_id, aweme_count, signature}。

        搜不到抖音号时（改名/只填了昵称/号本身失效），用表格里的昵称再试一次，
        但只认**昵称完全一致**的候选，避免抓错人。
        """
        info = self._resolve_once(unique_id)
        if info or not (nickname or "").strip():
            return info
        nick = str(nickname).strip()
        self.log(f"  ↪ 抖音号没搜到，改用昵称再搜一次：{nick}")
        return self._resolve_once(nick, by_nickname=True)

    def _resolve_once(self, unique_id: str, by_nickname: bool = False) -> dict | None:
        self.ensure_login()
        q = str(unique_id or "").strip().lstrip("@")
        if not q:
            return None
        self.log(f"  搜索：{q}")
        data = self._search(q)
        users = self._extract_users(data)
        text = self._body_text()
        if not users and "登录后即可搜索" in text:
            raise NotLoggedIn("搜索需要登录：页面上是「登录后即可搜索」")
        self.check_risk()

        ql = q.lower()
        hit_user = None
        for u in users:
            ids = {str(u.get("unique_id") or "").lower(), str(u.get("short_id") or "").lower()}
            if ql in ids:
                hit_user = u
                break
        if not hit_user:
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
        return {
            "sec_uid": hit_user.get("sec_uid"),
            "nickname": hit_user.get("nickname"),
            "unique_id": hit_user.get("unique_id") or hit_user.get("short_id"),
            "signature": hit_user.get("signature"),
            "aweme_count": hit_user.get("aweme_count"),
        }

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

        主路径：页面自己会调 /aweme/v1/web/aweme/post/，监听响应拿 aweme_list。
        兜底：接口没出来/为空时，从主页 DOM 的 /video/ 链接里捡
        （必须过滤掉 source=Baiduspider 这类 SEO / 推荐位的噪音链接）。
        """
        self.ensure_login()
        url = f"{DOUYIN}/user/{sec_uid}"

        def do_open() -> None:
            self._goto(url)
            self.page.wait_for_timeout(1200)
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
            settle=2000,
        )
        if not any((j.get("aweme_list") or []) for j in data):
            # 再滚一次给它机会（有的号挂载慢）
            data += self._capture(
                "/aweme/v1/web/aweme/post/",
                lambda: self.page.mouse.wheel(0, 1800),
                timeout=8000,
                expect=lambda j: bool(j.get("aweme_list")),
                settle=1200,
            )
        self.check_risk()
        works: list[dict] = []
        for j in data:
            for a in (j.get("aweme_list") or []):
                try:
                    works.append({
                        "aweme_id": a.get("aweme_id"),
                        "desc": (a.get("desc") or "").strip(),
                        "create_time": a.get("create_time") or 0,
                        "is_top": bool(a.get("is_top")),
                        "digg": (a.get("statistics") or {}).get("digg_count"),
                        "comment": (a.get("statistics") or {}).get("comment_count"),
                        "duration": (a.get("video") or {}).get("duration"),
                        "type": a.get("aweme_type"),
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
            return uniq[:limit]

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
            return dom[:limit]
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
            self.page.wait_for_timeout(700)
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
                self.page.wait_for_timeout(random.randint(800, 1400))
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
                    self.page.wait_for_timeout(random.randint(500, 900))
                    el = self._scan_input()
                    if el is not None:
                        return el, f"click:{sel}"
                except Exception:
                    continue
            if time.time() < deadline:
                self.page.wait_for_timeout(random.randint(900, 1400))
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
        self.page.wait_for_timeout(random.randint(600, 1200))
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
        self.page.wait_for_timeout(random.randint(400, 900))
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
            self.page.wait_for_timeout(random.randint(900, 1400))
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
                self.page.wait_for_timeout(random.randint(700, 1100))
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
