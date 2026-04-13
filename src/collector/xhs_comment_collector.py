"""小红书评论采集器 — 参考 MediaCrawler 实现

核心 API（与 MediaCrawler 一致）:
  - 搜索笔记: /api/sns/web/v1/search/notes
  - 笔记评论: /api/sns/web/v2/comment/page       ← 升级到 v2
  - 子评论:   /api/sns/web/v2/comment/sub/page    ← 新增子评论
  - 用户笔记: /api/sns/web/v1/user_posted
  - 笔记详情: /api/sns/web/v1/feed

签名: X-S / X-T / X-S-Common / X-B3-Traceid
  - 优先: Node.js 签名服务
  - 回退: 本地 xhshow 算法（需安装依赖）
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from ..config import get_config
from ..models.comment import VideoComment
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from ..utils.xhs_signer import get_xhs_signer

log = get_logger("xhs_comment_collector")

# ============================================================
# 常量
# ============================================================

_XHS_BASE_URL = "https://edith.xiaohongshu.com"

# 通用请求头
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.xiaohongshu.com/",
    "Origin": "https://www.xiaohongshu.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
}

# 请求间隔
_REQUEST_INTERVAL = (3.0, 6.0)

# 每页评论数
_COMMENTS_PER_PAGE = 20

# 单个笔记最多抓取评论数
_MAX_COMMENTS_PER_NOTE = 300


class XhsCommentCollector:
    """小红书评论采集器"""

    def __init__(self, cookie: str = "", proxy: str = ""):
        self.cookie = cookie
        self.proxy = proxy
        self._client: Optional[httpx.AsyncClient] = None
        self._signer = get_xhs_signer()
        self._xhs_browser_data = Path("data/xhs_browser_data")
        self._xhs_browser_data.mkdir(parents=True, exist_ok=True)
        self._intercept_cache: dict[str, dict] = {}  # 拦截结果缓存

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {**_DEFAULT_HEADERS}
            if self.cookie:
                headers["Cookie"] = self.cookie
            kwargs = {
                "timeout": 30.0,
                "follow_redirects": True,
                "headers": headers,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ============================================================
    # Playwright 拦截层 — 绕过 xhshow 算法签名被设备绑定的问题
    # 策略：打开浏览器，在页面 JS 上下文中执行 XMLHttpRequest，浏览器自动处理签名
    # XMLHttpRequest 会触发 context.response 拦截器，我们抓取响应体
    # ============================================================

    async def _intercept_xhs_api(self, api_path: str, params: dict, method: str = "GET",
                                  data: Optional[dict] = None, page_url: str = "") -> Optional[dict]:
        """
        通过 Playwright headful 浏览器拦截 XHS API 请求。
        在页面 JS 上下文中执行 XHR，浏览器自动添加正确签名，
        同时注册 response 拦截器捕获响应。
        浏览器复用 data/xhs_browser_data 持久化上下文（自动复用登录态）。
        按 (api_path, json.dumps(params)) 缓存。
        """
        cache_key = f"{method}:{api_path}:{json.dumps(params, sort_keys=True)}"
        if cache_key in self._intercept_cache:
            log.debug(f"命中 XHS 拦截缓存: {cache_key[:80]}")
            return self._intercept_cache[cache_key]

        log.info(f"Playwright 拦截 XHS API: {method} {api_path}")

        def _fetch_sync() -> Optional[dict]:
            from playwright.sync_api import sync_playwright

            intercepted_result: dict = {}

            def _run():
                nonlocal intercepted_result
                base_url = "https://edith.xiaohongshu.com"
                target_url = f"{base_url}{api_path}"

                with sync_playwright() as p:
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(self._xhs_browser_data),
                        headless=True,
                        viewport={"width": 1400, "height": 900},
                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                        args=["--no-sandbox", "--disable-setuid-sandbox",
                              "--disable-dev-shm-usage",
                              "--disable-blink-features=AutomationControlled"],
                    )

                    # 注册响应拦截器 — 抓取目标 API 的响应
                    def on_response(response):
                        url = response.url
                        if api_path in url and "edith.xiaohongshu.com" in url:
                            try:
                                body = response.text()
                                if body:
                                    rd = json.loads(body)
                                    intercepted_result["result"] = rd
                                    log.info(f"  拦截到 XHS 响应: success={rd.get('success')}, "
                                             f"items={len(rd.get('data', {}).get('items', []))}")
                            except Exception as e:
                                log.warning(f"  解析 XHS 响应失败: {e}")

                    context.on("response", on_response)

                    page = context.pages[0] if context.pages else context.new_page()

                    # 先打开任意 XHS 页面（建立签名上下文）
                    try:
                        page.goto("https://www.xiaohongshu.com/",
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)
                    except Exception as e:
                        log.warning(f"  XHS 首页打开失败: {e}")

                    # 构建 XHR JS 代码
                    if method == "POST":
                        body_json = json.dumps(data) if data else "{}"
                        xhr_code = f"""
                        (function() {{
                            window.__xhs_intercepted__ = false;
                            var xhr = new XMLHttpRequest();
                            xhr.open('POST', '{target_url}', true);
                            xhr.setRequestHeader('Content-Type', 'application/json;charset=UTF-8');
                            xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
                            xhr.setRequestHeader('Referer', 'https://www.xiaohongshu.com/');
                            xhr.setRequestHeader('Origin', 'https://www.xiaohongshu.com');
                            xhr.onload = function() {{ window.__xhs_intercepted__ = true; }};
                            xhr.onerror = function(e) {{ window.__xhs_intercepted__ = true; }};
                            xhr.send('{body_json.replace("'", "\\'")}');
                            return 'sent';
                        }})()
                        """
                    else:
                        qs = "&".join(f"{k}={v}" for k, v in params.items())
                        full_url = f"{target_url}?{qs}" if qs else target_url
                        xhr_code = f"""
                        (function() {{
                            window.__xhs_intercepted__ = false;
                            var xhr = new XMLHttpRequest();
                            xhr.open('GET', '{full_url}', true);
                            xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
                            xhr.setRequestHeader('Referer', 'https://www.xiaohongshu.com/');
                            xhr.setRequestHeader('Origin', 'https://www.xiaohongshu.com');
                            xhr.onload = function() {{ window.__xhs_intercepted__ = true; }};
                            xhr.onerror = function(e) {{ window.__xhs_intercepted__ = true; }};
                            xhr.send();
                            return 'sent';
                        }})()
                        """

                    log.info(f"  执行 XHR 调用并等待结果...")
                    try:
                        page.evaluate(xhr_code)
                        # 等待 XHR 请求完成
                        page.wait_for_function(
                            "window.__xhs_intercepted__ === true",
                            timeout=15000
                        )
                        log.info(f"  XHR 请求完成")
                    except Exception as e:
                        log.warning(f"  等待 XHR 完成超时: {e}")
                        page.wait_for_timeout(5000)

                    context.close()

            _run()
            return intercepted_result.get("result")

        try:
            result = await asyncio.to_thread(_fetch_sync)
            if result:
                self._intercept_cache[cache_key] = result
            return result
        except Exception as e:
            log.error(f"Playwright 拦截 XHS API 异常: {e}")
            return None

    async def _request(self, url: str, params: dict, method: str = "GET", data: Optional[dict] = None) -> Optional[dict]:
        """发送带签名的请求"""
        try:
            # 设置 cookie（供 xhshow 提取 a1）
            self._signer.set_cookie(self.cookie)
            # 获取签名头（X-S, X-T, X-S-Common, X-B3-Traceid）
            sign_headers = await self._signer.sign(url, data)

            client = await self._get_client()
            headers = {**sign_headers}

            if method == "GET":
                response = await client.get(url, params=params, headers=headers)
            else:
                response = await client.post(url, json=data, params=params, headers=headers)

            if response.status_code == 200:
                result = response.json()
                # 检查小红书业务状态码
                if result.get("success") is False:
                    log.error(f"小红书业务错误: {result.get('msg', 'unknown')}")
                return result
            else:
                log.error(f"请求失败: HTTP {response.status_code}, URL: {url[:80]}")
                return None
        except Exception as e:
            log.error(f"请求异常: {e}")
            return None

    async def _sleep(self):
        await asyncio.sleep(random.uniform(*_REQUEST_INTERVAL))

    # ============================================================
    # 模式一：关键词搜索笔记 → 抓取评论
    # ============================================================

    async def scrape_by_keyword(
        self,
        task_id: str,
        keywords: list[str],
        max_notes: int = 20,
        task_manager=None,
    ) -> int:
        """按关键词搜索笔记并抓取评论"""
        total_count = 0
        found_notes = 0

        for kw in keywords:
            if task_manager and task_manager.is_cancelled(task_id):
                break

            log.info(f"[关键词模式] 搜索: {kw}")
            notes = await self._search_notes(kw, max_count=max_notes)
            found_notes += len(notes)

            for i, note in enumerate(notes):
                if task_manager and task_manager.is_cancelled(task_id):
                    break

                note_id = note.get("note_id", "") or note.get("id", "")
                if not note_id:
                    continue

                title = note.get("display_title", "") or note.get("title", "")
                author = ""
                if note.get("user"):
                    author = note["user"].get("nickname", "")

                # 获取 xsec_token（部分笔记需要）
                xsec_token = note.get("xsec_token", "")

                count = await self._scrape_note_comments(
                    note_id=note_id,
                    note_title=title,
                    note_author=author,
                    xsec_token=xsec_token,
                    task_id=task_id,
                    source_keyword=kw,
                    task_manager=task_manager,
                )
                total_count += count

                if task_manager:
                    await task_manager.update_progress(
                        task_id, found=found_notes, scraped=i + 1, total=len(notes)
                    )

                await self._sleep()

        return total_count

    async def _search_notes(self, keyword: str, max_count: int = 20) -> list[dict]:
        """搜索小红书笔记 — 优先 Playwright 拦截，否则回退到 xhshow"""
        notes = []
        page = 1

        while len(notes) < max_count:
            api_path = "/api/sns/web/v1/search/notes"
            post_data = {
                "keyword": keyword,
                "page": page,
                "page_size": 20,
                "search_id": "",
                "sort": "general",
                "note_type": 0,
            }

            # 优先 Playwright 拦截（绕过 xhshow 设备绑定）
            page_url = (f"https://www.xiaohongshu.com/search_result?"
                        f"keyword={keyword}&type=51&source=web_explore_feed")
            result = await self._intercept_xhs_api(
                api_path=api_path,
                params={},
                method="POST",
                data=post_data,
                page_url=page_url,
            )

            # Playwright 拦截失败，回退 xhshow 签名
            if not result or result.get("success") is False:
                url = f"{_XHS_BASE_URL}{api_path}"
                result = await self._request(url, params={}, method="POST", data=post_data)
                if not result:
                    break

            items = result.get("data", {}).get("items", [])
            if not items:
                break

            for item in items:
                note_card = item.get("note_card") or item
                if note_card and (note_card.get("note_id") or note_card.get("id")):
                    notes.append(note_card)

            page += 1
            if page > 10:
                break

            has_more = result.get("data", {}).get("has_more", False)
            if not has_more:
                break

            await self._sleep()

        log.info(f"关键词 '{keyword}' 搜索到 {len(notes)} 条笔记")
        return notes[:max_count]

    # ============================================================
    # 模式二：指定账号 → 抓取该账号下笔记的评论
    # ============================================================

    async def scrape_by_account(
        self,
        task_id: str,
        user_id: str,
        max_notes: int = 30,
        task_manager=None,
    ) -> int:
        """抓取指定账号下笔记的评论"""
        total_count = 0

        log.info(f"[账号模式] 抓取账号: {user_id[:20]}...")
        notes = await self._get_user_notes(user_id, max_count=max_notes)

        for i, note in enumerate(notes):
            if task_manager and task_manager.is_cancelled(task_id):
                break

            note_id = note.get("note_id", "") or note.get("id", "")
            if not note_id:
                continue

            title = note.get("display_title", "") or note.get("title", "")
            author = ""
            if note.get("user"):
                author = note["user"].get("nickname", "")

            xsec_token = note.get("xsec_token", "")

            count = await self._scrape_note_comments(
                note_id=note_id,
                note_title=title,
                note_author=author,
                xsec_token=xsec_token,
                task_id=task_id,
                task_manager=task_manager,
            )
            total_count += count

            if task_manager:
                await task_manager.update_progress(
                    task_id, found=len(notes), scraped=i + 1, total=len(notes)
                )

            await self._sleep()

        return total_count

    async def _get_user_notes(self, user_id: str, max_count: int = 30) -> list[dict]:
        """获取用户发布的笔记列表"""
        notes = []
        cursor = ""

        while len(notes) < max_count:
            url = f"{_XHS_BASE_URL}/api/sns/web/v1/user_posted"
            params = {
                "user_id": user_id,
                "cursor": cursor,
                "num": 30,
            }

            result = await self._request(url, params=params)
            if not result:
                break

            notes_data = result.get("data", {}).get("notes", [])
            if not notes_data:
                break

            notes.extend(notes_data)
            cursor = result.get("data", {}).get("cursor", "")

            if not result.get("data", {}).get("has_more", False):
                break

            await self._sleep()

        return notes[:max_count]

    # ============================================================
    # 模式三：直接指定笔记 URL → 抓取评论
    # ============================================================

    async def scrape_by_note_url(
        self,
        task_id: str,
        note_url: str,
        task_manager=None,
    ) -> int:
        """根据笔记链接直接抓取评论"""
        note_id = self._resolve_note_id(note_url)
        if not note_id:
            log.error(f"无法解析笔记 ID: {note_url}")
            return 0

        # 尝试获取笔记详情
        note_title, note_author, xsec_token = await self._get_note_info(note_id)

        return await self._scrape_note_comments(
            note_id=note_id,
            note_title=note_title,
            note_author=note_author,
            note_url=note_url,
            xsec_token=xsec_token,
            task_id=task_id,
            task_manager=task_manager,
        )

    def _resolve_note_id(self, note_url: str) -> str:
        """从笔记链接中解析 note_id"""
        # 格式: https://www.xiaohongshu.com/explore/xxxx
        match = re.search(r"/explore/([a-f0-9]+)", note_url)
        if match:
            return match.group(1)

        # 格式: https://www.xiaohongshu.com/discovery/item/xxxx
        match = re.search(r"/discovery/item/([a-f0-9]+)", note_url)
        if match:
            return match.group(1)

        # 格式: https://www.xiaohongshu.com/search_result/xxxx
        match = re.search(r"/search_result/([a-f0-9]+)", note_url)
        if match:
            return match.group(1)

        # 格式: 短链接中的 note_id
        match = re.search(r"/note/([a-f0-9]+)", note_url)
        if match:
            return match.group(1)

        return ""

    async def _get_note_info(self, note_id: str) -> tuple[str, str, str]:
        """获取笔记标题、作者和 xsec_token"""
        try:
            url = f"{_XHS_BASE_URL}/api/sns/web/v1/feed"
            data = {
                "source_note_id": note_id,
                "image_formats": ["jpg", "webp", "avif"],
                "extra": {"need_body_topic": 1},
            }
            result = await self._request(url, params={}, method="POST", data=data)
            if result:
                items = result.get("data", {}).get("items", [])
                if items:
                    note_card = items[0].get("note_card", {})
                    title = note_card.get("display_title", "") or note_card.get("title", "")
                    author = ""
                    if note_card.get("user"):
                        author = note_card["user"].get("nickname", "")
                    xsec_token = items[0].get("xsec_token", "")
                    return title, author, xsec_token
        except Exception as e:
            log.debug(f"获取笔记详情失败: {e}")
        return "", "", ""

    # ============================================================
    # 核心评论抓取
    # ============================================================

    async def _scrape_note_comments(
        self,
        note_id: str,
        note_title: str = "",
        note_author: str = "",
        note_url: str = "",
        xsec_token: str = "",
        task_id: str = "",
        source_keyword: str = "",
        task_manager=None,
    ) -> int:
        """抓取单个笔记的评论（v2 API + 子评论）"""
        total_count = 0
        cursor = ""

        while total_count < _MAX_COMMENTS_PER_NOTE:
            if task_manager and task_manager.is_cancelled(task_id):
                break

            # 使用 v2 评论 API（与 MediaCrawler 一致）
            api_path = "/api/sns/web/v2/comment/page"
            params = {
                "note_id": note_id,
                "cursor": cursor,
                "top_comment_id": "",
                "image_formats": "jpg,webp,avif",
            }
            if xsec_token:
                params["xsec_token"] = xsec_token

            page_url = f"https://www.xiaohongshu.com/explore/{note_id}"

            # 优先 Playwright 拦截（绕过 xhshow 设备绑定）
            result = await self._intercept_xhs_api(
                api_path=api_path,
                params=params,
                method="GET",
                page_url=page_url,
            )

            # Playwright 拦截失败，回退 xhshow 签名
            if not result or result.get("success") is False:
                url = f"{_XHS_BASE_URL}{api_path}"
                result = await self._request(url, params=params)
                if not result:
                    # 回退到 v1 API
                    url_v1 = f"{_XHS_BASE_URL}/api/sns/web/v1/comment/page"
                    result = await self._request(url_v1, params=params)
                    if not result:
                        break

            comments = result.get("data", {}).get("comments", [])
            if not comments:
                break

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            video_comments = []
            for c in comments:
                user_info = c.get("user_info", {})
                vc = VideoComment(
                    source="xiaohongshu",
                    task_id=task_id,
                    video_id=note_id,
                    video_url=note_url or f"https://www.xiaohongshu.com/explore/{note_id}",
                    video_title=note_title[:200] if note_title else "",
                    video_author=note_author,
                    content=c.get("content", ""),
                    user_id=str(user_info.get("user_id", "")),
                    user_nickname=user_info.get("nickname", ""),
                    publish_time=str(c.get("create_time", "")),
                    like_count=c.get("like_count", 0),
                    reply_count=c.get("sub_comment_count", 0),
                    source_keyword=source_keyword,
                    collected_at=now,
                )
                video_comments.append(vc)

            if video_comments:
                db = get_db()
                if db and db._db:
                    await db.batch_insert_video_comments(video_comments)
                total_count += len(video_comments)

            # 检查 has_more
            has_more = result.get("data", {}).get("has_more", False)
            if not has_more:
                break

            cursor = result.get("data", {}).get("cursor", "")
            await self._sleep()

        log.info(f"笔记 {note_id} 抓取了 {total_count} 条评论")
        return total_count
