"""Protobuf 消息解析模块 — 动态解析抖音直播间弹幕协议"""

from __future__ import annotations

from typing import Optional, Any


def parse_message(method: str, payload: bytes) -> Optional[dict[str, Any]]:
    """
    解析 Protobuf 二进制消息。
    
    这是一个占位实现。完整实现需要：
    1. 编译 .proto 文件生成 Python 代码
    2. 或使用动态 protobuf 反射
    
    Args:
        method: 消息方法名（如 "WebcastChatMessage"）
        payload: 二进制载荷
        
    Returns:
        解析后的字典，包含 content, user_id, user_nickname 等字段
        如果无法解析返回 None
    """
    try:
        # 尝试使用编译好的 proto 模块（如果存在）
        from . import pb_messages as pbm
        return pbm.parse_message(method, payload)
    except ImportError:
        pass
    
    # 回退：简单文本提取
    return _fallback_parse(payload)


def _fallback_parse(data: bytes) -> dict[str, Any]:
    """回退解析：从二进制中提取可读字段"""
    result = {}
    strings = _extract_strings(data)
    
    if len(strings) >= 3:
        result["content"] = strings[0][:500]
        if len(strings) >= 5:
            result["user_nickname"] = strings[1]
            result["user_id"] = strings[2]
    
    return result


def _extract_strings(data: bytes) -> list[str]:
    """从二进制数据中提取 UTF-8 字符串"""
    strings = []
    current = b""
    
    for byte in data:
        if 32 <= byte < 127 or byte >= 0x80:
            current += bytes([byte])
        else:
            if len(current) >= 2:
                try:
                    s = current.decode("utf-8")
                    if s.strip():
                        strings.append(s)
                except UnicodeDecodeError:
                    pass
            current = b""
    
    if len(current) >= 2:
        try:
            s = current.decode("utf-8")
            if s.strip():
                strings.append(s)
        except UnicodeDecodeError:
            pass
    
    return strings
