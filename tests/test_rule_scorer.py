"""规则购车意向打分引擎测试

样本全部来自真实直播间弹幕（2026-11-07 采集）
"""

import pytest

from src.intelligence.rule_scorer import RuleScorer, HIGH_THRESHOLD, MEDIUM_THRESHOLD
from src.models.lead import IntentLevel


@pytest.fixture
def scorer():
    return RuleScorer(min_level="low")


# ============================================================
# 强意向 — 真实高意向弹幕
# ============================================================

HIGH_INTENT_SAMPLES = [
    "首付加保险以及其它费用，一共多少钱？",
    "我想问落地价格 车看好了 yu7标准长续航 选个珍珠白加6500那个科技",
    "MAX 靛石绿 加方向盘 加黑色内饰 加20寸轮毂落地多少",
    "yu7 pro 加智能舒享套装 加20寸梅花轮毂，落地多少",
    "首付多少钱",
    "置换落地多少？",
    "落地在多少[看]分期",
    "这个裸车现在优惠十几万？？？ 现在多少",
    "N90多久可以提车",
    "店在哪里",
    "现在有置换补贴不",
    "具体位置",
]


@pytest.mark.parametrize("text", HIGH_INTENT_SAMPLES)
def test_高意向弹幕命中(scorer, text):
    r = scorer.score(text)
    assert r.is_lead, f"未命中: {text}"
    assert r.level in (IntentLevel.HIGH.value, IntentLevel.MEDIUM.value), \
        f"等级偏低: {text} → {r.level} ({r.score})"
    assert r.score >= MEDIUM_THRESHOLD


def test_价格询问单独命中价格类别(scorer):
    r = scorer.score("落地多少")
    assert "价格询问" in r.signals
    assert r.score >= 40


def test_车型提取(scorer):
    assert scorer.score("yu7 pro 落地多少").car_model == "YU7"
    assert scorer.score("su7pro选配轮毂").car_model == "SU7"
    assert scorer.score("N90多久可以提车").car_model == "N90"
    assert scorer.score("这是max？").car_model == "MAX"


def test_判定依据可追溯(scorer):
    r = scorer.score("落地多少")
    assert "命中信号" in r.reason
    assert "价格询问" in r.reason


# ============================================================
# 弱意向
# ============================================================

LOW_INTENT_SAMPLES = [
    "这是max？",
    "这是什么版本",
    "有粉色吗？",
    "丹霞紫还生产吗",
    "看一下YU7",
    "yu7标准版",
]


@pytest.mark.parametrize("text", LOW_INTENT_SAMPLES)
def test_弱意向被识别但不判为高意向(scorer, text):
    r = scorer.score(text)
    assert r.is_lead, f"未命中: {text}"
    assert r.level in (IntentLevel.LOW.value, IntentLevel.MEDIUM.value)
    assert r.score < HIGH_THRESHOLD


# ============================================================
# 噪音 — 不该进线索池
# ============================================================

NOISE_SAMPLES = [
    "发过来了",
    "18年的",
    "星期三，连玩3天",
    "666",
    "哈哈哈",
    "",
]


@pytest.mark.parametrize("text", NOISE_SAMPLES)
def test_噪音不命中(scorer, text):
    r = scorer.score(text)
    assert not r.is_lead, f"误命中: {text}"


# ============================================================
# 排除规则
# ============================================================

def test_自家账号被排除(scorer):
    r = scorer.score("落地多少", nickname="小米汽车 | 成都龙湖滨江体验店",
                     self_names={"小米汽车成都龙湖滨江体验店"})
    assert r.excluded
    assert "自家账号" in r.exclude_reason


def test_营销模板被排除(scorer):
    r = scorer.score("最新优惠政策/福利/报价，省利息，省保险，还有新购补贴，欢迎咨询")
    assert r.excluded
    assert r.exclude_reason in ("营销模板", "门店营销话术")


def test_小风车话术被排除(scorer):
    r = scorer.score("需要精准报价和补贴情况的朋友们点击小风车 预约完成后 产品顾问线上帮")
    assert r.excluded


def test_后台私信报价被排除(scorer):
    r = scorer.score("还不知道价格的老板，后台私信您10月最新梦想家限时专属权益报价单")
    assert r.excluded


def test_投诉维权被排除(scorer):
    r = scorer.score("我要投诉你们，售后太差了")
    assert r.excluded
    assert "售后" in r.exclude_reason


def test_同行引流被排除(scorer):
    r = scorer.score("加微信了解，私域引流")
    assert r.excluded


def test_已是车主被排除(scorer):
    r = scorer.score("我的SU7 开了半年了")
    assert r.excluded
    assert "车主" in r.exclude_reason


def test_车评内容被排除(scorer):
    r = scorer.score("这车测评做得很客观")
    assert r.excluded


# ============================================================
# 等级阈值
# ============================================================

def test_等级映射(scorer):
    assert RuleScorer._to_level(HIGH_THRESHOLD) == IntentLevel.HIGH.value
    assert RuleScorer._to_level(MEDIUM_THRESHOLD) == IntentLevel.MEDIUM.value
    assert RuleScorer._to_level(0) == IntentLevel.NONE.value
    assert RuleScorer._to_level(11) == IntentLevel.NONE.value


def test_同类信号只计一次分(scorer):
    """'多少钱' 出现多次不应把分数刷高"""
    once = scorer.score("落地多少").score
    many = scorer.score("落地多少？多少钱？落地是多少钱").score
    assert once == many


def test_min_level_过滤(scorer):
    s = RuleScorer(min_level="high")
    low = s.score("有粉色吗？")
    assert not s.meets_min_level(low)
    high = s.score("落地多少钱 什么时候提车 店在哪里")
    assert s.meets_min_level(high)


def test_提及车型但无购买行为降到低意向(scorer):
    r = scorer.score("yu7标准版")
    assert r.level == IntentLevel.LOW.value
    assert r.car_model == "YU7"
