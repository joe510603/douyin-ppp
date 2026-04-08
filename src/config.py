"""配置管理模块 — 加载和管理应用配置"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# 项目根目录（app.py 所在目录）
# 在Docker容器中工作目录是 /app，直接使用 cwd
import os
_cwd = Path(os.getcwd())
PROJECT_ROOT = _cwd if (_cwd / "config.yaml").exists() else Path(__file__).parent.parent.parent.resolve()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class AppConfig(BaseModel):
    """应用基础配置"""
    name: str = "douyin-ppp"
    host: str = "0.0.0.0"
    port: int = 9527
    debug: bool = False


class DouyinConfig(BaseModel):
    """抖音相关配置"""
    cookie: str = ""
    cookie_expire_warning_hours: int = 24
    # 手动指定 WebSocket URL 模板，留空则自动获取
    # 在浏览器 F12 中获取抖音直播间的 WebSocket URL，填在这里
    # URL 中的 room_id 会自动替换
    websocket_url_template: str = ""


class MonitorItem(BaseModel):
    """监控账号项"""
    name: str
    sec_user_id: str
    room_id: str = ""
    websocket_url: str = ""  # 手动指定的 WebSocket URL（带签名）
    enabled: bool = True
    tags: list[str] = []


class LiveDetectionConfig(BaseModel):
    """开播检测配置"""
    interval: int = 60          # 检测间隔(秒)
    timeout: int = 15           # 超时(秒)
    max_failures: int = 5       # 连续失败上限
    retry_interval: int = 300   # 失败后重试间隔(秒)


class WebSocketConfig(BaseModel):
    """WebSocket 连接配置"""
    heartbeat_interval: int = 5
    reconnect_initial_delay: int = 2
    reconnect_max_delay: int = 60
    max_reconnect_attempts: int = 0   # 0=无限
    log_unknown_messages: bool = False


class StorageConfig(BaseModel):
    """存储配置"""
    db_path: str = "data/db/douyin_ppp.db"
    export_dir: str = "data/exports"
    flush_interval: int = 10       # 内存刷写间隔(秒)
    excel_max_rows: int = 100000   # 单次导出最大行数


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = "INFO"
    file_path: str = "data/logs/app.log"
    rotation: str = "50 MB"
    retention: str = "7 days"


class Settings(BaseSettings):
    """全局配置（支持环境变量覆盖）"""
    
    app: AppConfig = Field(default_factory=AppConfig)
    douyin: DouyinConfig = Field(default_factory=DouyinConfig)
    monitors: list[MonitorItem] = Field(default_factory=list)
    live_detection: LiveDetectionConfig = Field(default_factory=LiveDetectionConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    class Config:
        env_prefix = "DOUYIN_PPP_"
        extra = "ignore"


def load_config(config_path: Optional[str] = None) -> Settings:
    """
    加载配置文件，返回 Settings 实例。
    
    Args:
        config_path: 配置文件路径，默认使用项目根目录下的 config.yaml
        
    Returns:
        Settings 配置对象
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    
    if not path.exists():
        # 返回默认配置
        return Settings()
    
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    
    return Settings(**data)


def save_config(settings: Settings, config_path: Optional[str] = None) -> None:
    """
    将当前配置保存到 YAML 文件。
    
    Args:
        settings: Settings 配置实例
        config_path: 保存路径，默认为项目根目录 config.yaml
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    
    data = settings.model_dump(exclude_none=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# 全局单例（延迟加载）
_config_instance: Optional[Settings] = None


def get_config() -> Settings:
    """获取全局配置实例（懒加载）"""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config()
    return _config_instance


def reload_config(config_path: Optional[str] = None) -> Settings:
    """重新加载配置（热更新）"""
    global _config_instance
    _config_instance = load_config(config_path)
    return _config_instance
