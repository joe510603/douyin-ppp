"""重试工具 — 支持指数退避的异步重试机制"""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Callable, TypeVar, Any
from dataclasses import dataclass

from src.utils.logger import get_logger

log = get_logger("retry")

# Python 3.9 兼容：ParamSpec 在 3.10+ 引入，用简单的 TypeVar 替代
if sys.version_info >= (3, 10):
    from typing import ParamSpec
    P = ParamSpec("P")
else:
    P = TypeVar("P")  # 退化为普通 TypeVar，仅用于签名

T = TypeVar("T")


@dataclass
class RetryPolicy:
    """重试策略配置"""
    max_attempts: int = 5          # 最大重试次数（0=无限）
    initial_delay: float = 1.0     # 初始等待秒数
    max_delay: float = 60.0        # 最大等待秒数
    exponential_base: float = 2.0  # 退避倍率
    jitter: bool = True            # 是否添加随机抖动


DEFAULT_POLICY = RetryPolicy()


async def async_retry(
    func: Callable[..., Any],
    *args: Any,
    policy: RetryPolicy = DEFAULT_POLICY,
    **kwargs: Any,
) -> T:
    """
    异步函数重试装饰器（函数式调用方式）。
    
    Args:
        func: 要执行的异步函数
        *args: 函数位置参数
        policy: 重试策略
        **kwargs: 函数关键字参数
        
    Returns:
        函数返回值
        
    Raises:
        Exception: 重试耗尽后的最后一个异常
    """
    attempt = 0
    current_delay = policy.initial_delay
    
    while True:
        try:
            result = await func(*args, **kwargs)
            if attempt > 0:
                log.info(f"{func.__name__} 在第 {attempt + 1} 次尝试成功")
            return result
        except Exception as e:
            attempt += 1
            
            if policy.max_attempts > 0 and attempt >= policy.max_attempts:
                log.error(f"{func.__name__} 重试 {attempt} 次均失败: {e}")
                raise
            
            delay = min(current_delay, policy.max_delay)
            if policy.jitter:
                import random
                delay *= (0.5 + random.random())
            
            log.warning(
                f"{func.__name__} 第 {attempt} 次失败: {e}, "
                f"等待 {delay:.1f}s 后重试..."
            )
            await asyncio.sleep(delay)
            current_delay *= policy.exponential_base


def retry_decorator(
    max_attempts: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
):
    """
    异步重试装饰器工厂（语法糖方式）。
    
    用法:
        @retry_decorator(max_attempts=3)
        async def connect_ws():
            ...
    """
    policy = RetryPolicy(
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        jitter=jitter,
    )
    
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await async_retry(func, *args, policy=policy, **kwargs)
        return wrapper
    
    return decorator


class ReconnectManager:
    """WebSocket 断线重连管理器"""
    
    def __init__(self, policy: RetryPolicy = None):
        self.policy = policy or DEFAULT_POLICY
        self._attempt_count = 0
        self._consecutive_failures = 0
    
    @property
    def attempts(self) -> int:
        return self._attempt_count
    
    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
    
    def reset(self):
        """连接成功后重置计数器"""
        self._attempt_count = 0
        self._consecutive_failures = 0
    
    def record_failure(self):
        """记录一次失败"""
        self._attempt_count += 1
        self._consecutive_failures += 1
    
    def record_success(self):
        """记录一次成功连接"""
        self.reset()
    
    @property
    def should_give_up(self) -> bool:
        """是否应该放弃重连"""
        return (
            self.policy.max_attempts > 0 
            and self._attempt_count >= self.policy.max_attempts
        )
    
    def next_delay(self) -> float:
        """计算下一次重连的等待时间（秒）"""
        delay = self.policy.initial_delay * (
            self.policy.exponential_base ** self._attempt_count
        )
        delay = min(delay, self.policy.max_delay)
        if self.policy.jitter:
            import random
            delay *= (0.5 + random.random())
        return delay
