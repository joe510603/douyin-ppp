"""抖音网页操作器 — 在真实抖音界面搜索用户并给作品留言

设计要点：
- 用 Playwright 异步 API（应用本身是 asyncio 架构），复用 detector 的浏览器起法
- 持久化 profile（data/browser_data）+ 注入 config 里的最新 cookie，
  避免使用过期 ttwid 被重定向
- 选择器全部带多路兜底：抖音 DOM 经常变，单一 selector 很容易失效
- 全程限速，失败即记录并向上抛，由 runner 统一决定是否继续

风险提示：自动化操作有触发平台风控的可能，务必保持限速与每日上限。
"""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from ..config import get_config
from ..utils.logger import get_logger

log = get_logger("douyin_operator")

# PC Web UA（与 detector 保持一致）
WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 搜索结果里的用户链接
_USER_LINK_SELECTORS = [
    'a[href*="/user/"]',
    '[data-e2e="search-user-item"] a',
    'li a[href^="/user/"]',
]

# 评论输入框
_COMMENT_INPUT_SELECTORS = [
    '[data-e2e="comment-input"]',
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="评论"]',
    'textarea[placeholder*="留下"]',
]

# 发送按钮
_SEND_BUTTON_SELECTORS = [
    '[data-e2e="comment-publish"]',
    'button:has-text("发布")',
    'button:has-text("发送")',
    'span:has-text("发送")',
]


