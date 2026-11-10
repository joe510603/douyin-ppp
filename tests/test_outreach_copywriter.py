"""触达模块测试 — 话术生成、门店映射、模板选择

重点覆盖两个容易出错的逻辑：
1. 模板选择必须基于**弹幕原文**，不能基于「判定依据」——
   否则客户只说了「su7」却收到「看到你问价格」，是编造。
2. 变体选择必须**确定性**（同一个抖音号永远同一句话），
   否则没法复盘，且两次预览不一致。
"""

import collections

import pytest

from src.outreach.copywriter import Copywriter, templates_path
from src.outreach.worker import lead_to_copywriter_input


def _cfg(**over):
    base = {
        "comment": {
            "max_chars": 60,
            "salutation": "老板",
            "salutation_pool": ["老板", "这位朋友", "你好"],
            "templates_file": "",
            "allow_contact_words": False,
        },
        "dm": {"max_chars": 120},
    }
    base["comment"].update(over.get("comment", {}))
    return base


def _lead(danmaku="", douyin_id="abc", car_model="", **over):
    d = {
        "douyin_id": douyin_id,
        "nickname": "小明",
        "car_model": car_model,
        "store": "世茂广场店",
        "intent": "high",
        "score": 80,
        "danmaku": danmaku,
        "evidence": "",
        "live_room": "小米汽车｜世茂广场店",
    }
    d.update(over)
    return d


# ============================================================
# 话术库
# ============================================================

def test_templates_path_default_exists():
    p = templates_path({})
    assert p.exists()
    assert p.name == "templates.yaml"


def test_templates_have_model_angle():
    """必须有「车型关注」角度：客户只提车型没问问题时用它，不编造"""
    import yaml

    data = yaml.safe_load(templates_path({}).read_text(encoding="utf-8"))
    ids = [t["id"] for t in data["templates"]]
    assert "model" in ids


def test_templates_forbidden_words_present():
    """违禁词必须包含联系方式类词"""
    import yaml

    data = yaml.safe_load(templates_path({}).read_text(encoding="utf-8"))
    words = data.get("forbidden_words") or []
    assert "微信" in words
    assert "电话" in words


# ============================================================
# 模板选择
# ============================================================

def test_price_keywords_pick_price_template():
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="落地多少钱"))
    assert r["template_id"].startswith("price")


def test_delivery_keywords_pick_delivery_template():
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="N90多久能提车"))
    assert r["template_id"].startswith("delivery")


def test_testdrive_keywords_pick_testdrive_template():
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="想试驾"))
    assert r["template_id"].startswith("testdrive")


def test_model_only_picks_model_not_price():
    """客户只说了车型 → 走「车型关注」角度，不能说「看到你问价格」

    注意断言怎么写：model 模板说「我这边能帮你对比下价格和配置」是**提供信息**，
    不是编造客户问了价格。真正的雷区是「看到你在直播间问…的价格」这种
    把没发生过的事说成发生过。
    """
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="su7", car_model="SU7"))
    assert r["template_id"].startswith("model")
    assert "问" not in r["text"] or "问过" not in r["text"]
    # 不能出现「问…价格」这种编造句式
    assert "问SU7的价格" not in r["text"]
    assert "你在直播间问" not in r["text"]


def test_no_info_picks_generic():
    """什么都没说 → 通用角度"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku=""))
    assert r["template_id"].startswith("generic")


def test_car_alias_translated():
    """内部代号要翻译成用户听得懂的说法"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="落地多少", car_model="MAX"))
    assert "SU7 Max" in r["text"]


def test_internal_code_car_falls_back_to_generic_brand():
    """N90 这种内部代号 → 翻译成「小米汽车」，不暴露内部代号"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="落地多少", car_model="N90"))
    assert "N90" not in r["text"]
    assert "小米汽车" in r["text"]


# ============================================================
# 变体：确定性与分散度
# ============================================================

def test_variant_is_deterministic_for_same_douyin_id():
    """同一个抖音号两次生成必须完全一样（否则没法复盘）"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    a = cw.generate(_lead(danmaku="落地多少", douyin_id="user001"))
    b = cw.generate(_lead(danmaku="落地多少", douyin_id="user001"))
    assert a["text"] == b["text"]
    assert a["template_id"] == b["template_id"]


