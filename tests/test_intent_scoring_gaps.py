"""**打分器的两个盲区** —— 客户被丢掉 + 门店口播挡不住（2026-11-17）

盲区 1：利息类话术一个都不认 → 真客户被判 0 分、**直接丢掉**
-----------------------------------------------------------
买车的人最关心的就是「利息」，但「金融补贴」词表里一个都没有：

    免息|分期|贷款|首付|置换|补贴|置换补|置换政策|权益|优惠|折扣|降价|以旧换新

实测 29 句真实购车问法里 **9 句被判 0 分**，
其中 8 句在问利息/手续费/月供 —— 全被静默丢掉（线索池里根本没有）。
门店自己的广告词「享0息提车」都认得，客户问「几年0利息」却认不得。

盲区 2：门店口播内容挡不住，只能靠账号身份 → 单点防线
---------------------------------------------------------
排除自家账号靠 secUid 比对（自家账号集合 + 当前主播），
但**内容层面**完全挡不住门店的通用广告脚本：

    "SU7&YU7、澎程方案、首付、分期、全款、补贴、点击右下角私信主播领取"
      → excluded=False, score=72 (high!)

只要有一个门店账号不在监控名单里、或那条弹幕的 secUid 读不到，
它就会变成「高意向客户线索」，程序对着同行/自己人的号发广告
（2026-11-17 实测踩过：给金牛凯德官方号真发了一条）。

要钉住的
--------
1. 利息/手续费/月供/置换抵扣 → 必须命中「金融补贴」
2. 门店广告脚本 → 必须被内容防线排除
3. **真客户绝不能被误伤**（这条最要紧 —— 误伤就是丢线索）
4. 无关闲聊仍然 0 分
5. 昵称归一化要扛得住「认证徽章」后缀和两种竖线字形
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.intelligence.intent_analyzer import _build_self_names, _normalize_name
from src.intelligence.rule_scorer import RuleScorer


def _scorer() -> RuleScorer:
    return RuleScorer(min_level="low")


# ---------------------------------------------------------------- 盲区 1

def test_interest_phrases_are_recognised():
    """利息/手续费/月供/置换抵扣 —— 这些是买车最关心的问题，不能判 0 分"""
    sc = _scorer()
    phrases = [
        "几年0利息", "0利息", "零利息", "利息多少", "利息怎么算",
        "有无手续费", "手续费多少", "月供多少",
        "旧车能抵多少", "抵扣多少钱",
    ]
    missed = []
    for t in phrases:
        r = sc.score(t, "客户", set())
        if r.score <= 0 or "金融补贴" not in r.signals:
            missed.append((t, r.level, r.score, r.signals))
    assert not missed, f"这些高意向问法被判 0 分（被丢掉）：{missed}"


def test_zero_interest_does_not_match_larger_numbers():
    """「0息」要能命中「享0息」，但不能把「10息」也当成 0 息

    词表里写的是 ``(?<![0-9])0\\s*息`` —— 负向断言保证
    「享0息」「0利息」能命中，而「10息」里的「0息」不吃掉。
    """
    import re

    from src.intelligence.rule_scorer import SIGNAL_CATEGORIES

    pattern = next(p for name, _w, p in SIGNAL_CATEGORIES if name == "金融补贴")
    rx = re.compile(pattern, re.IGNORECASE)

    assert rx.search("享0息提车"), "「享0息」应该命中"
    assert rx.search("0利息"), "「0利息」应该命中"
    assert not rx.search("10息"), "「10息」不该被当成「0息」"
    assert not rx.search("2020息"), "「2020息」不该被当成「0息」"


# ---------------------------------------------------------------- 盲区 2

STORE_ADS = [
    "SU7&YU7、澎程方案、首付、分期、全款、补贴、点击右下角私信主播领取",
    "SU7-YU7-澎程，首付、分期、全款、补贴各种方案，点击右下角私信主播领取！！",
    "点击右下角小卡片，领取免费报价单[比心]",
    "SU7&YU7，澎程现车可售，享0息提车私信了解",
    "首付、分期、全款、补贴各种方案",
    # 实测库里真实出现过的（2026-11-10 起的真实弹幕）
    "最新的优惠政策/福利/报价，省利息，省保险，还有新购补贴，欢迎咨询",
    "宝子点击私信商家卡片，报价单明细和更多优惠福利都给您安排哦",
    "点击卡片查看澎程最新报价单，补贴分期表",
    "后台领取数字：1、SU7报价单，2、YU7报价单，3、澎程报价单以及试驾预约",
    # 竞争对手官方号（特斯拉）—— 它们也在别人的直播间发广告
    "各地的限时补贴不同，点击右下角小风车，根据您的实际情况了解免费置换评估",
    "需要精准报价和补贴情况的朋友们点击小风车 预约完成后 产品顾问线上帮大家做详解",
]


def test_store_ads_are_excluded_by_content():
    """门店广告脚本必须在**内容层面**被排除，不能只靠账号身份"""
    sc = _scorer()
    leaked = []
    for t in STORE_ADS:
        r = sc.score(t, "某观众", set())
        if not r.excluded:
            leaked.append((t, r.level, r.score, r.signals))
    assert not leaked, f"门店广告没被内容防线挡住：{leaked}"


def test_media_agency_promo_is_excluded():
    """传媒代运营号发的政策推广（实测库里真实存在）要挡住

    「置换，是不是可以去绵阳提车领6000哦」—— 看着像问句，
    但它来自传媒号（张氏传媒），是在**替门店发政策**。
    判据是「送具体金额」（领6000）—— 真人客户不会这么写。
    """
    sc = _scorer()
    r = sc.score("置换，是不是可以去绵阳提车领6000哦", "张氏传媒", set())
    assert r.excluded, "传媒号的政策推广没被排除"
    assert r.score == 0


def test_store_ad_scores_are_not_promoted_to_leads():
    """就算没被排除，门店广告的分数也绝不能到 high（72 分那种）"""
    sc = _scorer()
    for t in STORE_ADS:
        r = sc.score(t, "某观众", set())
        assert r.excluded, f"这句没被排除：{t}"
        assert r.score == 0


# ---------------------------------------------------------------- 反向：真客户不误伤

REAL_CUSTOMERS = [
    "私信我", "报价单发我", "0利息有吗", "首付10万够吗",
    "落地价，裸车价多少", "首付、分期哪个划算", "这个多少钱，有现车吗",
    "置换能抵多少", "利息多少", "月供多少", "我能抵2万吗",
    "分期利息高吗", "现车吗", "首付多少合适", "免息还是分期划算",
    "提车要多久", "0息和免息一样吗", "有现车吗", "多少钱",
    # 「是不是」「能不能」也是客户常用的问句形式 —— 不能因为
    # 「置换」「提车」都在方案词表里就把人家当广告排掉
    "置换是不是能抵3万", "是不是有0利息", "能不能便宜点",
    "首付是不是要10万", "这个车是不是能马上提",
]


def test_real_customers_are_not_excluded():
    """**真客户绝不能被误伤** —— 误伤就是丢线索"""
    sc = _scorer()
    wrong = []
    for t in REAL_CUSTOMERS:
        r = sc.score(t, "客户", set())
        if r.excluded:
            wrong.append((t, r.exclude_reason))
    assert not wrong, f"真客户被误排除：{wrong}"


def test_customer_questions_with_is_not_excluded():
    """「…是不是…」这种问句是客户常用形式，必须保留"""
    sc = _scorer()
    for t in ["置换是不是能抵3万", "是不是有0利息", "首付是不是要10万"]:
        r = sc.score(t, "客户", set())
        assert not r.excluded, f"客户问句被当成广告排掉了：{t}"
        assert r.score > 0, f"客户问句被判 0 分：{t}"


def test_real_customers_still_score():
    """真客户还得有分（不能只保证没被排除，还得有分）"""
    sc = _scorer()
    zero = []
    for t in REAL_CUSTOMERS:
        r = sc.score(t, "客户", set())
        if r.score <= 0:
            zero.append(t)
    assert not zero, f"这些真客户判 0 分：{zero}"


# ---------------------------------------------------------------- 无关闲聊

CHATTER = ["好", "2", "90", "真的假的", "在吗", "哈哈哈", "来了", "666",
           "主播好", "1", "3", "87", "吵到我的耳朵了"]


def test_chatter_stays_zero():
    """无关闲聊必须仍然是 0 分（别把打分器变成噪音放大器）"""
    sc = _scorer()
    noisy = []
    for t in CHATTER:
        r = sc.score(t, "客户", set())
        if r.score > 0 and not r.excluded:
            noisy.append((t, r.score, r.signals))
    assert not noisy, f"闲聊被判成线索：{noisy}"


# ---------------------------------------------------------------- 昵称归一化

def test_normalize_name_strips_badge_suffix():
    """配置名带「认证徽章」、弹幕名不带 → 归一化后要能对上"""
    a = _normalize_name("小米汽车-成都金牛凯德体验店 认证徽章")
    b = _normalize_name("小米汽车-成都金牛凯德体验店")
    assert a == b, f"归一化后仍不相等：{a!r} vs {b!r}"


def test_normalize_name_handles_both_vertical_bars():
    """配置用「丨」(U+4E28)、弹幕用「｜」(U+FF5C) —— 字形不同也要能对上"""
    a = _normalize_name("小米汽车丨成都万象城店")
    b = _normalize_name("小米汽车｜成都万象城店")
    assert a == b, f"两种竖线归一化后仍不相等：{a!r} vs {b!r}"


def test_self_names_include_badge_variant():
    """自家账号集合里要能同时命中「带徽章」和「不带徽章」两种写法"""
    from src.config import get_config

    names = _build_self_names(get_config())
    assert _normalize_name("小米汽车-成都金牛凯德体验店") in names
    assert _normalize_name("小米汽车-成都金牛凯德体验店 认证徽章") in names
