"""签名服务客户端 - 调用 Node.js 签名服务"""

from __future__ import annotations

import httpx
from typing import Optional
from ..utils.logger import get_logger

log = get_logger("signer_client")


class SignerClient:
    """Node.js 签名服务客户端"""
    
    def __init__(self, server_url: str = "http://localhost:3010"):
        """
        Args:
            server_url: 签名服务地址
        """
        self.server_url = server_url
        self._client = httpx.AsyncClient(timeout=10.0)
    
    async def get_signature(self, x_ms_stub: str) -> Optional[str]:
        """
        获取签名
        
        Args:
            x_ms_stub: X-Ms-Stub 参数 (MD5 哈希值)
            
        Returns:
            签名字符串,失败返回 None
        """
        try:
            response = await self._client.post(
                f"{self.server_url}/signature",
                content=x_ms_stub,
                headers={"Content-Type": "text/plain; charset=utf-8"}
            )
            
            if response.status_code == 200:
                return response.text
            else:
                log.error(f"签名服务返回错误: {response.status_code}")
                return None
                
        except Exception as e:
            log.error(f"调用签名服务失败: {e}")
            return None
    
    async def close(self):
        """关闭客户端"""
        await self._client.aclose()


# 全局单例
_signer_client: Optional[SignerClient] = None


def get_signer_client() -> SignerClient:
    """获取签名客户端单例"""
    global _signer_client
    if _signer_client is None:
        _signer_client = SignerClient()
    return _signer_client