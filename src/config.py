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
# fallback 用本文件路径推导：src/config.py -> src -> <项目根>
# 修复：原实现多走了一层 .parent，会指向项目的上一级目录
import os
_cwd = Path(os.getcwd())
_PROJECT_ROOT_FALLBACK = Path(__file__).resolve().parent.parent
PROJECT_ROOT = _cwd if (_cwd / "config.yaml").exists() else _PROJECT_ROOT_FALLBACK
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


class XhsConfig(BaseModel):
    """小红书相关配置"""
    cookie: str = ""
    cookie_expire_warning_hours: int = 24


class DouyinVideoConfig(BaseModel):
    """抖音视频评论配置（独立于直播监控的 Cookie）"""
    cookie: str = ""
    cookie_expire_warning_hours: int = 24


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


class LLMFeaturesConfig(BaseModel):
    """LLM 分析功能开关"""
    sentiment: bool = True       # 情感分析
    intent: bool = True          # 意图分类
    competitor: bool = False     # 竞品识别
    user_profile: bool = False   # 用户画像


class LLMConfig(BaseModel):
    """LLM 模型配置"""
    enabled: bool = False
    provider: str = "openai"     # openai / deepseek / minimax / minimax_intl / local / custom
    api_key: str = ""
    base_url: str = ""           # 可选，用于代理或本地服务；留空则按 provider 取默认值
    model: str = "gpt-4o-mini"   # 模型名称

    # 成本控制
    max_cost_per_batch: float = 0.5   # 每批最大成本（元）
    daily_budget: float = 10.0         # 每日预算上限（元）

    # 分析功能开关
    features: LLMFeaturesConfig = Field(default_factory=LLMFeaturesConfig)

    # 批处理配置
    batch_size: int = 50         # 每批处理评论数
    timeout: int = 60            # API 超时时间（秒）
    max_tokens: int = 4000       # 单次响应最大 token
    temperature: float = 0.2     # 分类任务用低温，保证结构化输出稳定


class LeadConfig(BaseModel):
    """购车意向线索筛选配置"""
    enabled: bool = False
    # 实时判定：弹幕进来即打分（纯规则，不依赖 LLM）
    realtime_enabled: bool = True
    # 实时命中后是否立即推送飞书群
    realtime_push: bool = True
    # 同一用户两次推送的最小间隔（分钟），防刷屏
    push_cooldown_minutes: int = 30
    # 只有达到该意向等级的弹幕才进入线索池（high / medium / low）
    min_intent_level: str = "low"
    # 扫描回溯时间窗（小时）
    lookback_hours: int = 24
    # 后台自动扫描间隔（秒）
    scan_interval: int = 300
    # 每批送给 LLM 的用户数
    batch_size: int = 20
    # 每日 LLM 调用预算（次），超限自动暂停扫描
    daily_call_limit: int = 200
    # 是否把联系方式单独抽成高优先级线索
    contact_priority: bool = True


class NotifyConfig(BaseModel):
    """飞书群机器人通知配置"""
    enabled: bool = False
    webhook_url: str = ""
    # 加签密钥（群机器人「签名校验」开启时必填）
    secret: str = ""
    # 推送哪些意向等级
    push_levels: list[str] = ["high", "medium"]
    # 只推送带联系方式的线索
    contact_only: bool = False
    # 单条消息最多包含的线索数
    max_per_message: int = 5
    # 推送后是否把线索标记为「已推送」
    mark_pushed: bool = True

    # ---- 多维表格（线索表）----
    bitable_enabled: bool = False
    bitable_app_id: str = ""       # 企业自建应用 App ID
    bitable_app_secret: str = ""   # 企业自建应用 App Secret
    bitable_app_token: str = ""    # 多维表格 App Token
    bitable_table_id: str = ""     # 数据表 ID（tbl...）
    # 是否在推送群消息的同时写入多维表格
    bitable_sync_with_push: bool = True
    # 备选认证：用户 access token（与企业自建应用二选一）。
    # 后台自跑程序推荐用 app_id/app_secret；user_token 适合临时排查，
    # 但会过期，过期后需要重新授权。
    bitable_user_token: str = ""
    # 写入后端：auto / http / lark_cli
    #   http     —— 直连开放平台（app 凭据或 user_token）
    #   lark_cli —— 交给 lark-cli 写，认证与 token 续期由 lark-cli 负责
    #   auto     —— 有 app 凭据走 http，否则走 lark_cli
    bitable_backend: str = "auto"
    # lark_cli 后端：用哪个身份调 lark-cli（user = 当前授权用户，bot = 绑定应用）
    bitable_lark_cli_identity: str = "user"
    # lark_cli 可执行文件路径（留空自动探测；launchd 的 PATH 很短，建议显式指定）
    bitable_lark_cli_path: str = ""
    # 哪些意向等级自动写入多维表格
    bitable_levels: list[str] = ["high", "medium", "low"]
    # 同一用户两次写表的最小间隔（分钟），避免频繁更新
    bitable_cooldown_minutes: int = 5
    # 多久把飞书表里销售改过的状态拉回本地（分钟）。
    # 「处理状态」「触达状态」是双向字段：销售在飞书改，本地也要跟着变。
    bitable_pull_interval_minutes: int = 5


class OutreachConfig(BaseModel):
    """抖音触达（搜人 + 作品留言）配置"""
    enabled: bool = False
    # 全自动模式（关闭时需在页面上手动触发）
    auto_mode: bool = False
    # 每日留言上限（防风控）
    daily_limit: int = 30
    # 两次留言之间的随机延时区间（秒）
    min_delay: float = 45.0
    max_delay: float = 120.0
    # 单次任务最多处理的线索数
    max_per_run: int = 10
    # 每个用户留言的作品数量
    comments_per_user: int = 1
    # 连续失败多少次自动停止本轮任务
    fail_stop_threshold: int = 3
    # 留言文案模板，{name} 昵称 {model} 车型 {nickname} 抖音号
    message_template: str = "看到你在{live_room}聊到{model}，我们门店在售同款车型，方便留个联系方式细聊吗？"
    # 浏览器操作超时（秒）
    timeout: int = 30


class Settings(BaseSettings):
    """全局配置（支持环境变量覆盖）"""
    
    app: AppConfig = Field(default_factory=AppConfig)
    douyin: DouyinConfig = Field(default_factory=DouyinConfig)
    douyin_video: DouyinVideoConfig = Field(default_factory=DouyinVideoConfig)
    xhs: XhsConfig = Field(default_factory=XhsConfig)
    monitors: list[MonitorItem] = Field(default_factory=list)
    live_detection: LiveDetectionConfig = Field(default_factory=LiveDetectionConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    lead: LeadConfig = Field(default_factory=LeadConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)

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
