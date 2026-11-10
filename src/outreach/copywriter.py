"""生成留言文案。

原则：
1. 短。抖音评论区刷屏，超过两行没人看，60 字以内。
2. 具体。能提到车型/他关心的问题，就比通用话术像真人。
3. 不提联系方式。评论区留微信号 = 主动送人头，引导动作留给私信和回复。
4. 可解释。每条留言都记下来用的是哪个模板、为什么选它，方便复盘。
"""
from __future__ import annotations

import hashlib
import pathlib
import random
import re
from typing import Any, Optional

import yaml

from ..intelligence.rule_scorer import is_aftersale_context

_DEFAULT_TPL = {
    "salutation_default": "老板",
    "car_alias": {},
    "car_patterns": [],
    "templates": [{"id": "generic", "keywords": [], "text": "{salutation}，看到你在直播间咨询小米汽车，可以找我了解下价格对比下～"}],
    "forbidden_words": [],
}

# 话术库默认放 src/outreach/templates.yaml
_TEMPLATES_PATH = pathlib.Path(__file__).parent / "templates.yaml"

# ============================================================
# 规则信号 → 话术角度
# ============================================================
# 话术角度必须和规则引擎的信号用同一套口径，
# 否则会出现「规则判的是价格，话术回的是配置」这种答非所问。
#
# 「联系方式」故意不在这张表里：评论区出现微信/电话是限流封号红线，
# 命中这个信号就直接跳过，不映射任何角度。
_SIGNAL_TEMPLATE_MAP: dict[str, str] = {
    "价格询问": "price",
    "提车交付": "delivery",
    "金融补贴": "price",
    "门店到店": "testdrive",
    "购买流程": "delivery",
    "产品参数": "config",
    "版本选择": "version",
    "库存颜色": "config",
    "时间节点": "delivery",
    "外观内饰": "config",
    "泛车提及": "model",
    "车型关注": "model",
}

# 「车型关注」不在 rule_scorer.SIGNAL_CATEGORIES 里
# （它只在「只提车型、没有任何购买行为」时动态产生），单独给个弱信号权重。
_SIGNAL_EXTRA_WEIGHT: dict[str, int] = {"车型关注": 6}


def _signal_weights() -> dict[str, int]:
    """信号 → 权重（从规则引擎实时取，别写死）"""
    try:
        from ..intelligence.rule_scorer import SIGNAL_CATEGORIES

        weights = {name: w for name, w, _ in SIGNAL_CATEGORIES}
    except Exception:
        weights = {}
    weights.update(_SIGNAL_EXTRA_WEIGHT)
    return weights


def templates_path(cfg: dict) -> pathlib.Path:
    """话术库路径

    优先用配置里指定的（相对路径按项目根解析），
    否则用打包在 src/outreach/templates.yaml 的默认话术库。
    """
    rel = ((cfg or {}).get("comment") or {}).get("templates_file")
    if rel:
        p = pathlib.Path(str(rel)).expanduser()
        if not p.is_absolute():
            p = pathlib.Path(__file__).resolve().parents[2] / p
        if p.exists():
            return p
    return _TEMPLATES_PATH