def test_variants_spread_across_douyin_ids():
    """不同抖音号应该散到不同说法上（防平台判 spam）"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    texts = {
        cw.generate(_lead(danmaku="落地多少", douyin_id=f"u{i:03d}"))["text"]
        for i in range(40)
    }
    assert len(texts) >= 5, f"40 个不同抖音号只产生 {len(texts)} 种话术，太集中"


def test_salutation_pool_used():
    """称呼变体应该生效：同角度不同人称呼不同"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    sals = set()
    for i in range(30):
        r = cw.generate(_lead(danmaku="落地多少", douyin_id=f"u{i:03d}"))
        for s in ("老板", "这位朋友", "你好"):
            if r["text"].startswith(s):
                sals.add(s)
                break
    assert len(sals) >= 2, f"称呼没分散开，只有 {sals}"


def test_no_salutation_pool_uses_configured():
    """没配称呼变体池时，只用配置里的那一个"""
    cfg = _cfg(comment={"salutation_pool": []})
    cw = Copywriter(cfg, log=lambda m: None)
    r = cw.generate(_lead(danmaku="落地多少"))
    assert r["text"].startswith("老板")


# ============================================================
# 安全：违禁词与长度
# ============================================================

def test_forbidden_word_blocks():
    """文案含违禁词要判不合格"""
    cw = Copywriter(_cfg(), log=lambda m: None)
    r = cw.generate(_lead(danmaku="加微信"))
    # 「加微信」本身会命中 contact 触发词，但文案模板里没有联系方式字眼
    # 所以关键是：模板产出的文案不能含违禁词
    assert r["ok"] is True
    assert "微信" not in r["text"]


def test_text_truncated_to_max_chars():
    """超长要截断"""
    cfg = _cfg(comment={"max_chars": 20})
    cw = Copywriter(cfg, log=lambda m: None)
    r = cw.generate(_lead(danmaku="落地多少钱"))
    assert len(r["text"]) <= 20


# ============================================================
# leads 行 → copywriter 输入
# ============================================================

def test_lead_to_copywriter_input_uses_evidence_not_reason():
    """模板选择必须用弹幕原文（evidence），不是判定依据（intent_reason）

    intent_reason 是「命中信号：价格询问、金融补贴」这种汇总，
    拿它选模板会答非所问。
    """
    lead = {
        "display_id": "u1",
        "nickname": "小明",
        "car_model": "SU7",
        "store": "世茂广场店",
        "intent_level": "high",
        "intent_score": 51,
        "intent_reason": "命中信号：提车交付、版本选择",
        "evidence": '["n70max有没有现车啊"]',
        "monitor_name": "小米汽车｜乐山龙湖星悦荟体验店",
    }
    out = lead_to_copywriter_input(lead)
    assert "n70max有没有现车啊" in out["danmaku"]
    assert "命中信号" not in out["danmaku"]


def test_lead_to_copywriter_input_handles_bad_json():
    """evidence 坏掉也不能抛异常"""
    lead = {"display_id": "u1", "evidence": "[不是合法 JSON"}
    out = lead_to_copywriter_input(lead)
    assert isinstance(out["danmaku"], str)


def test_lead_to_copywriter_input_empty_evidence():
    lead = {"display_id": "u1", "evidence": ""}
    out = lead_to_copywriter_input(lead)
    assert out["danmaku"] == ""


def test_问价位走价格角度():
    """「su7max现在什么价位了」是真实问价，必须用 price 角度

    词表没对齐的话会落到车型兜底，回「看到你在看SU7」——回避了客户问的价格。
    """
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="su7max现在什么价位了", car_model="SU7"))
    assert out["template_id"].startswith("price")


