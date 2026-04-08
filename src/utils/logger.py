"""日志工具 — 基于 Loguru 的日志管理"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from loguru import logger


def setup_logging(
    level: str = "INFO",
    file_path: Optional[str] = None,
    rotation: str = "50 MB",
    retention: str = "7 days",
) -> None:
    """
    初始化日志配置。
    
    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        file_path: 日志文件路径，None 表示仅输出到控制台
        rotation: 日志轮转大小
        retention: 日志保留时长
    """
    logger.remove()  # 移除默认 handler
    
    # 控制台输出格式
    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level:<7}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    logger.add(sys.stderr, level=level, format=fmt)
    
    # 文件输出（如果指定了路径）
    if file_path:
        log_dir = Path(file_path).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        
        logger.add(
            file_path,
            level=level,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            format=fmt,
        )


def get_logger(name: Optional[str] = None):
    """
    获取 logger 实例。
    
    Args:
        name: logger 名称（可选）
        
    Returns:
        Loguru logger 绑定到指定名称
    """
    if name:
        return logger.bind(name=name)
    return logger
