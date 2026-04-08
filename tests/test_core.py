"""基础单元测试"""

import pytest
import asyncio
from datetime import datetime

# 测试数据模型
from src.models.comment import LiveComment, MessageType, MonitorAccount


class TestLiveComment:
    def test_create_chat_comment(self):
        c = LiveComment(
            message_type=MessageType.CHAT,
            content="测试弹幕",
            user_id="12345",
            user_nickname="测试用户",
            room_id="room001",
        )
        assert c.is_chat is True
        assert c.is_gift is False
        assert c.content == "测试弹幕"
    
    def test_gift_comment(self):
        c = LiveComment(
            message_type=MessageType.GIFT,
            gift_name="小心心",
            gift_count=10,
            content="[礼物] 小心心",
            room_id="room001",
        )
        assert c.is_gift is True
    
    def test_model_dump(self):
        c = LiveComment(content="test", create_time=datetime(2025, 1, 1, 12, 0))
        d = c.model_dump()
        assert "content" in d
        assert d["content"] == "test"


class TestMonitorAccount:
    def test_default_enabled(self):
        acc = MonitorAccount(name="test", sec_user_id="abc123")
        assert acc.enabled is True
        assert acc.status == "idle"
    
    def test_fields(self):
        acc = MonitorAccount(
            name="账号A",
            sec_user_id="MS4wLjABAAAAxxx",
            tags=["标签1", "标签2"],
        )
        assert len(acc.tags) == 2


class TestCommentProcessor:
    def test_clean_text(self):
        from src.processor.comment_processor import CommentProcessor
        
        proc = CommentProcessor()
        
        # 正常文本
        assert proc._clean_text("hello world") == "hello world"
        
        # 含零宽字符
        assert "\u200b" not in proc._clean_text("hello\u200bworld")
        
        # 空字符串
        assert proc._clean_text("") == ""
        
        # 多余空白
        assert proc._clean_text("hello   world") == "hello world"
    
    def test_dedup(self):
        from src.processor.comment_processor import CommentProcessor
        
        proc = CommentProcessor(dedup_window_seconds=60.0)
        
        c1 = LiveComment(content="hello", user_id="user1")
        c2 = LiveComment(content="hello", user_id="user1")
        
        assert proc.process(c1) is not None  # 第一次通过
        assert proc.process(c2) is None       # 第二次重复被过滤
    
    def test_different_users_not_dedup(self):
        from src.processor.comment_processor import CommentProcessor
        
        proc = CommentProcessor(dedup_window_seconds=60.0)
        
        c1 = LiveComment(content="hello", user_id="user1")
        c2 = LiveComment(content="hello", user_id="user2")
        
        assert proc.process(c1) is not None
        assert proc.process(c2) is not None   # 不同用户不去重


class TestConfig:
    def test_load_default_config(self):
        from src.config import load_config, Settings
        
        config = load_config()  # 应能加载默认值
        assert isinstance(config, Settings)
        assert config.app.port == 8080
    
    def test_config_monitors(self):
        from src.config import load_config
        
        config = load_config()
        assert isinstance(config.monitors, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
