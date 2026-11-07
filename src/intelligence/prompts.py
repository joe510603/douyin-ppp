"""LLM 提示词模板 — 购车意向判定 / 留言话术生成

所有提示词都要求「只输出 JSON」，并在调用侧做容错解析。
"""

from __future__ import annotations

import json

# ============================================================
# 购车意向判定
# ============================================================

INTENT_SYSTEM_PROMPT = (
    "你是一位汽车经销商的直播运营助理，正在分析抖音汽车直播间的弹幕，"
    "目标是识别出有真实购车意向的潜在客户。你只输出 JSON，不做任何额外解释。"
)

INTENT_ANALYZE_PROMPT = """以下是抖音汽车直播间里 {count} 位观众的弹幕内容。请逐个判断他们有没有购车意向。

【意向等级标准】
- high（高意向）：明确要买车、问价格/落地价/优惠、问何时提车、问能否试驾、问到店地址、问有没有现车、问怎么订车、主动留联系方式、说要到店看车
- medium（中意向）：对具体车型感兴趣、比较车型、问配置/续航/油耗/空间/尺寸、说"挺喜欢""在考虑""多少钱"（泛问价格）、问优惠政策
- low（低意向）：只是看热闹、跟风感叹、与主播闲聊、泛泛提问但和买车无关、和其他观众互动
- none（无意向）：完全与购车无关（问主播吃饭、闲聊、刷表情、问视频剪辑、如何关注等）

【判定规则】
1. 只依据弹幕原文判断，不要脑补用户背景
2. 同一用户有多条弹幕时，综合全部弹幕整体判断，取最高意向
3. 若弹幕中出现手机号、微信号、QQ 号，填进 contact 字段（原文照抄），没有就留空字符串
4. car_model 填用户明确提到的车型（如"小米SU7""问M3""想看YU7"），没提到就留空字符串
5. evidence 填触发你做出该判断的那几条弹幕原文，最多 3 条，必须是原文
6. reason 用一句话说明为什么判成这个等级，要具体，引用弹幕里的关键词
7. intent_score 打 0-100 分，与等级大致对应（high 70-100，medium 40-69，low 10-39，none 0-9）

【观众弹幕】
{users}

严格只输出 JSON 数组，不要 markdown 代码块，不要任何解释文字。
数组每个元素结构：
{{"user_id":"原始 user_id","display_id":"原始抖音号","nickname":"原始昵称","intent_level":"high|medium|low|none","intent_score":0-100,"reason":"一句话依据","car_model":"车型或空字符串","contact":"联系方式或空字符串","evidence":["弹幕原文"]}}
"""


def build_intent_prompt(users: list[dict]) -> str:
    """
    构造购车意向分析提示词。

    Args:
        users: [{"user_id","display_id","nickname","messages":[...]}, ...]
    """
    blocks = []
    for u in users:
        msgs = u.get("messages") or []
        # 单条消息最多保留 200 字，整组最多 12 条，控制 token
        trimmed = [m[:200] for m in msgs[:12]]
        blocks.append(
            f'- user_id={u.get("user_id", "")} '
            f'display_id={u.get("display_id", "")} '
            f'nickname={u.get("nickname", "")}\n'
            + "\n".join(f"    弹幕: {m}" for m in trimmed)
        )
    users_block = "\n".join(blocks)
    return INTENT_ANALYZE_PROMPT.format(count=len(users), users=users_block)


# ============================================================
# 留言话术生成（第二步）
# ============================================================

MESSAGE_SYSTEM_PROMPT = (
    "你是一位汽车门店的短视频运营，正在为抖音用户写作品评论。"
    "你只输出 JSON，不做任何额外解释。"
)

MESSAGE_PROMPT = """请为下面这位抖音用户生成一条作品评论，目标是礼貌地引导其到店看车。

【用户信息】
昵称：{nickname}
抖音号：{display_id}
他在直播间说过的内容：{context}
意向等级：{intent_level}
关注的车型：{car_model}

【要求】
1. 语气自然、口语化，像真人在评论区说话，**不要**像广告
2. 长度 15-40 字，最多 2 句
3. 可以呼应他在直播间的发言，显得是真人
4. 不要出现"加微信""私信我""扫码"这类引流词
5. 不要承诺价格、优惠、赠送等具体销售政策（避免虚假宣传）
6. 不要编造对方的姓名、年龄、职业

严格只输出 JSON：
{{"message":"评论内容","reason":"为什么这么写"}}
"""


def build_message_prompt(
    nickname: str,
    display_id: str,
    context: str,
    intent_level: str,
    car_model: str,
) -> str:
    """构造留言话术生成提示词"""
    return MESSAGE_PROMPT.format(
        nickname=nickname or "这位朋友",
        display_id=display_id or "",
        context=context[:400] or "（无具体发言记录）",
        intent_level=intent_level,
        car_model=car_model or "我们店里的车型",
    )


# ============================================================
# 解析工具
# ============================================================

def parse_json_array(raw: str) -> list[dict]:
    """
    从 LLM 回复里解析 JSON 数组，容错处理 markdown 包裹与前后缀文字。

    Args:
        raw: LLM 原始回复文本

    Returns:
        解析出的字典列表；解析失败返回空列表
    """
    if not raw:
        return []

    text = raw.strip()

    # 去掉 ```json ... ``` 包裹
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("["):
                text = candidate
                break

    # 直接尝试解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass

    # 截取第一个 [ 到最后一个 ] 之间再试
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass

    return []


def parse_json_object(raw: str) -> dict:
    """从 LLM 回复里解析单个 JSON 对象（容错）"""
    items = parse_json_array(raw)
    return items[0] if items else {}
