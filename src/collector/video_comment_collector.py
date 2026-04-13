"""抖音视频评论采集器 — 参考 MediaCrawler 实现

核心 API（与 MediaCrawler 一致）:
  - 搜索视频: /aweme/v1/web/general/search/single/
  - 视频评论: /aweme/v1/web/comment/list/
  - 子评论:   /aweme/v1/web/comment/list/reply/
  - 用户视频: /aweme/v1/web/aweme/post/
  - 视频详情: /aweme/v1/web/aweme/detail/

签名: a_bogus 通过 Node.js 签名服务或本地 JS 引擎生成
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote as _quote

import httpx

from ..config import get_config
from ..models.comment import VideoComment
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from ..utils.signer import generate_a_bogus

log = get_logger("douyin_video_collector")

# ============================================================
# 常量 — 参考 MediaCrawler 的请求参数
# ============================================================

_DOUYIN_BASE_URL = "https://www.douyin.com"

# 通用请求头
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 公共请求参数 — 参考 MediaCrawler DouYinClient
_COMMON_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383c56f0f24b2dcd6ee5fc45e679a79",
    "channel": "channel_pc_web",
    "version_code": "190600",
    "version_name": "19.6.0",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "MacIntel",
    "browser_name": "Chrome",
    "browser_version": "125.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Mac OS",
    "os_version": "10.15.7",
    "cpu_core_num": "8",
    "device_memory": "8",
    "engine_version": "109.0",
    "platform": "PC",
    "screen_width": "2560",
    "screen_height": "1440",
    "effective_type": "4g",
    "round_trip_time": "50",
}

# 请求间隔（秒）— 随机范围，防风控
_REQUEST_INTERVAL = (2.0, 5.0)

# 每页评论数
_COMMENTS_PER_PAGE = 20

# 单个视频最多抓取评论数
_MAX_COMMENTS_PER_VIDEO = 500


def _get_web_id() -> str:
    """生成随机 webid，模拟浏览器标识"""
    return str(random.randint(5300000000000000000, 5999999999999999999))


def _generate_ms_token() -> str:
    """生成 msToken（抖音标准格式）"""
    import string
    timestamp = int(time.time() * 1000)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=107))
    return f"{timestamp}_{random_str}"


class CookieExpiredError(Exception):
    """Cookie 过期或被风控拦截，需要刷新 Cookie"""
    pass


def _is_verify_blocked(data: Optional[dict]) -> bool:
    """检测是否被 verify_check 风控拦截"""
    if not data:
        return True
    nil_type = data.get("search_nil_info", {}).get("search_nil_type", "")
    if nil_type == "verify_check":
        return True
    # 视频详情/评论风控特征：comments 为 None 但 status_code=0
    if data.get("comments") is None and data.get("status_code") == 0:
        return True
    return False


class DouyinVideoCollector:
    """抖音视频评论采集器"""

    def __init__(self, cookie: str = "", proxy: str = ""):
        # 优先使用传入的 cookie，其次使用视频评论专用 cookie，最后使用直播监控 cookie
        self.cookie = cookie or get_config().douyin_video.cookie or get_config().douyin.cookie
        self.proxy = proxy
        self._client: Optional[httpx.AsyncClient] = None
        self._web_id = _get_web_id()
        self._ms_token = ""

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kwargs = {
                "timeout": 30.0,
                "follow_redirects": True,
                "headers": {
                    **_DEFAULT_HEADERS,
                    "Cookie": self.cookie,
                },
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _build_params(self, extra: dict) -> dict:
        """构建完整请求参数（公共参数 + 业务参数）"""
        params = {**_COMMON_PARAMS, **extra}
        params["webid"] = self._web_id
        
        # msToken: 从 Cookie 中提取，如果没有则生成
        if not self._ms_token:
            if self.cookie:
                match = re.search(r"msToken=([^;]+)", self.cookie)
                if match:
                    self._ms_token = match.group(1)
            if not self._ms_token:
                self._ms_token = _generate_ms_token()
                log.debug(f"生成 msToken: {self._ms_token[:30]}...")
        
        params["msToken"] = self._ms_token
        return params

    async def _request(self, url: str, params: dict, extra_headers: dict = None) -> Optional[dict]:
        """发送带 a_bogus 签名的请求"""
        try:
            # 合并公共参数
            full_params = self._build_params(params)

            # 生成 a_bogus 签名
            try:
                # 将参数字典转换为URL参数字符串
                from urllib.parse import urlencode
                params_str = urlencode(full_params)

                # 获取 User-Agent
                user_agent = _DEFAULT_HEADERS.get("User-Agent", "")

                # 生成签名
                a_bogus = generate_a_bogus(params_str, user_agent)
                if a_bogus:
                    full_params["a_bogus"] = a_bogus
                    log.debug(f"a_bogus 签名生成成功: {a_bogus[:50]}...")
                else:
                    log.warning("a_bogus 签名为空，请求可能被风控")
            except Exception as e:
                log.warning(f"a_bogus 签名失败: {e}，请求可能被风控")

            client = await self._get_client()
            # 合并额外的 headers（如特殊的 Referer）
            headers = None
            if extra_headers:
                headers = dict(client.headers)
                headers.update(extra_headers)

            response = await client.get(url, params=full_params, headers=headers)

            if response.status_code == 200:
                data = response.json()
                # 检查业务状态码
                status_code = data.get("status_code", 0)
                if status_code == 0:
                    return data
                else:
                    log.error(f"业务错误: status_code={status_code}, msg={data.get('status_msg', '')}")
                    return data  # 仍然返回，让上层判断
            else:
                log.error(f"请求失败: HTTP {response.status_code}, URL: {url[:80]}")
                return None
        except Exception as e:
            log.error(f"请求异常: {e}")
            return None

    async def _sleep(self):
        """随机间隔，防风控"""
        await asyncio.sleep(random.uniform(*_REQUEST_INTERVAL))

    # ============================================================
    # 模式一：关键词搜索视频 → 抓取评论
    # ============================================================

    async def scrape_by_keyword(
        self,
        task_id: str,
        keywords: list[str],
        max_videos: int = 20,
        task_manager=None,
    ) -> int:
        """按关键词搜索视频并抓取评论"""
        total_count = 0
        found_videos = 0

        for kw in keywords:
            if task_manager and task_manager.is_cancelled(task_id):
                break

            log.info(f"[关键词模式] 搜索: {kw}")
            videos = await self._search_videos(kw, max_count=max_videos)
            found_videos += len(videos)

            for i, video in enumerate(videos):
                if task_manager and task_manager.is_cancelled(task_id):
                    break

                aweme_id = video.get("aweme_id", "") or video.get("id", "")
                if not aweme_id:
                    continue

                title = video.get("desc", "")
                author = ""
                if video.get("author"):
                    author = video["author"].get("nickname", "")

                count = await self._scrape_video_comments(
                    aweme_id=aweme_id,
                    video_title=title,
                    video_author=author,
                    task_id=task_id,
                    source_keyword=kw,
                    task_manager=task_manager,
                )
                total_count += count

                if task_manager:
                    await task_manager.update_progress(
                        task_id, found=found_videos, scraped=i + 1, total=len(videos)
                    )

                await self._sleep()

        return total_count

    async def _intercept_search_item_abogus(self, keyword: str, cursor: int = 0) -> str:
        """
        通过 headless 浏览器拦截 Douyin 视频搜索 API (/aweme/v1/web/search/item/) 请求，
        返回已签名的完整 URL。按 (keyword, cursor) 缓存。
        """
        cache_key = f"item_search:{keyword}:{cursor}"
        if hasattr(self, "_abogus_cache") and cache_key in self._abogus_cache:
            log.debug(f"命中 a_bogus 缓存: {cache_key}")
            return self._abogus_cache[cache_key]

        if not hasattr(self, "_abogus_cache"):
            self._abogus_cache: dict[str, str] = {}

        user_data_dir = Path("data/browser_data")
        log.info(f"拦截视频搜索 '{keyword}' cursor={cursor} 的 a_bogus...")

        def _fetch_sync() -> str:
            from playwright.sync_api import sync_playwright
            signed_url = None

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=True,
                    viewport={"width": 1400, "height": 900},
                    user_agent=_DEFAULT_HEADERS["User-Agent"],
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
                )

                def on_request(request):
                    nonlocal signed_url
                    # 拦截视频搜索专用接口
                    if "/aweme/v1/web/search/item/" in request.url:
                        signed_url = request.url
                        log.info(f"捕获视频搜索 a_bogus URL，长度: {len(request.url)}")

                context.on("request", on_request)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    kw_encoded = _quote(keyword)
                    # type=video 触发视频搜索接口
                    page.goto(f"https://www.douyin.com/search/{kw_encoded}?type=video",
                              wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(8000)
                except Exception as e:
                    log.warning(f"拦截页面打开失败: {e}")
                context.close()

            return signed_url or ""

        signed_url = await asyncio.to_thread(_fetch_sync)

        if not signed_url:
            raise RuntimeError(f"无法为关键词 '{keyword}' cursor={cursor} 拦截到 a_bogus")

        self._abogus_cache[cache_key] = signed_url
        log.info(f"关键词 '{keyword}' cursor={cursor} 的 a_bogus 拦截成功")
        return signed_url

    async def _search_videos(self, keyword: str, max_count: int = 20) -> list[dict]:
        """搜索抖音视频 — 使用 /aweme/v1/web/search/item/ 接口，支持真正的视频分页列表"""
        videos = []
        seen_ids: set[str] = set()
        cursor = 0
        page_size = 20

        while len(videos) < max_count:
            try:
                signed_url = await self._intercept_search_item_abogus(keyword, cursor)
            except Exception as e:
                log.error(f"拦截 a_bogus 失败: {e}")
                break

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    cookies = {}
                    for part in (self.cookie or "").split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, _, v = part.partition("=")
                            cookies[k.strip()] = v.strip()

                    headers = {
                        "User-Agent": _DEFAULT_HEADERS["User-Agent"],
                        "Referer": f"https://www.douyin.com/search/{_quote(keyword)}?type=video",
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    }

                    resp = await client.get(signed_url, headers=headers, cookies=cookies)
                    raw_text = resp.text

                    # 解析 NDJSON
                    data = None
                    for line in raw_text.split("\n"):
                        line = line.strip()
                        if line and line.startswith("{"):
                            data = json.loads(line)
                            break

                    if not data:
                        log.warning(f"cursor={cursor} 解析失败，响应: {raw_text[:200]}")
                        break

                    status_code = data.get("status_code", 0)
                    if status_code != 0:
                        log.warning(f"API 业务错误: {status_code}, msg={data.get('status_msg', '')}")

                    # 视频搜索接口返回 data 数组（每项含 aweme_info）或 aweme_list
                    raw_items = data.get("data", []) or data.get("aweme_list", [])
                    if isinstance(raw_items, list):
                        items = []
                        for item in raw_items:
                            aweme_info = item.get("aweme_info") or item
                            if aweme_info:
                                items.append(aweme_info)
                    else:
                        items = list(raw_items.values()) if isinstance(raw_items, dict) else []

                    has_more = data.get("has_more", 0)
                    log.info(f"视频搜索 '{keyword}' cursor={cursor}: {len(items)} 条, has_more={has_more}")
                    if not items:
                        log.info(f"原始响应前500: {raw_text[:500]}")

                    if not items:
                        nil_type = data.get("search_nil_info", {}).get("search_nil_type", "")
                        if nil_type == "verify_check":
                            raise CookieExpiredError(
                                f"搜索失败：Cookie 已过期，请在「配置管理→抖音视频评论」中刷新 Cookie。"
                            )
                        log.info(f"关键词 '{keyword}' 搜索结果为空，停止")
                        break

                    page_new_count = 0
                    for item in items:
                        aweme_id = item.get("aweme_id", "")
                        if aweme_id and aweme_id not in seen_ids:
                            seen_ids.add(aweme_id)
                            videos.append(item)
                            page_new_count += 1

                    log.info(f"  本页新增 {page_new_count} 条，累计 {len(videos)} 条")

                    if not has_more:
                        break

                    cursor += page_size
                    await self._sleep()

            except CookieExpiredError:
                raise
            except Exception as e:
                log.warning(f"搜索请求异常: {e}")
                break

        return videos[:max_count]

    # ============================================================
    # 模式二：指定账号 → 抓取该账号下视频的评论
    # ============================================================

    async def scrape_by_account(
        self,
        task_id: str,
        sec_user_id: str,
        max_videos: int = 30,
        task_manager=None,
    ) -> int:
        """抓取指定账号下视频的评论"""
        total_count = 0

        log.info(f"[账号模式] 抓取账号: {sec_user_id[:20]}...")
        videos = await self._get_user_videos(sec_user_id, max_count=max_videos)

        for i, video in enumerate(videos):
            if task_manager and task_manager.is_cancelled(task_id):
                break

            aweme_id = video.get("aweme_id", "") or video.get("id", "")
            if not aweme_id:
                continue

            title = video.get("desc", "")
            author = ""
            if video.get("author"):
                author = video["author"].get("nickname", "")

            count = await self._scrape_video_comments(
                aweme_id=aweme_id,
                video_title=title,
                video_author=author,
                task_id=task_id,
                task_manager=task_manager,
            )
            total_count += count

            if task_manager:
                await task_manager.update_progress(
                    task_id, found=len(videos), scraped=i + 1, total=len(videos)
                )

            await self._sleep()

        return total_count

    async def _get_user_videos(self, sec_user_id: str, max_count: int = 30) -> list[dict]:
        """获取用户发布的视频列表"""
        videos = []
        cursor = 0
        count = 18

        while len(videos) < max_count:
            params = {
                "sec_user_id": sec_user_id,
                "max_cursor": cursor,
                "count": count,
                "locate_query": "false",
                "show_live_replay_strategy": "1",
                "need_item_list": "0",
            }

            data = await self._request(
                f"{_DOUYIN_BASE_URL}/aweme/v1/web/aweme/post/",
                params,
            )
            if not data:
                break

            aweme_list = data.get("aweme_list", [])
            if not aweme_list:
                break

            videos.extend(aweme_list)
            cursor = data.get("next_cursor", 0)

            if not data.get("has_more", False):
                break

            await self._sleep()

        return videos[:max_count]

    # ============================================================
    # 模式三：直接指定视频 URL → 抓取评论
    # ============================================================

    async def scrape_by_video_url(
        self,
        task_id: str,
        video_url: str,
        task_manager=None,
    ) -> int:
        """根据视频链接直接抓取评论"""
        aweme_id = await self._resolve_video_id(video_url)
        if not aweme_id:
            log.error(f"无法解析视频 ID: {video_url}")
            return 0

        # 尝试获取视频标题和作者
        video_title, video_author = await self._get_video_info(aweme_id)

        return await self._scrape_video_comments(
            aweme_id=aweme_id,
            video_title=video_title,
            video_author=video_author,
            video_url=video_url,
            task_id=task_id,
            task_manager=task_manager,
        )

    async def _resolve_video_id(self, video_url: str) -> str:
        """从视频链接中解析 aweme_id"""
        # 格式1: https://www.douyin.com/video/7123456789
        match = re.search(r"/video/(\d+)", video_url)
        if match:
            return match.group(1)

        # 格式2: https://www.douyin.com/discover?modal_id=7123456789
        match = re.search(r"modal_id=(\d+)", video_url)
        if match:
            return match.group(1)

        # 格式3: https://www.douyin.com/note/7123456789
        match = re.search(r"/note/(\d+)", video_url)
        if match:
            return match.group(1)

        # 格式4: 短链接 — 请求解析
        try:
            client = await self._get_client()
            response = await client.get(video_url, follow_redirects=True)
            final_url = str(response.url)
            match = re.search(r"/video/(\d+)", final_url)
            if match:
                return match.group(1)
            match = re.search(r'\"aweme_id\"\s*:\s*\"(\d+)\"', response.text)
            if match:
                return match.group(1)
        except Exception as e:
            log.error(f"解析短链接失败: {e}")

        return ""

    async def _get_video_info(self, aweme_id: str) -> tuple[str, str]:
        """获取视频标题和作者"""
        try:
            params = {
                "aweme_id": aweme_id,
            }
            data = await self._request(
                f"{_DOUYIN_BASE_URL}/aweme/v1/web/aweme/detail/",
                params,
            )
            if data:
                aweme_detail = data.get("aweme_detail", {})
                title = aweme_detail.get("desc", "")
                author = ""
                if aweme_detail.get("author"):
                    author = aweme_detail["author"].get("nickname", "")
                return title, author
        except Exception as e:
            log.debug(f"获取视频详情失败: {e}")
        return "", ""

    # ============================================================
    # 核心评论抓取
    # ============================================================

    async def _scrape_video_comments(
        self,
        aweme_id: str,
        video_title: str = "",
        video_author: str = "",
        video_url: str = "",
        task_id: str = "",
        source_keyword: str = "",
        task_manager=None,
    ) -> int:
        """抓取单个视频的评论"""
        total_count = 0
        cursor = 0
        seen_keys: set[str] = set()  # 跨页去重

        while total_count < _MAX_COMMENTS_PER_VIDEO:
            if task_manager and task_manager.is_cancelled(task_id):
                break

            params = {
                "aweme_id": aweme_id,
                "cursor": cursor,
                "count": _COMMENTS_PER_PAGE,
                "item_type": "0",
            }

            # 构建评论 API 的特殊 Referer（参考 MediaCrawler）
            import urllib.parse
            keyword = source_keyword or "video"
            referer_url = f"https://www.douyin.com/search/{keyword}?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general"

            data = await self._request(
                f"{_DOUYIN_BASE_URL}/aweme/v1/web/comment/list/",
                params,
                extra_headers={"Referer": urllib.parse.quote(referer_url, safe=':/')},
            )
            if not data:
                log.warning(f"视频 {aweme_id} 评论 API 返回空数据")
                break

            # 检测风控
            if _is_verify_blocked(data):
                log.error(f"视频 {aweme_id} 评论抓取失败：Cookie 已过期或被风控，请刷新 Cookie")
                raise CookieExpiredError(
                    f"评论抓取失败：Cookie 已过期或被风控，请在「配置管理→抖音视频评论」中点击「自动获取 Cookie」。"
                )

            # 详细调试日志
            log.debug(f"视频 {aweme_id} API 返回: status_code={data.get('status_code')}, has_more={data.get('has_more')}, cursor={data.get('cursor')}")
            log.debug(f"返回数据键: {list(data.keys())}")

            comments = data.get("comments", [])
            if not comments:
                log.warning(f"视频 {aweme_id} comments 数组为空，完整响应: {data}")
                break

            # 构建评论对象并批量入库（去重：同视频+同用户+同内容）
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            video_comments = []
            for c in comments:
                cid = str(c.get("user", {}).get("uid", ""))
                ctext = c.get("text", "")
                key = f"{aweme_id}:{cid}:{ctext}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                vc = VideoComment(
                    source="douyin",
                    task_id=task_id,
                    video_id=aweme_id,
                    video_url=video_url or f"https://www.douyin.com/video/{aweme_id}",
                    video_title=video_title[:200] if video_title else "",
                    video_author=video_author,
                    content=ctext,
                    user_id=cid,
                    user_nickname=c.get("user", {}).get("nickname", ""),
                    publish_time=str(c.get("create_time", "")),
                    like_count=c.get("digg_count", 0),
                    reply_count=c.get("reply_comment_total", 0),
                    source_keyword=source_keyword,
                    collected_at=now,
                )
                video_comments.append(vc)

            # 批量入库
            if video_comments:
                db = get_db()
                if db and db._db:
                    await db.batch_insert_video_comments(video_comments)
                total_count += len(video_comments)

            # 检查 has_more — 注意抖音返回的可能是 int 0/1 或 bool
            has_more = data.get("has_more", 0)
            if not has_more:
                break

            cursor = data.get("cursor", 0)
            await self._sleep()

        log.info(f"视频 {aweme_id} 抓取了 {total_count} 条评论")
        return total_count
