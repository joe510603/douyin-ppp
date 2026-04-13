"""签名服务管理器 - 自动启动、健康检查、自动重启"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from .logger import get_logger

log = get_logger("signer_manager")


class SignerManager:
    """签名服务生命周期管理"""
    
    def __init__(self, port: int = 3010, websdk_path: str = "websdk"):
        self.port = port
        self.websdk_path = Path(websdk_path)
        self._process: Optional[subprocess.Popen] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._is_running = False
    
    async def check_health(self) -> bool:
        """检查签名服务是否健康"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(
                    f"http://localhost:{self.port}/signature",
                    content="health_check"
                )
                return response.status_code == 200
        except Exception:
            return False
    
    async def start_if_needed(self) -> bool:
        """如果签名服务未运行,则启动它"""
        # 1. 检查是否已经在运行
        if await self.check_health():
            log.info(f"✅ 签名服务已在运行 (端口 {self.port})")
            self._is_running = True
            return True
        
        # 2. 检查 websdk 目录是否存在
        if not self.websdk_path.exists():
            log.error(f"❌ 签名服务目录不存在: {self.websdk_path}")
            return False
        
        server_js = self.websdk_path / "server.js"
        if not server_js.exists():
            log.error(f"❌ 签名服务文件不存在: {server_js}")
            return False
        
        # 3. 启动签名服务
        log.info(f"🚀 正在启动签名服务 (端口 {self.port})...")
        
        try:
            # 使用 subprocess 启动 Node.js 服务
            self._process = subprocess.Popen(
                ["node", "server.js"],
                cwd=str(self.websdk_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # 创建新的进程组
            )
            
            # 等待服务启动
            max_retries = 5
            for i in range(max_retries):
                await asyncio.sleep(1)
                if await self.check_health():
                    log.info(f"✅ 签名服务启动成功 (PID: {self._process.pid})")
                    self._is_running = True
                    # 启动健康检查任务
                    self._health_check_task = asyncio.create_task(self._periodic_health_check())
                    return True
                else:
                    log.debug(f"等待签名服务启动... ({i+1}/{max_retries})")
            
            # 启动失败
            log.error("❌ 签名服务启动超时")
            if self._process:
                self._process.kill()
                self._process = None
            return False
            
        except Exception as e:
            log.error(f"❌ 启动签名服务失败: {e}")
            return False
    
    async def _periodic_health_check(self, interval: int = 60):
        """定期健康检查,如果服务挂掉则自动重启"""
        while self._is_running:
            try:
                await asyncio.sleep(interval)
                
                if not await self.check_health():
                    log.warning("⚠️ 签名服务健康检查失败,尝试重启...")
                    self._is_running = False
                    
                    # 杀掉旧进程
                    if self._process:
                        try:
                            self._process.kill()
                            self._process.wait(timeout=2)
                        except Exception:
                            pass
                        self._process = None
                    
                    # 重启
                    await asyncio.sleep(2)
                    if await self.start_if_needed():
                        log.info("✅ 签名服务重启成功")
                    else:
                        log.error("❌ 签名服务重启失败")
                        break
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"健康检查异常: {e}")
    
    async def stop(self):
        """停止签名服务"""
        self._is_running = False
        
        # 停止健康检查任务
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
        
        # 停止签名服务进程
        if self._process:
            try:
                log.info("正在停止签名服务...")
                self._process.terminate()
                self._process.wait(timeout=5)
                log.info("✅ 签名服务已停止")
            except Exception as e:
                log.warning(f"停止签名服务时出现异常: {e}")
                try:
                    self._process.kill()
                except Exception:
                    pass
            finally:
                self._process = None


# 全局单例
_signer_manager: Optional[SignerManager] = None


def get_signer_manager() -> SignerManager:
    """获取签名服务管理器单例"""
    global _signer_manager
    if _signer_manager is None:
        _signer_manager = SignerManager()
    return _signer_manager


async def ensure_signer_running() -> bool:
    """确保签名服务正在运行"""
    manager = get_signer_manager()
    return await manager.start_if_needed()