class Copywriter:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.tpl: dict = {}
        self.reload()

    def reload(self) -> None:
        path = templates_path(self.cfg)
        data: dict = {}
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as e:  # 话术文件写错了不许把整轮跑挂掉
                self.log(f"⚠️ templates.yaml 解析失败，用内置兜底：{e}")
        merged = dict(_DEFAULT_TPL)
        merged.update({k: v for k, v in data.items() if v is not None})
        self.tpl = merged
        self._alias = {str(k).strip().upper(): str(v) for k, v in (merged.get("car_alias") or {}).items()}
        self._patterns = [(str(p[0]).lower(), str(p[1])) for p in (merged.get("car_patterns") or []) if p]
        self._templates = [t for t in (merged.get("templates") or []) if t.get("text")]
        self._forbidden = [str(w) for w in (merged.get("forbidden_words") or [])]

    # ------------------------------------------------------------------ 车型
    def resolve_car(self, lead: dict) -> str:
        """确定话术里说的车型

        优先用规则引擎的 extract_model——
        它认得「su7max」「N70 Max」这类完整写法；
        而纯子串匹配会把「N70 Max」里的 max 当成 SU7 Max，
        客户问 N70 我们回 SU7 Max，那叫**车型说错**。
        """
        text = f"{lead.get('danmaku') or ''} {lead.get('evidence') or ''}"

        model = ""
        try:
            from ..intelligence.rule_scorer import RuleScorer

            model = str(RuleScorer.extract_model(text) or "")
        except Exception:
            model = ""

        raw = model.strip().upper()
        if raw and raw in self._alias:
            return self._alias[raw]

        # 规则引擎没认出来 → 看表里的「车型」字段
        field = str(lead.get("car_model") or "").strip().upper()
        if field and field in self._alias:
            return self._alias[field]

        # 再退到话术库里的子串匹配
        low = text.lower()
        for pat, name in self._patterns:
            if pat and pat in low:
                return name

        return raw or field or "小米汽车"

    # ------------------------------------------------------------------ 选模板
    def _pick_template(self, lead: dict, car: str) -> dict:
        """按弹幕原文选一个角度

        ⚠️ 别用「车型明确 → price 角度」的兜底：
        客户只说了「su7」（没问价格），回「看到你问SU7的价格」是**编造**，
        对方点进主页看到会觉得莫名其妙。
        车型明确但没问具体问题时，走「车型关注」角度——具体但不撒谎。

        优先用**规则引擎判出的信号**来选角度
        ------------------------------------
        规则引擎（rule_scorer）和话术模板原来是两套独立的词表，
        靠「模板按固定顺序、谁先命中用谁」来对齐，结果会错位：
        客户同时问了价格和到店地址，模板顺序是固定的，
        选出来的角度不一定是他主要关心的那个。

        改成直接用规则引擎的信号（按信号权重排序）来选：
        两边天然用同一套口径，角度一定对得上客户真正问的东西。
        """
        danmaku = str(lead.get("danmaku") or "") + " " + str(lead.get("evidence") or "")

        # 1) 先看规则引擎判了什么信号
        tpl = self._pick_by_signals(danmaku)
        if tpl is not None:
            return tpl

        # 2) 信号里没有能映射到模板的 → 回退到关键词匹配
        for t in self._templates:
            kws = [str(k) for k in (t.get("keywords") or [])]
            if not kws:
                continue
            if not any(k and k in danmaku for k in kws):
                continue
            # 价格角度但整句在售后语境里 → 不选这个角度
            # 例："修车要多少钱"——回"给你报个落地价"是答非所问。
            # 规则层（rule_scorer）已经排除了这种误判，话术层必须同步排除，
            # 否则规则不把它当购车问价、话术却照样回落地价。
            if t.get("id") == "price" and is_aftersale_context(danmaku):
                continue
            return t
        # 弹幕里没具体问题：车型明确走「车型关注」，否则走通用
        fallback_id = "model" if (car and car != "小米汽车") else "generic"
        for t in self._templates:
            if t.get("id") == fallback_id:
                return t
        for t in self._templates:
            if not (t.get("keywords")):
                return t
        return self._templates[0]

    def _pick_by_signals(self, danmaku: str) -> Optional[dict]:
        """用规则引擎的信号选角度（信号按权重从高到低）

        「联系方式」信号不映射任何模板——评论区不能出现微信/电话，
        那是平台限流封号的红线，命中就直接跳过这个信号。
        """
        if not danmaku.strip():
            return None
        try:
            from ..intelligence.rule_scorer import RuleScorer

            result = RuleScorer().score(danmaku)
        except Exception:
            return None
        if not result.signals:
            return None

        ids = {t.get("id"): t for t in self._templates}
        weights = _signal_weights()
        # 按规则引擎的权重从高到低，
        # 权重表从 rule_scorer 实时取——规则那边改了权重，
        # 这里不用同步改一遍（以前是写死的顺序，会悄悄失序）。
        order = sorted(_SIGNAL_TEMPLATE_MAP, key=lambda s: -weights.get(s, 0))
        for signal in order:
            if signal not in result.signals:
                continue
            tpl_id = _SIGNAL_TEMPLATE_MAP[signal]
            tpl = ids.get(tpl_id)
            if tpl is None:
                continue
            # 售后语境里的「金融补贴」信号不映射到价格角度——
            # 客户问的是「修车有优惠吗」，回「给你报个落地价」是答非所问。
            # 规则层只排除了「价格询问」，「金融补贴」漏过去了，
            # 话术层必须自己兜住（关键词匹配路径也有同样的守卫）。
            if tpl_id == "price" and is_aftersale_context(danmaku):
                continue
            return tpl
        return None

    @staticmethod
    def _pick_variant(tpl: dict, seed: str = "") -> tuple[str, str]:
        """从模板里挑一个说法变体

        为什么需要变体：
        同一个模板只用一句话，给几百个客户发一模一样的评论，
        平台很容易判定为批量 spam。

        为什么用「抖音号做种子」而不是纯随机：
        - 纯随机的话，同一个人两次预览得到不同文案，没法复盘
        - 用种子后，同一个抖音号**永远**得到同一句话，
          不同人之间又自然分散开

        Returns:
            (文本, 变体标识)
        """
        variants = [str(v) for v in (tpl.get("variants") or []) if v]
        base = str(tpl.get("text", ""))
        if not variants:
            return base, ""
        pool = ([base] if base else []) + variants
        if seed:
            digest = hashlib.md5(str(seed).encode("utf-8")).digest()
            # 用 4 字节而不是单字节取模：
            # 单字节 % N 在小样本下偏差很明显（实测 215 条样本里，
            # 某个组合出现 15 次、另一个只有 2 次），
            # 多字节取模能消掉这个系统性偏差。
            idx = int.from_bytes(digest[:4], "big") % len(pool)
        else:
            idx = random.randrange(len(pool))
        return pool[idx], ("v" if idx > 0 else "")

    def _pick_salutation(self, seed: str) -> str:
        """挑一个称呼

        模板变体 × 称呼变体 = 组合数相乘。
        实测待触达池里 price 角度有 100+ 条，只靠模板变体必然大量重复；
        加上 3 个称呼后同样的内容能散开好几倍。

        称呼用摘要的另一段字节（4~8），和选变体那段互不相关。
        """
        pool = [s for s in (self.cfg.get("comment", {}).get("salutation_pool") or []) if s]
        if not pool:
            return str(self.cfg.get("comment", {}).get("salutation") or "老板")
        if seed:
            digest = hashlib.md5(str(seed).encode("utf-8")).digest()
            return pool[int.from_bytes(digest[4:8], "big") % len(pool)]
        return random.choice(pool)

    def _truncate(self, text: str, limit: int) -> str:
        """截断到指定长度

        ⚠️ 补「～」时必须留出位置：cut[:i] 已经是 i 个字符，
        再拼一个「～」就是 limit+1 字，超了。
        """
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        # 尽量断在标点处，别把「价格对比下」切成「价格对比」
        for sep in ("～", "，", "。", "！", " "):
            i = cut.rfind(sep)
            if i >= limit * 0.5:
                body = cut[:i]
                tail = "" if sep == "～" else "～"
                # 补尾巴后仍不能超限
                if len(body) + len(tail) > limit:
                    body = body[: limit - len(tail)]
                return body + tail
        body = cut.rstrip("，,、 ")
        if len(body) + 1 > limit:
            body = body[: limit - 1]
        return body + "～"

    # ------------------------------------------------------------------ 主入口
    def _limit(self, channel: str) -> int:
        """评论区 60 字够用；私信给到 120 字，一次说清来意。"""
        if channel == "dm":
            return int(self.cfg.get("dm", {}).get("max_chars") or 120)
        return int(self.cfg.get("comment", {}).get("max_chars") or 60)

    def generate(self, lead: dict, channel: str = "comment") -> dict:
        """→ {text, template_id, car, ok, reason, channel}"""
        car = self.resolve_car(lead)
        tpl = self._pick_template(lead, car)
        seed = str(lead.get("douyin_id") or "")
        salutation = self._pick_salutation(seed)
        raw = tpl.get("dm_text") if channel == "dm" else None
        raw = str(raw or tpl.get("text", ""))

        # 评论区用变体机制降重复率；私信只有一次机会，用原句
        variant_tag = ""
        if channel != "dm":
            raw, variant_tag = self._pick_variant(tpl, seed)
            raw = raw or str(tpl.get("text", ""))

        text = raw.format(
            salutation=salutation,
            car=car,
            store=str(lead.get("store") or "门店"),
        )
        text = self._truncate(text, self._limit(channel))

        hit = [w for w in self._forbidden if w and w in text]
        if hit:
            return {"ok": False, "reason": f"文案含敏感引导词：{'/'.join(hit)}", "text": text,
                    "template_id": tpl.get("id"), "car": car, "channel": channel}

        return {"ok": True, "reason": "", "text": text,
                "template_id": f"{tpl.get('id')}{variant_tag}",
                "car": car, "channel": channel}

    # 供看板预览：给出多种角度，让运营自己挑
    def variants(self, lead: dict, n: int = 3) -> list[dict]:
        out, seen = [], set()
        car = self.resolve_car(lead)
        salutation = str(self.cfg.get("comment", {}).get("salutation") or "老板")
        limit = int(self.cfg.get("comment", {}).get("max_chars") or 60)
        for t in self._templates:
            txt = self._truncate(str(t.get("text", "")).format(
                salutation=salutation, car=car, store=str(lead.get("store") or "门店")), limit)
            if txt in seen:
                continue
            if any(w and w in txt for w in self._forbidden):
                continue
            seen.add(txt)
            out.append({"template_id": t.get("id"), "text": txt})
            if len(out) >= n:
                break
        return out
