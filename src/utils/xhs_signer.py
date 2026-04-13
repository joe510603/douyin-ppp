"""小红书签名模块 — 基于 xhshow 纯 Python 算法

使用 Cloxl/xhshow 纯算法签名库，无需外部签名服务。
参考 MediaCrawler 实现：https://github.com/NanmiCoder/MediaCrawler
算法库：https://github.com/Cloxl/xhshow
"""

from __future__ import annotations

import re
from typing import Optional

from xhshow import Xhshow

from .logger import get_logger

log = get_logger("xhs_signer")


class XhsSigner:
    """小红书签名器 — 基于 xhshow 纯算法"""

    def __init__(self):
        self._client: Optional[Xhshow] = None
        self._cookie: str = ""

    def set_cookie(self, cookie: str) -> None:
        """设置 cookie 字符串（从配置或请求头中提取）"""
        self._cookie = cookie

    def _get_client(self) -> Xhshow:
        if self._client is None:
            self._client = Xhshow()
        return self._client

    def _extract_a1(self) -> str:
        """从 cookie 字符串中提取 a1 值"""
        if not self._cookie:
            return ""
        # cookie 格式: a1=xxx; webId=xxx; ...
        match = re.search(r"a1=([^;]+)", self._cookie)
        return match.group(1).strip() if match else ""

    async def sign(self, url: str, data: Optional[dict] = None) -> dict:
        """
        对请求进行签名。

        Args:
            url: 请求 URL
            data: POST 请求体（可选）

        Returns:
            {"X-S": str, "X-T": str, "x-s-common": str, "X-B3-Traceid": str} 签名头
        """
        try:
            client = self._get_client()
            a1 = self._extract_a1()

            # 提取 URI 路径
            # url 格式: https://edith.xiaohongshu.com/api/sns/web/v1/search/notes?keyword=xxx
            match = re.match(r"https://[^/]+(/[^?]+)", url)
            if not match:
                log.warning(f"无法从 URL 提取路径: {url}")
                return self._fallback_sign()
            uri = match.group(1)

            # 提取查询参数
            params = {}
            if "?" in url:
                query = url.split("?", 1)[1]
                for part in query.split("&"):
                    if "=" in part:
                        k, _, v = part.partition("=")
                        params[k] = v

            if a1:
                cookies = {"a1": a1}
            else:
                cookies = ""

            if data:
                # POST 请求
                headers = client.sign_headers_post(
                    uri=uri,
                    cookies=cookies,
                    xsec_appid="xhs-pc-web",
                    payload=data,
                )
            else:
                # GET 请求
                headers = client.sign_headers_get(
                    uri=uri,
                    cookies=cookies,
                    xsec_appid="xhs-pc-web",
                    params=params if params else None,
                )

            return {
                "X-S": headers.get("x-s", ""),
                "X-T": headers.get("x-t", ""),
                "x-s-common": headers.get("x-s-common", ""),
                "X-B3-Traceid": headers.get("x-b3-traceid", ""),
            }
        except Exception as e:
            log.error(f"xhshow 签名失败: {e}", exc_info=True)
            return self._fallback_sign()

    def _fallback_sign(self) -> dict:
        """回退签名（不应在正常流程中触发）"""
        import time
        import uuid
        timestamp = str(int(time.time() * 1000))
        return {
            "X-S": "",
            "X-T": timestamp,
            "x-s-common": "",
            "X-B3-Traceid": str(uuid.uuid4()).replace("-", ""),
        }


# 全局单例
_xhs_signer: Optional[XhsSigner] = None


def get_xhs_signer() -> XhsSigner:
    global _xhs_signer
    if _xhs_signer is None:
        _xhs_signer = XhsSigner()
    return _xhs_signer