def test_问修车多少钱不用价格角度():
    """问剐蹭修复费不是问买车价，不能回「给你报个落地价」"""
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(
        danmaku="我把别人的小米su7max挂蹭了3个小地方 修的花大概是要多少钱",
        car_model="SU7",
    ))
    assert not out["template_id"].startswith("price")
    assert "落地价" not in out["text"]


def test_信号决定话术角度():
    """模板选择要用规则引擎判出的信号，不是自己再匹配一遍关键词

    两套词表各匹配各的会错位：客户同时问了价格和到店地址，
    模板顺序是固定的，选出来的角度不一定是他主要关心的。
    """
    cw = Copywriter(_cfg())

    # 问价格 → price
    out = cw.generate(_lead(danmaku="su7落地多少钱", car_model="SU7"))
    assert out["template_id"].startswith("price")

    # 问提车 → delivery
    out = cw.generate(_lead(danmaku="su7现在有现车吗，多久能提车", car_model="SU7"))
    assert out["template_id"].startswith("delivery")

    # 问版本 → version（原来只能落到车型兜底，回「看到你在关注SU7」，答非所问）
    out = cw.generate(_lead(danmaku="su7 max和pro哪个好，差多少", car_model="SU7"))
    assert out["template_id"].startswith("version")

    # 问配置 → config
    out = cw.generate(_lead(danmaku="珍珠白有吗，轮毂能选吗", car_model="SU7"))
    assert out["template_id"].startswith("config")

    # 只提车型 → model（不能编造他问了价格）
    out = cw.generate(_lead(danmaku="su7", car_model="SU7"))
    assert out["template_id"].startswith("model")
    assert "多少钱" not in out["text"]
    assert "落地价" not in out["text"]


def test_多信号时按信号权重选角度():
    """价格 + 到店 → price（价格询问权重 40 > 门店到店 30）"""
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="su7多少钱，你们店在哪", car_model="SU7"))
    assert out["template_id"].startswith("price")


def test_联系方式信号不产生联系方式话术():
    """问联系方式的线索，评论区话术不能出现微信/电话

    评论区留联系方式是平台限流封号的红线。
    """
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="加个微信聊", car_model="SU7"))
    text = out["text"]
    for w in ("微信", "电话", "手机号", "加我"):
        assert w not in text


# ============================================================
# 回归：信号路径要和关键词路径一样守住售后语境
# ============================================================

def test_售后语境不走价格角度():
    """「修车有优惠吗」——问的是修车优惠，不是买车优惠

    回归记录：信号路径（_pick_by_signals）先跑，
    「金融补贴」信号命中后直接映射到 price 模板，
    没走关键词路径里的售后守卫 → 回「给你报个落地价」，答非所问。
    """
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="修车有优惠吗", car_model="SU7"))
    assert not out["template_id"].startswith("price")
    assert "落地价" not in out["text"]


def test_售后语境不走价格角度_问价版本():
    """同一句里既有优惠又提到修理，也不能被当成购车问价"""
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="我车刮蹭了，修车有优惠吗", car_model="SU7"))
    assert not out["template_id"].startswith("price")
    assert "落地价" not in out["text"]


# ============================================================
# 回归：车型不能认错
# ============================================================

def test_车型解析不串型号():
    """客户问 N70 Max，不能回「SU7 Max」——那是车型说错

    回归记录：话术库用子串匹配车_patterns，
    「N70 Max」里的 max 先被匹配到 → 说成 SU7 Max。
    """
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="N70 Max能讲解一下吗", car_model="N70"))
    assert "SU7 Max" not in out["text"]
    assert "N70" not in out["text"]


def test_点名车型走车型关注角度():
    """「展车有pro吗」是点名那台车，不是问选哪个版本"""
    cw = Copywriter(_cfg())
    out = cw.generate(_lead(danmaku="展车有pro吗", car_model="PRO"))
    assert out["template_id"].startswith("model")
    assert "版本" not in out["text"]