class DouyinOperator:
    """抖音网页操作器（一次任务内复用同一个浏览器会话）"""

    def __init__(self):
        self._playwright = None
        self._context = None
        self._page = None
        # 独立的浏览器 profile 目录。
        # 不能复用 detector 的 data/browser_data —— Chromium 对同一 user_data_dir
        # 是独占的，而 detector 常年轮询占用着它，复用会导致启动直接失败。
        # 这里用独立目录，登录态靠注入 config 里的 cookie 保证。
        self._browser_data_dir = str(Path("data/outreach_browser_data"))
        self._config = get_config()

    # ============================================================
    # 生命周期
    # ============================================================

    def _get_effective_cookie(self) -> str:
        """
        取用于网页操作的 Cookie。

        搜索用户和发评论都必须**登录态**（需要 sessionid），
        匿名 cookie（只有 ttwid）打开搜索页只会看到「登录后即可搜索」。
        因此优先选带 sessionid 的那份。
        """
        config = self._config
        candidates = [
            (config.douyin_video.cookie or "").strip(),
            (config.douyin.cookie or "").strip(),
        ]
        for ck in candidates:
            if "sessionid" in ck:
                return ck
        return candidates[0] if candidates[0] else candidates[1]

    def _parse_cookies(self) -> list[dict]:
        """把 config 里的 cookie 串转成 Playwright 格式"""
        cookie_str = self._get_effective_cookie()
        if not cookie_str:
            return []
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })
        return cookies

    async def start(self):
        """启动浏览器"""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        Path(self._browser_data_dir).mkdir(parents=True, exist_ok=True)

        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._browser_data_dir,
                headless=True,
                viewport={"width": 1400, "height": 900},
                user_agent=WEB_UA,
                args=args,
            )
            log.info("[触达] 持久化上下文启动成功")
        except Exception as e:
            log.warning(f"[触达] 持久化上下文启动失败({e})，回退独立 browser")
            browser = await self._playwright.chromium.launch(headless=True, args=args)
            self._context = await browser.new_context(
                viewport={"width": 1400, "height": 900}, user_agent=WEB_UA
            )

        # 必须先访问域名才能注入 cookie
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        try:
            await self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        cookies = self._parse_cookies()
        if cookies:
            try:
                await self._context.add_cookies(cookies)
                log.info(f"[触达] 已注入 {len(cookies)} 条 cookie")
            except Exception as e:
                log.warning(f"[触达] 注入 cookie 失败: {e}")

        return self

    async def close(self):
        """关闭浏览器"""
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._playwright = None
        self._page = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.close()

    # ============================================================
    # 搜索用户
    # ============================================================

    async def search_user(self, display_id: str) -> tuple[Optional[str], str]:
        """
        在抖音搜索页搜索抖音号，定位用户主页。

        Args:
            display_id: 目标抖音号

        Returns:
            (主页 URL, 备注信息)；找不到时 URL 为 None
        """
        if not self._page:
            raise RuntimeError("浏览器未启动")

        keyword = quote(display_id.strip())
        url = f"https://www.douyin.com/search/{keyword}?type=user"

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning(f"[触达] 打开搜索页失败: {e}")
            return None, f"打开搜索页失败: {e}"

        # 等用户链接出现
        try:
            await self._page.wait_for_selector(
                'a[href*="/user/"]', timeout=15000
            )
        except Exception:
            log.info(f"[触达] 搜索「{display_id}」未出现用户结果")
            return None, await self._describe_block()

        await self._page.wait_for_timeout(1500)

        # 登录墙检测：抖音未登录时搜索结果区会提示「登录后即可搜索」
        blocked = await self._detect_login_wall()
        if blocked:
            return None, blocked

        # 收集候选用户链接（排除导航栏「我的」入口）
        candidates = await self._collect_user_links()
        if not candidates:
            # 结果区可能有「用户」标签页，尝试切过去
            try:
                user_tab = await self._page.query_selector(
                    '[data-e2e="search-user-tab"], li[role="tab"]:has-text("用户")'
                )
                if user_tab:
                    await user_tab.click(timeout=5000)
                    await self._page.wait_for_timeout(2500)
                    candidates = await self._collect_user_links()
            except Exception:
                pass

        if not candidates:
            return None, "搜索结果里没有解析到用户链接（可能未登录或被风控）"

        # 优先挑文本里包含抖音号的（精确匹配优先）
        target = display_id.strip().lower()
        exact = [c for c in candidates if target in c[1].lower().replace(" ", "")]
        if exact:
            chosen = exact[0]
            return self._abs_url(chosen[0]), "精确匹配到抖音号"

        # 否则取第一个作为候选（可能是近似结果）
        chosen = candidates[0]
        return self._abs_url(chosen[0]), f"未精确匹配抖音号，取首个结果（候选 {len(candidates)} 个）"

    async def _detect_login_wall(self) -> str:
        """
        检测是否撞上登录墙 / 风控页。

        Returns:
            命中时返回中文原因说明，未命中返回空字符串
        """
        try:
            body = (await self._page.inner_text("body")) or ""
        except Exception:
            return ""
        if "登录后即可搜索" in body or "扫码登录" in body:
            return "抖音未登录（Cookie 缺 sessionid），请在「配置管理」用「自动获取 Cookie」扫码登录后再试"
        if "验证码" in body and "安全验证" in body:
            return "触发抖音安全验证，请先在浏览器里完成验证"
        return ""

    async def _describe_block(self) -> str:
        """没有结果时，给出比「无结果」更有用的诊断"""
        reason = await self._detect_login_wall()
        if reason:
            return reason
        return "搜索页无用户结果（可能该抖音号不存在，或被平台风控）"

    async def _collect_user_links(self) -> list[tuple[str, str]]:
        """收集页面里的用户主页链接，排除「我的」等导航链接"""
        candidates: list[tuple[str, str]] = []
        seen = set()
        for selector in _USER_LINK_SELECTORS:
            try:
                els = await self._page.query_selector_all(selector)
            except Exception:
                continue
            for el in els:
                try:
                    href = await el.get_attribute("href")
                    if not href or "/user/" not in href:
                        continue
                    # 排除「我的」入口与无意义路径
                    if "/user/self" in href or href.rstrip("/").endswith("/user"):
                        continue
                    if href in seen:
                        continue
                    seen.add(href)
                    text = (await el.inner_text()) or ""
                    candidates.append((href, text.strip()))
                except Exception:
                    continue
            if candidates:
                break
        return candidates

    def _abs_url(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return f"https://www.douyin.com{href}"

    # ============================================================
    # 作品留言
    # ============================================================

    async def get_latest_video_url(self, profile_url: str) -> Optional[str]:
        """取用户主页的第一个作品链接"""
        try:
            await self._page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning(f"[触达] 打开主页失败: {e}")
            return None

        try:
            await self._page.wait_for_selector('a[href*="/video/"]', timeout=15000)
        except Exception:
            return None

        await self._page.wait_for_timeout(1000)
        try:
            els = await self._page.query_selector_all('a[href*="/video/"]')
            for el in els:
                href = await el.get_attribute("href")
                if href and "/video/" in href:
                    return self._abs_url(href)
        except Exception:
            pass
        return None

    async def comment_on_video(self, video_url: str, message: str) -> tuple[bool, str]:
        """
        在视频页面发送一条评论。

        Args:
            video_url: 视频详情页 URL
            message: 评论内容

        Returns:
            (是否成功, 说明)
        """
        if not self._page:
            raise RuntimeError("浏览器未启动")
        if not message:
            return False, "留言内容为空"

        try:
            await self._page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return False, f"打开视频页失败: {e}"

        # 等评论区出现
        try:
            await self._page.wait_for_selector(
                'div[contenteditable="true"], textarea, [data-e2e="comment-input"]',
                timeout=15000,
            )
        except Exception:
            return False, "评论区未加载（可能被风控或需要登录）"

        await self._page.wait_for_timeout(1200)

        # 定位输入框并输入
        input_el = None
        for selector in _COMMENT_INPUT_SELECTORS:
            try:
                el = await self._page.query_selector(selector)
            except Exception:
                el = None
            if el:
                input_el = el
                break

        if not input_el:
            return False, "找不到评论输入框"

        try:
            await input_el.click(timeout=5000)
        except Exception:
            pass

        try:
            await input_el.fill("")
        except Exception:
            pass

        try:
            await input_el.type(message, delay=60)
        except Exception as e:
            return False, f"输入评论失败: {e}"

        await self._page.wait_for_timeout(600)

        # 点击发送
        sent = False
        for selector in _SEND_BUTTON_SELECTORS:
            try:
                btn = await self._page.query_selector(selector)
            except Exception:
                btn = None
            if not btn:
                continue
            try:
                if await btn.is_visible():
                    await btn.click(timeout=5000)
                    sent = True
                    break
            except Exception:
                continue

        if not sent:
            # 兜底：回车发送
            try:
                await input_el.press("Enter")
                sent = True
            except Exception:
                pass

        if not sent:
            return False, "找不到发送按钮"

        await self._page.wait_for_timeout(1500)

        # 校验评论是否真的发出去了
        try:
            body_text = await self._page.inner_text("body")
            if message[:10] in body_text:
                return True, "评论已发送并出现在评论区"
            return True, "已点击发送（未在页面确认到评论）"
        except Exception:
            return True, "已点击发送"

    # ============================================================
    # 组合操作
    # ============================================================

    async def search_and_comment(
        self, display_id: str, message: str, comments_per_user: int = 1
    ) -> tuple[bool, str, str]:
        """
        完整流程：搜人 → 定位主页 → 给作品留言。

        Returns:
            (是否成功, 说明, 目标主页 URL)
        """
        profile_url, note = await self.search_user(display_id)
        if not profile_url:
            return False, note, ""

        log.info(f"[触达] {display_id} → {profile_url}（{note}）")

        done = 0
        last_error = ""
        for i in range(max(1, comments_per_user)):
            video_url = await self.get_latest_video_url(profile_url)
            if not video_url:
                last_error = "主页里没找到作品"
                break
            ok, msg = await self.comment_on_video(video_url, message)
            if ok:
                done += 1
                last_error = msg
            else:
                last_error = msg
                break
            if i + 1 < comments_per_user:
                await asyncio.sleep(random.uniform(2.0, 4.0))

        if done > 0:
            return True, f"已留言 {done} 条：{last_error}", profile_url
        return False, last_error or "留言失败", profile_url
