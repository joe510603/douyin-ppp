"""评论数据处理模块 — 清洗、去重、格式化"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Optional

from ..models.comment import LiveComment, MessageType
from src.utils.logger import get_logger

log = get_logger("processor")


class CommentProcessor:
    """
    评论数据处理器。
    
    负责：
    - 数据清洗（去除无效字符、截断超长内容）
    - 智能去重（基于用户+时间窗口）
    - 敏感词过滤（可选）
    - 数据格式化
    """
    
    def __init__(self, dedup_window_seconds: float = 30.0, max_content_length: int = 500):
        """
        Args:
            dedup_window_seconds: 去重时间窗口（秒），同一用户在此时间内相同内容视为重复
            max_content_length: 最大内容长度限制
        """
        self.dedup_window = dedup_window_seconds
        self.max_content_length = max_content_length
        # 去重缓存：{(user_id, content_hash): last_seen_timestamp}
        self._dedup_cache: OrderedDict = OrderedDict()
        self._max_cache_size = 10000
    
    def process(self, comment: LiveComment) -> Optional[LiveComment]:
        """
        处理单条评论。
        
        Returns:
            处理后的评论，如果判定为无效/重复则返回 None
        """
        # 1. 只保留弹幕/评论类型（过滤掉进场/礼物等非文字消息）
        if comment.message_type != MessageType.CHAT:
            return comment
        
        # 2. 清洗内容
        cleaned = self._clean_text(comment.content)
        if not cleaned.strip():
            return None
        
        comment.content = cleaned[:self.max_content_length]
        
        # 3. 去重
        if self._is_duplicate(comment.user_id, comment.content):
            return None
        
        # 4. 格式化补充字段
        if not comment.create_time or comment.create_time.year == 1900:
            comment.create_time = datetime.now()
        
        return comment
    
    def process_batch(self, comments: list[LiveComment]) -> list[LiveComment]:
        """批量处理"""
        results = []
        for c in comments:
            processed = self.process(c)
            if processed is not None:
                results.append(processed)
        return results
    
    def _clean_text(self, text: str) -> str:
        """清洗文本：去除特殊控制字符"""
        if not text:
            return ""
        
        import re
        # 移除零宽字符、控制字符
        text = re.sub(r'[\u200b-\u200f\ufeff\u202a-\u202e]', '', text)
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        # 合理空白
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text
    
    def _is_duplicate(self, user_id: str, content: str) -> bool:
        """判断是否重复"""
        cache_key = (user_id, hash(content))
        
        now = datetime.now().timestamp()
        last_seen = self._dedup_cache.get(cache_key)
        
        if last_seen is not None and (now - last_seen) < self.dedup_window:
            return True
        
        self._dedup_cache[cache_key] = now
        
        # 缓存大小限制
        while len(self._dedup_cache) > self._max_cache_size:
            self._dedup_cache.popitem(last=False)
        
        return False
    
    def clear_cache(self):
        """清空去重缓存"""
        self._dedup_cache.clear()
