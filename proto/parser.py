"""Protobuf 消息解析器 - 使用完整编译的 Protobuf 类"""

from __future__ import annotations

from typing import Optional, Any
import gzip

# 导入编译好的 Protobuf 类
try:
    from . import dy_pb2 as pb
    HAS_PROTOBUF = True
except ImportError:
    HAS_PROTOBUF = False
    from ..utils.logger import get_logger
    log = get_logger("proto")
    log.warning("Protobuf 模块未编译,将使用简化解析")


def parse_push_frame(data: bytes) -> list[tuple[int, bytes]]:
    """
    解析 PushFrame 数据包
    
    Returns:
        [(message_type, payload_bytes), ...]
    """
    if not HAS_PROTOBUF:
        return _fallback_parse_push_frame(data)
    
    messages = []
    try:
        push_frame = pb.PushFrame()
        push_frame.ParseFromString(data)
        
        payload = push_frame.payload
        if push_frame.payloadEncoding == 'gzip':
            payload = gzip.decompress(payload)
        
        response = pb.Response()
        response.ParseFromString(payload)
        
        for msg in response.messagesList:
            messages.append((msg.msgType, msg.payload))
        
        return messages
        
    except Exception as e:
        return _fallback_parse_push_frame(data)


def parse_chat_message(payload: bytes) -> Optional[dict[str, Any]]:
    """解析弹幕消息"""
    if not HAS_PROTOBUF:
        return None
    
    try:
        chat_msg = pb.ChatMessage()
        chat_msg.ParseFromString(payload)
        
        return {
            "content": chat_msg.content,
            "user_id": chat_msg.user.id if chat_msg.user else None,
            "user_nickname": chat_msg.user.nickName if chat_msg.user else None,
            "user_avatar": chat_msg.user.AvatarThumb.uri if chat_msg.user and chat_msg.user.AvatarThumb else None,
            "timestamp": chat_msg.common.createTime if chat_msg.common else None,
        }
    except Exception:
        return None


def parse_gift_message(payload: bytes) -> Optional[dict[str, Any]]:
    """解析礼物消息"""
    if not HAS_PROTOBUF:
        return None
    
    try:
        gift_msg = pb.GiftMessage()
        gift_msg.ParseFromString(payload)
        
        return {
            "gift_id": gift_msg.giftId,
            "gift_name": gift_msg.gift.describe if gift_msg.gift else None,
            "gift_count": gift_msg.repeatCount,
            "user_id": gift_msg.user.id if gift_msg.user else None,
            "user_nickname": gift_msg.user.nickName if gift_msg.user else None,
        }
    except Exception:
        return None


def parse_member_message(payload: bytes) -> Optional[dict[str, Any]]:
    """解析进场消息"""
    if not HAS_PROTOBUF:
        return None
    
    try:
        member_msg = pb.MemberMessage()
        member_msg.ParseFromString(payload)
        
        return {
            "user_id": member_msg.user.id if member_msg.user else None,
            "user_nickname": member_msg.user.nickName if member_msg.user else None,
            "member_count": member_msg.memberCount,
        }
    except Exception:
        return None


def _fallback_parse_push_frame(data: bytes) -> list[tuple[int, bytes]]:
    """回退的 PushFrame 解析"""
    import struct
    
    messages = []
    offset = 0
    length = len(data)
    
    # 简化的二进制解析
    while offset < length:
        if offset + 20 > length:
            break
        
        # 尝试解析头部 (简化版)
        try:
            header_len = struct.unpack_from("<I", data, offset)[0]
            if header_len > 1024 or header_len < 10:
                break
            
            # 跳过头部找到 payload
            payload_offset = offset + header_len
            if payload_offset + 4 > length:
                break
            
            payload_len = struct.unpack_from("<I", data, payload_offset)[0]
            if payload_offset + 4 + payload_len > length:
                break
            
            payload = data[payload_offset + 4:payload_offset + 4 + payload_len]
            
            # 尝试解压
            if len(payload) >= 2 and payload[:2] == b'\x1f\x8b':
                payload = gzip.decompress(payload)
            
            messages.append((0, payload))  # msg_type=0 表示未知
            
            offset = payload_offset + 4 + payload_len
        except Exception:
            break
    
    return messages