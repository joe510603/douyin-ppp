"""签名工具 — JS 签名算法执行器封装"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from src.utils.logger import get_logger

log = get_logger("signer")

# JS 文件路径（相对于项目根目录）
JS_DIR = Path(__file__).parent.parent.parent / "proto"
SIGN_JS_FILE = JS_DIR / "sign.js"
A_BOGUS_JS_FILE = JS_DIR / "a_bogus.js"

_js_context = None


def _get_js_context():
    """懒初始化 JS 执行上下文"""
    global _js_context
    if _js_context is not None:
        return _js_context
    
    try:
        import js2py
        _js_context = js2py.EvalJs()
        log.info("使用 js2py 作为 JS 引擎")
        return _js_context
    except ImportError:
        pass
    
    try:
        import execjs
        _js_context = execjs.get().compile("")
        log.info(f"使用 PyExecJS ({execjs.get().name}) 作为 JS 引擎")
        return _js_context
    except ImportError:
        pass
    
    raise ImportError(
        "未找到可用的 JS 执行引擎。请安装: pip install PyExecJS 或 pip install js2py\n"
        "如使用 Node.js 引擎，请确保已安装 Node.js。"
    )


def generate_signature(url_or_params: str, js_file: Optional[str] = None) -> str:
    """
    生成抖音请求签名。
    
    Args:
        url_or_params: 需要签名的 URL 参数字符串或完整 URL
        js_file: 签名算法 JS 文件路径（默认使用 proto/sign.js）
        
    Returns:
        签名字符串
    """
    ctx = _get_js_context()
    js_path = Path(js_file) if js_file else SIGN_JS_FILE
    
    if not js_path.exists():
        log.warning(f"签名 JS 文件不存在: {js_path}")
        return ""
    
    try:
        code = js_path.read_text(encoding="utf-8")
        if hasattr(ctx, 'execute'):
            ctx.execute(code)
            result = ctx.call('get_sign', url_or_params)
        else:
            # js2py 方式
            ctx.execute(code)
            result = ctx.get_sign(url_or_params)
        
        return str(result) if result else ""
    except Exception as e:
        log.error(f"签名生成失败: {e}")
        return ""


def generate_a_bogus(params: str, user_agent: str = "", js_file: Optional[str] = None) -> str:
    """
    生成 a_bogus 参数（抖音反爬签名）。
    
    参考 MediaCrawler 项目实现：
    https://github.com/NanmiCoder/MediaCrawler
    
    Args:
        params: URL 参数字符串（如 "aid=6383&device_platform=webapp"）
        user_agent: User-Agent 字符串
        js_file: a_bogus JS 文件路径（默认使用 proto/douyin.js）
        
    Returns:
        a_bogus 字符串
    """
    js_path = Path(js_file) if js_file else JS_DIR / "douyin.js"
    
    if not js_path.exists():
        log.warning(f"a_bogus JS 文件不存在: {js_path}")
        return ""
    
    # 默认 User-Agent
    if not user_agent:
        user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    
    try:
        import execjs
        
        # 读取并编译 JS 文件
        code = js_path.read_text(encoding="utf-8-sig")
        ctx = execjs.compile(code)
        
        # 调用签名函数（MediaCrawler 使用 sign_datail 函数）
        result = ctx.call("sign_datail", params, user_agent)
        
        return str(result) if result else ""
    except ImportError:
        log.error("未安装 PyExecJS，请运行: pip install PyExecJS")
        return ""
    except Exception as e:
        log.error(f"a_bogus 生成失败: {e}", exc_info=True)
        return ""


def sign_websocket_url(room_id: str, cookie: str = "") -> str:
    """
    为 WebSocket 连接生成完整的签名 URL。
    
    Args:
        room_id: 直播间 room_id
        cookie: 抖音 cookie（用于提取 ttwid 等）
        
    Returns:
        带签名的 WebSocket URL
    """
    from urllib.parse import urlencode, urlparse, parse_qs, urlunparse
    
    base_url = f"wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?app_name=douyin_web&version_code=19300000&live_platform=web&room_id={room_id}"
    
    try:
        sig = generate_signature(base_url)
        if sig:
            separator = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{separator}{sig}"
    except Exception as e:
        log.warning(f"WS URL 签名失败，使用原始 URL: {e}")
    
    return base_url
