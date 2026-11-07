"""智能分析层测试 — 联系方式提取 + 提示词解析"""

import pytest

from src.intelligence.contact_extractor import extract_contact, extract_contacts
from src.intelligence.prompts import parse_json_array, parse_json_object, build_intent_prompt


class Test手机号提取:
    def test_标准手机号(self):
        info = extract_contact("我的电话是13812345678")
        assert info.phone == "13812345678"
        assert info.has_contact
        assert "phone" in info.contact_type

    def test_带分隔符(self):
        info = extract_contact("138-1234-5678")
        assert info.phone == "13812345678"

    def test_空格分隔(self):
        info = extract_contact("138 1234 5678 找我")
        assert info.phone == "13812345678"

    def test_抖音号不被误判为电话(self):
        """回归：autoengine-monitor 曾把「用户4151082501973」里的
        「15108250197」误判成电话"""
        info = extract_contact("用户4151082501973")
        assert info.phone == ""

    def test_抖音号带前缀不被误判(self):
        info = extract_contact("抖音号：13800138000")
        assert info.phone == ""

    def test_非手机号段不匹配(self):
        # 10 位不合法长度
        assert extract_contact("1234567890").phone == ""
        # 12 位以上不匹配内部片段
        assert extract_contact("138123456789012").phone == ""


class Test微信QQ提取:
    def test_微信加v(self):
        info = extract_contact("加V：xiao_ming_2024")
        assert info.wechat == "xiao_ming_2024"

    def test_微信中文冒号(self):
        info = extract_contact("微信：lisi888")
        assert info.wechat == "lisi888"

    def test_微信无触发词不提取(self):
        """没有上下文触发词时，不该把普通英文单词当微信号"""
        info = extract_contact("hello world")
        assert info.wechat == ""

    def test_QQ号(self):
        info = extract_contact("QQ：123456789")
        assert info.qq == "123456789"

    def test_广告引流标记(self):
        info = extract_contact("加微信了解，私域引流")
        assert info.is_spam


class Test合并提取:
    def test_多弹幕合并(self):
        merged = extract_contacts([
            "多少钱呀",
            "我电话13812345678",
            "微信 tom_2024",
        ])
        assert merged.phone == "13812345678"
        assert merged.wechat == "tom_2024"
        assert "电话" in merged.value
        assert "微信" in merged.value

    def test_空输入(self):
        merged = extract_contacts([])
        assert not merged.has_contact
        assert merged.value == ""


class TestJSON解析:
    def test_标准数组(self):
        raw = '[{"a": 1}, {"b": 2}]'
        assert len(parse_json_array(raw)) == 2

    def test_markdown包裹(self):
        raw = '```json\n[{"intent_level": "high"}]\n```'
        assert parse_json_array(raw)[0]["intent_level"] == "high"

    def test_前后有文字(self):
        raw = '好的，结果如下：\n[{"x": 1}]\n以上。'
        assert len(parse_json_array(raw)) == 1

    def test_垃圾输入返回空(self):
        assert parse_json_array("完全不是 JSON") == []
        assert parse_json_array("") == []

    def test_单对象(self):
        raw = '{"message": "你好", "reason": "x"}'
        obj = parse_json_object(raw)
        assert obj["message"] == "你好"


class Test提示词构造:
    def test_包含用户与弹幕(self):
        prompt = build_intent_prompt([
            {"user_id": "u1", "display_id": "d1", "nickname": "小明",
             "messages": ["这个多少钱", "有现车吗"]},
        ])
        assert "u1" in prompt
        assert "这个多少钱" in prompt
        assert "1 位观众" in prompt

    def test_超长弹幕被截断(self):
        prompt = build_intent_prompt([
            {"user_id": "u1", "display_id": "", "nickname": "",
             "messages": ["啊" * 500]},
        ])
        assert "啊" * 500 not in prompt
