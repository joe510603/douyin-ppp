"""规则购车意向打分引擎 — 纯本地规则，零成本零延迟

为什么先做规则而不是 LLM：
1. 直播间的购车信号非常规整（"落地多少" / "全款多少" / "N90多久可以提车" / "店在哪里"），
   关键词 + 权重就能覆盖绝大多数真客户
2. 规则是**实时**的：弹幕一来就判定，不存在批量扫描的时间窗延迟，短命直播间也不漏
3. 零成本、零延迟、不受 API 配额和网络影响
4. 判定依据可追溯（命中了哪些词），比 LLM 的一句话更可审计

LLM 作为可选的第二层：只对规则筛出的少量候选做精排（语义细分、车型归一、话术生成），
一天几十条的量，4B 本地模型完全够用。

打分模型：按「信号类别」计分，同一类别内命中多个词也只加一次分，
避免"多少钱多少钱"把分数刷高。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..models.lead import IntentLevel

# ============================================================
# 信号类别表
# 格式：(类别名, 权重, 正则)
# 权重越高代表离「掏钱买车」越近
# ============================================================

SIGNAL_CATEGORIES: list[tuple[str, int, str]] = [
    # ---- 强购买动作 ----
    ("联系方式", 45,
     r"电话|微信|加\s*[vV]|加\s*[微威薇]|v\s*信|微信号|联系我|留个|私我|私信我|扣\s*[qQ]|QQ"),
    ("价格询问", 40,
     r"多少钱|多少米|什么价|落地|全款|裸车|车价|报价|成交价|优惠价|贵不贵|便宜"),
    ("提车交付", 35,
     r"提车|交付|订车|下单|现车|有货|有现车|库存|排产|定金|订了|什么时候能|多久能提|现不现"),
    ("金融补贴", 32,
     r"免息|分期|贷款|首付|置换|补贴|置换补|置换政策|权益|优惠|折扣|降价|以旧换新"),
    ("门店到店", 30,
     r"试驾|到店|来店|店在哪|地址|具体位置|门店在哪|哪里提车|展厅|体验店|你们在哪|在哪儿"),
    ("购买流程", 24,
     r"怎么买|在哪买|上牌|保险|购置税|开票|合同|订金|怎么订|怎么购买"),

    # ---- 中等信号 ----
    ("产品参数", 18,
     r"配置|参数|续航|电池|充电|能跑多少|马力|功率|油耗|空间|座椅|后备箱|悬架|刹车|"
     r"智驾|辅助驾驶|空调|音响|后备|天窗|轮毂|尺寸|轴距"),
    ("版本选择", 16,
     r"什么版本|有哪些版本|哪个版本|标准版|max|pro|ultra|有什么区别|区别|对比|相比|哪个好"),
    ("库存颜色", 14,
     r"有粉色|有紫色|有白色|有黑色|有红色|生产吗|还生产|现不现货|颜色|配色"),
    ("时间节点", 12,
     r"多久|什么时候|什么时候能|几天|下周|当天|能提车吗|能到店吗"),

    # ---- 弱信号 ----
    ("外观内饰", 8,
     r"好看|漂亮|颜值|内饰|外观|颜色|设计|酷|帅"),
    ("泛车提及", 6,
     r"这个车|这车|这款|这型号|车子|你们这个"),
]

# 编译成一个大正则，按类别分组捕获，避免逐词扫描
_COMPILED_CATEGORIES: list[tuple[str, int, re.Pattern]] = [
    (name, weight, re.compile(pattern, re.IGNORECASE))
    for name, weight, pattern in SIGNAL_CATEGORIES
]

# ============================================================
# 排除规则
# ============================================================

# 同行/引流
_SPAM_RE = re.compile(
    r"加\s*微\s*信\s*了\s*解|私域|引流|代运营|起号|涨粉|培训|加群|私我拿|招代理|加盟|课程|代理招募",
    re.IGNORECASE,
)

# 门店营销/引流话术模板（不管哪家门店的，自动回复都会混进弹幕池）
# 例："需要精准报价和补贴情况的朋友们点击小风车"、"后台私信您…报价单"
_CTA_RE = re.compile(
    r"点击\s*小风车|小风车|点击下方|点击下方|后台私信您|点击预约|私信获取|加\s*[vV]\s*领|"
    r"扫码|欢迎咨询|产品顾问线上|专属权益报价单|限时专属权益|"
    r"私信小助理|一对一对接|找小助理|进私信|咨询小助理|找产品顾问",
    re.IGNORECASE,
)

# 店员在直播间回复客户：通常以 @昵称 开头 + 报价/引导话术
# 例："@纷乱人生 不同车型全款与贷款优惠不一样，进私信小助理一对一对接"
_AT_REPLY_RE = re.compile(r"^@\S+\s")

# 营销模板特征词：一条消息里同时出现 ≥2 个，几乎必然是自动回复而非真人提问
_MARKETING_TERMS = (
    "最新", "优惠", "福利", "报价", "补贴", "省利息", "省保险", "权益",
    "欢迎咨询", "产品顾问", "小风车", "新车", "上市", "限时", "专属",
)
_MARKETING_RE = re.compile("|".join(_MARKETING_TERMS), re.IGNORECASE)

# 售后/维权（不是买车意向）
_COMPLAINT_RE = re.compile(
    r"投诉|维权|退款|骗人|欺骗|差评|售后(不|太差|很差)|质量(问题|太差)|欺骗消费者",
    re.IGNORECASE,
)

# 已经是车主（老客户，价值另算）
_OWNER_RE = re.compile(
    r"我的\s*(su7|yu7|n70|n90|车)|我(提|买|开)(了|的)|已提车|已经买了|我的新车|提了车",
    re.IGNORECASE,
)

# 车评/媒体/同行车主（专业内容，不是潜在买家）
_REVIEWER_RE = re.compile(
    r"测评|评测|车评|试驾感受|实拍|参数对比表|科普|做功课|对比一下各",
    re.IGNORECASE,
)

# 售后语境：问的是「修车/事故/理赔」的钱，不是「买车」的钱
# 例："我把别人的小米su7max挂蹭了3个小地方 修的花大概是要多少钱"
#     —— 命中了「多少钱」，但这是问剐蹭修复费，不是问购车落地价。
#     不排除的话话术会回「看到你问SU7多少钱，这个可以给你报个落地价」，
#     答非所问，还可能被当成骚扰。
# 只收明确的损伤/维修词，「质保几年」「保养多少钱」这类仍属购车问题，不能排除。
_AFTERSALE_CONTEXT_RE = re.compile(
    r"刮蹭|剐蹭|蹭(了|到)?|修车|维修|修理|钣金|钣喷|喷漆|定损|理赔|"
    r"事故|碰撞|追尾|换(了)?(配件|零件|轮胎|电池|后视镜|保险)|掉漆|凹了|变形",
    re.IGNORECASE,
)


def is_aftersale_context(text: str) -> bool:
    """这句话问的是「修车/事故/理赔」的钱，不是「买车」的钱

    话术层（copywriter）也要用这个判断——
    否则规则层不把「修车多少钱」算成购车问价了，话术模板却照样匹配上
    「多少钱」然后回「可以给你报个落地价」，照样答非所问。
    """
    return bool(text) and bool(_AFTERSALE_CONTEXT_RE.search(text))


# 某些信号在特定语境下要作废：类别名 → 语境正则
_SIGNAL_CONTEXT_EXCLUDE: dict[str, re.Pattern] = {
    "价格询问": _AFTERSALE_CONTEXT_RE,
}

# ============================================================
# 车型识别
# ============================================================

CAR_MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"su\s*7\s*ultra", "SU7 Ultra"),
    (r"su\s*7", "SU7"),
    (r"yu\s*7", "YU7"),
    # 注意：不能用 \b —— 汉字属于 \w，"N90多久"里的 \b 不成立
    # 改用 ASCII 字符的负向断言，中英文混排都能命中
    (r"(?<![a-z0-9])n\s*70(?![a-z0-9])", "N70"),
    (r"(?<![a-z0-9])n\s*90(?![a-z0-9])", "N90"),
    (r"(?<![a-z0-9])mi\s*x(?![a-z0-9])", "MIX"),
    (r"标准版", "标准版"),
    (r"(?<![a-z0-9])max(?![a-z0-9])", "MAX"),
    (r"(?<![a-z0-9])pro(?![a-z0-9])", "PRO"),
    (r"(?<![a-z0-9])ultra(?![a-z0-9])", "Ultra"),
]

_COMPILED_MODELS: list[tuple[str, re.Pattern]] = [
    (name, re.compile(pattern, re.IGNORECASE)) for pattern, name in CAR_MODEL_PATTERNS
]

# ============================================================
# 阈值
# ============================================================

HIGH_THRESHOLD = 55
MEDIUM_THRESHOLD = 30
LOW_THRESHOLD = 12
# 只提到车型、没有任何购买行为的分数（低于 LOW_THRESHOLD 也仍算命中线索）
MODEL_ONLY_SCORE = 8


@dataclass
class ScoreResult:
    """一条弹幕的打分结果"""

    score: int = 0
    level: str = IntentLevel.NONE.value
    signals: list[str] = field(default_factory=list)
    car_model: str = ""
    excluded: bool = False
    exclude_reason: str = ""

    @property
    def is_lead(self) -> bool:
        """是否值得作为线索"""
        return not self.excluded and self.level != IntentLevel.NONE.value

    @property
    def reason(self) -> str:
        """人类可读的判定依据"""
        if self.excluded:
            return f"已排除：{self.exclude_reason}"
        if not self.signals:
            return "无购车相关信号"
        return "命中信号：" + "、".join(self.signals)


class RuleScorer:
    """规则购车意向打分器"""

    def __init__(self, min_level: str = "low", exclude_aftersale: bool = True):
        self.min_level = min_level
        # 售后语境排除开关。
        # 关掉它就等于回到 v2.0.14 之前的口径——
        # 给「重打分」脚本用：只有开关前后结果不同的线索才是规则变更影响的，
        # 不会把「判定口径不同」误当成「规则变更」。
        self.exclude_aftersale = exclude_aftersale

    def score(self, text: str, nickname: str = "", self_names: Optional[set] = None) -> ScoreResult:
        """
        给一条弹幕打分。

        Args:
            text: 弹幕原文
            nickname: 用户昵称（用于排除自家账号）
            self_names: 自家账号昵称集合（归一化后）

        Returns:
            ScoreResult
        """
        if not text:
            return ScoreResult()

        content = text.strip()

        # ---- 排除 ----
        if self_names and nickname:
            from .intent_analyzer import _normalize_name
            if _normalize_name(nickname) in self_names:
                return ScoreResult(excluded=True, exclude_reason="自家账号")

        if _SPAM_RE.search(content):
            return ScoreResult(excluded=True, exclude_reason="同行/引流")
        if _CTA_RE.search(content):
            return ScoreResult(excluded=True, exclude_reason="门店营销话术")
        # 一条消息里出现 ≥2 个营销特征词 → 判定为自动回复模板
        if len(_MARKETING_RE.findall(content)) >= 2:
            return ScoreResult(excluded=True, exclude_reason="营销模板")
        if _COMPLAINT_RE.search(content):
            return ScoreResult(excluded=True, exclude_reason="售后/维权")
        if _OWNER_RE.search(content):
            return ScoreResult(excluded=True, exclude_reason="已是车主")
        if _REVIEWER_RE.search(content):
            return ScoreResult(excluded=True, exclude_reason="车评/专业内容")

        # ---- 打分 ----
        model = self.extract_model(content)
        score = 0
        signals: list[str] = []
        for name, weight, pattern in _COMPILED_CATEGORIES:
            if not pattern.search(content):
                continue
            # 语境排除：信号命中了，但整句落在不该算这个信号的语境里
            # （例：问「修车多少钱」不算购车「价格询问」）
            ctx_re = _SIGNAL_CONTEXT_EXCLUDE.get(name)
            if (
                ctx_re is not None
                and self.exclude_aftersale
                and ctx_re.search(content)
            ):
                continue
            score += weight
            signals.append(name)

        # 只提车型、没有任何购买行为（如"看一下YU7"）→ 记为弱信号
        # 这种情况在直播间很常见，是典型的"对某款车感兴趣但还没问价"
        model_only = False
        if not signals and model:
            score = MODEL_ONLY_SCORE
            signals.append("车型关注")
            model_only = True

        if not signals:
            return ScoreResult(score=0, level=IntentLevel.NONE.value)

        level = IntentLevel.LOW.value if model_only else self._to_level(score)

        # 提到具体车型但没有任何购买行为 → 降到 low（只是好奇）
        if model and score < MEDIUM_THRESHOLD and level != IntentLevel.NONE.value:
            level = IntentLevel.LOW.value

        # @某人 + 购买信号 → 是店员在回复客户，不是客户本人
        if _AT_REPLY_RE.match(content):
            return ScoreResult(excluded=True, exclude_reason="店员回复")

        return ScoreResult(
            score=score,
            level=level,
            signals=signals,
            car_model=model,
        )

    @staticmethod
    def _to_level(score: int) -> str:
        if score >= HIGH_THRESHOLD:
            return IntentLevel.HIGH.value
        if score >= MEDIUM_THRESHOLD:
            return IntentLevel.MEDIUM.value
        if score >= LOW_THRESHOLD:
            return IntentLevel.LOW.value
        return IntentLevel.NONE.value

    @staticmethod
    def extract_model(content: str) -> str:
        """从文本里提取车型"""
        for name, pattern in _COMPILED_MODELS:
            if pattern.search(content):
                return name
        return ""

    def meets_min_level(self, result: ScoreResult) -> bool:
        """是否达到配置的最低等级"""
        order = {
            IntentLevel.NONE.value: 0,
            IntentLevel.LOW.value: 1,
            IntentLevel.MEDIUM.value: 2,
            IntentLevel.HIGH.value: 3,
        }
        return order.get(result.level, 0) >= order.get(self.min_level, 1)


_scorer: Optional[RuleScorer] = None


def get_scorer(min_level: str = "low") -> RuleScorer:
    """获取全局打分器"""
    global _scorer
    if _scorer is None:
        _scorer = RuleScorer(min_level=min_level)
    return _scorer
