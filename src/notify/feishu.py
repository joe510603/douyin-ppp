"""飞书群机器人通知器

只依赖飞书群机器人的 webhook，不依赖企业自建应用，
因此不需要 app_id / app_secret，接入成本最低。

支持：
- 文本消息
- 交互式卡片（线索摘要用卡片更清晰）
- 签名校验（群机器人「签名校验」开启时需要 secret）

安全：webhook URL 与 secret 属于凭据，日志中一律脱敏。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

import httpx

from ..utils.logger import get_logger

log = get_logger("feishu_notifier")

# 意向等级 → 卡片颜色
LEVEL_COLORS = {
    "high": "red",
    "medium": "orange",
    "low": "blue",
    "none": "grey",
}

LEVEL_LABELS = {
    "high": "高意向",
    "medium": "中意向",
    "low": "低意向",
    "none": "无意向",
}


def _mask(url: str) -> str:
    """把 webhook URL 脱敏，只保留末尾 6 位"""
    if not url:
        return "(未配置)"
    tail = url[-6:]
    return f"***{tail}"


class FeishuNotifier:
    """飞书群机器人通知器"""

    def __init__(self, webhook_url: Optional[str] = None, secret: Optional[str] = None,
                 timeout: int = 15):
        self.webhook_url = (webhook_url or "").strip()
        self.secret = (secret or "").strip()
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def _sign(self) -> tuple[str, str]:
        """
        飞书签名校验。

        官方算法：string_to_sign = "{timestamp}\\n{secret}"，
        以它为 key、空消息为 content 做 HMAC-SHA256，再 base64。
        """
        ts = str(int(time.time()))
        h = hmac.new(f"{ts}\n{self.secret}".encode("utf-8"), digestmod=hashlib.sha256).digest()
        return ts, base64.b64encode(h).decode("utf-8")

    def _build_payload(self, body: dict) -> dict:
        """按需附加签名字段"""
        if self.secret:
            ts, sign = self._sign()
            body = {**body, "timestamp": ts, "sign": sign}
        return body

    async def send(self, body: dict) -> tuple[bool, str]:
        """
        发送一条 webhook 消息。

        Returns:
            (是否成功, 错误信息)
        """
        if not self.is_configured:
            return False, "未配置飞书 webhook"

        payload = self._build_payload(body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if resp.status_code != 200:
                    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
                # 飞书返回 code 0 表示成功；自定义机器人部分场景无 code 字段
                code = data.get("code")
                if code is not None and code != 0:
                    return False, f"code={code} msg={data.get('msg', '')}"
                return True, ""
        except httpx.TimeoutException:
            log.error(f"飞书推送超时: {_mask(self.webhook_url)}")
            return False, "请求超时"
        except Exception as e:
            log.error(f"飞书推送失败: {_mask(self.webhook_url)} — {e}")
            return False, str(e)

    async def send_text(self, text: str) -> tuple[bool, str]:
        """发送纯文本消息"""
        return await self.send({"msg_type": "text", "content": {"text": text}})

    async def send_card(self, card: dict) -> tuple[bool, str]:
        """发送交互式卡片"""
        return await self.send({"msg_type": "interactive", "card": card})

    async def send_risk_card(
        self,
        title: str,
        reason: str = "",
        steps: str = "",
        at_open_id: str = "",
        who: str = "",
    ) -> tuple[bool, str]:
        """发一张「触达出事了」的告警卡片（红色 + @人）

        为什么用卡片而不是纯文本
        ----------------------
        1. **群里会推**：飞书私聊会话**不产生手机推送**（实测消息发得出去，
           但人在手机上根本不知道），群消息才会推。
        2. **@ 人才是真提醒**：`<at user_id="ou_xxx">` 会触发飞书的强提醒
           （红点 + 横幅），只发一条普通群消息很容易被刷过去。
        3. 卡片有颜色和分区：红头 + 原因 + 怎么办，一眼能看清发生了什么。

        Args:
            title: 卡片标题（已经带好 emoji）
            reason: 触发原因（平台的原话）
            steps: 怎么办（分步文字）
            at_open_id: 要 @ 的人的 open_id，留空就不 @
            who: 卡在哪条线索上（可选）
        """
        if not self.is_configured:
            return False, "未配置飞书 webhook"

        elements: list[dict] = []
        head = []
        if who:
            head.append(f"**卡在**：{who}")
        if reason:
            head.append(f"**原因**：{reason}")
        if head:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(head)},
            })
        if steps:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**怎么办**\n{steps}"},
            })

        # @ 人放在最后一行：飞书的强提醒是跟 @ 绑定的
        if at_open_id:
            elements.append({"tag": "hr"})
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f'<at user_id="{at_open_id}">请处理</at> '
                               f"—— 触达已经停下来了，等你验证完再继续",
                },
            })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements or [
                {"tag": "div", "text": {"tag": "lark_md", "content": "无内容"}}
            ],
        }
        return await self.send_card(card)

    async def push_leads(
        self,
        leads: list[dict],
        title: str = "新购车线索",
    ) -> tuple[bool, str]:
        """
        推送线索摘要到飞书群。

        Args:
            leads: 线索字典列表（来自 leads 表）
            title: 卡片标题

        Returns:
            (是否成功, 错误信息)
        """
        if not leads:
            return False, "没有待推送的线索"
        if not self.is_configured:
            return False, "未配置飞书 webhook"

        elements: list[dict] = []
        for lead in leads[:20]:
            level = lead.get("intent_level") or "none"
            color = LEVEL_COLORS.get(level, "grey")
            label = LEVEL_LABELS.get(level, level)

            name = lead.get("nickname") or "匿名用户"
            did = lead.get("display_id") or "无抖音号"
            score = lead.get("intent_score") or 0
            model = lead.get("car_model") or ""
            contact = lead.get("contact_value") or ""
            reason = (lead.get("intent_reason") or "").strip()
            room = lead.get("monitor_name") or lead.get("live_room") or ""

            lines = [
                f"**{name}**  ·  `{label}` ({score}分)",
                f"抖音号: `{did}`" + (f"  |  车型: **{model}**" if model else ""),
            ]
            if contact:
                lines.append(f"📞 **联系方式: {contact}**")
            if reason:
                lines.append(f"💬 {reason}")
            if room:
                lines.append(f"📍 来源: {room}")

            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            })
            elements.append({"tag": "hr"})

        if len(leads) > 20:
            elements.append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"另有 {len(leads) - 20} 条线索未展示"}],
            })

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "green",
                "title": {"tag": "plain_text", "content": f"🚗 {title} · {len(leads)} 条"},
            },
            "elements": elements or [{"tag": "div", "text": {"tag": "lark_md", "content": "无内容"}}],
        }
        return await self.send_card(card)


def get_notifier(config=None) -> FeishuNotifier:
    """按配置构造通知器"""
    if config is None:
        from ..config import get_config
        config = get_config()
    return FeishuNotifier(
        webhook_url=config.notify.webhook_url,
        secret=config.notify.secret,
    )
