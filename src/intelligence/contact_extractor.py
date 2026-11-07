"""联系方式提取器 — 从弹幕原文提取手机号 / 微信号 / QQ 号

设计原则：**宁漏勿误**。
- 手机号：靠正则的强特征（1 开头 11 位），误报率低，可直接提取
- 微信号 / QQ：必须先有上下文触发词，否则不提取
  （否则订单号、房间号、"已售 30 台"这类数字会被误判）

提取结果不依赖 LLM，纯本地规则，零成本、零延迟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ============================================================
# 手机号
# ============================================================

# 在原文上匹配（容忍 138-1234-5678 / 138 1234 5678 这类写法）
_MOBILE_LOOSE_RE = re.compile(
    r"(?<!\d)(1[3-9]\d(?:[\s\-_·•]?\d){8})(?!\d)"
)

# 带区号的固定电话（028-87654321 / 02887654321），只在明确像电话时收
_LANDLINE_RE = re.compile(r"(?<!\d)(0\d{2,3}-?\d{7,8})(?!\d)")

# ============================================================
# 微信号（需上下文触发）
# ============================================================

_WECHAT_TRIGGER = (
    r"(?:加\s*[vV]|加\s*[微威薇]|微信|威信|薇信|薇信|微\s*信|威|vx|VX|Vx|wX|WX|"
    r"v\s*信|微\s*号|威\s*号|私\s*我|私信\s*我|扣\s*[扣Qq]|扣扣|q\s*q|企鹅)"
)
# 微信号本体：字母开头，允许字母数字下划线减号，5-20 位
_WECHAT_RE = re.compile(
    _WECHAT_TRIGGER + r"\s*(?:号|码)?\s*[:：=]?\s*([A-Za-z][-_A-Za-z0-9]{4,19})(?![-_A-Za-z0-9])"
)

# ============================================================
# QQ 号（需上下文触发）
# ============================================================

_QQ_RE = re.compile(r"(?:QQ|qq|Qq|qQ)\s*(?:号|号码)?\s*[:：=]?\s*(\d{5,11})(?!\d)")

# ============================================================
# 其他线索词（可能带线索但非联系方式）
# ============================================================

# 明显的广告/同行引流，标记为低价值
_SPAM_MARKERS = (
    "加微信了解", "私域", "引流", "代运营", "起号", "涨粉", "培训", "课程",
)

# 号码前面的抖音号语境——命中则该数字是抖音号，不是联系方式
# 教训来源：autoengine-monitor 曾把「用户4151082501973」里的
# 「15108250197」误判成电话号码。这里做同样的语境排除。
_DOUYIN_ID_CONTEXT = re.compile(
    r"(?:抖音号|抖音\s*号|我的号|账号|号码是|号是|号:|号：)\s*[:：]?\s*$"
)


def _looks_like_douyin_id(text: str, position: int) -> bool:
    """判断号码位置之前是否是抖音号语境"""
    prefix = text[max(0, position - 20):position]
    return bool(_DOUYIN_ID_CONTEXT.search(prefix))


@dataclass
class ContactInfo:
    """从一条（或多条）弹幕中提取到的联系方式"""

    phone: str = ""
    wechat: str = ""
    qq: str = ""
    raw: list[str] = field(default_factory=list)
    is_spam: bool = False

    @property
    def has_contact(self) -> bool:
        return bool(self.phone or self.wechat or self.qq)

    @property
    def contact_type(self) -> str:
        """phone / wechat / qq / phone+wechat ..."""
        parts = []
        if self.phone:
            parts.append("phone")
        if self.wechat:
            parts.append("wechat")
        if self.qq:
            parts.append("qq")
        return "+".join(parts)

    @property
    def value(self) -> str:
        """人类可读的联系方式摘要"""
        parts = []
        if self.phone:
            parts.append(f"电话 {self.phone}")
        if self.wechat:
            parts.append(f"微信 {self.wechat}")
        if self.qq:
            parts.append(f"QQ {self.qq}")
        return " / ".join(parts)


def extract_contact(text: str) -> ContactInfo:
    """
    从单条弹幕文本提取联系方式。

    Args:
        text: 弹幕原文

    Returns:
        ContactInfo —— 没提取到时所有字段为空
    """
    info = ContactInfo()
    if not text:
        return info

    lowered = text.lower()
    if any(marker in lowered for marker in _SPAM_MARKERS):
        info.is_spam = True

    # --- 手机号 ---
    # 在原文上匹配（保留位置信息，便于判断抖音号语境），容忍分隔符
    for m in _MOBILE_LOOSE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) != 11:
            continue
        # 号码前面是「抖音号:」之类 → 这是抖音号不是电话
        if _looks_like_douyin_id(text, m.start()):
            continue
        info.phone = digits
        info.raw.append(digits)
        break

    # --- 固定电话（仅当没有手机号时，避免误吞）---
    if not info.phone:
        m = _LANDLINE_RE.search(text)
        if m:
            info.phone = m.group(1)
            info.raw.append(m.group(1))

    # --- 微信号 ---
    m = _WECHAT_RE.search(text)
    if m:
        candidate = m.group(1)
        # 排除 "vx 13800138000" 这类误抓（纯数字不是微信号形态）
        if not candidate.isdigit():
            info.wechat = candidate
            info.raw.append(candidate)

    # --- QQ ---
    m = _QQ_RE.search(text)
    if m:
        info.qq = m.group(1)
        info.raw.append(m.group(1))

    return info


def extract_contacts(texts: list[str]) -> ContactInfo:
    """
    从多条弹幕合并提取联系方式。

    手机号优先取第一个；微信/QQ 同样取第一个命中。
    若其中任一条被判定为广告引流，整体标记 is_spam。
    """
    merged = ContactInfo()
    for text in texts or []:
        info = extract_contact(text)
        if info.is_spam:
            merged.is_spam = True
        if not merged.phone and info.phone:
            merged.phone = info.phone
        if not merged.wechat and info.wechat:
            merged.wechat = info.wechat
        if not merged.qq and info.qq:
            merged.qq = info.qq
        for r in info.raw:
            if r not in merged.raw:
                merged.raw.append(r)
        if merged.has_contact and merged.is_spam:
            # 已经有联系方式且判定为广告，直接返回
            return merged
    return merged
