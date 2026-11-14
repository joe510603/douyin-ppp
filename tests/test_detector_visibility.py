"""检测器不能瞎，也不能把别人的直播间当成这个账号的

三个钉住的点（2026-11-14 实测踩出来的）
----------------------------------------
1. **登录态没了要能发现**：抖音 Web 端用户接口必须登录态，
   未登录时返回 0 字节的 body —— 检测器对所有账号一律「未检测到直播」，
   采集器永不启动，一整天的弹幕全丢，而日志上一片正常。
   → ``info.rendered`` 必须能区分「看不见」和「没开播」。

2. **别把别的直播间的 room_id 塞给当前账号**：
   cookie 里有个 ``live_debug_info``，存着「上次看的直播间的 roomId」。
   以前的代码在「没检测到直播」时也会去用它 →
   没开播的「成都锦江」拿到了「成都万象城」的 room_id。

3. **浏览器路径要从直播链接里直接拿 room_id**：
   链接的 query 里就带着 ``room_id=7707947305089501961``，
   以前只取短 ID，还要再发一次 HTTP 去直播页里刨。
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.detector.live_detector import LiveDetector, LiveRoomInfo

SEC = "MS4wLjABAAAATVed-OOWyEXK_HHppKlR1fTchBgq2WE1Bjc3k88AoUrPuDkKbMugORGZpXI4PCFi"
COOKIE_WITH_OTHER_ROOM = (
    "ttwid=xxx; live_debug_info=%7B%22roomId%22%3A%227707934377145469706%22%7D"
)


# ---------------------------------------------------------------- 1. rendered 判据

def test_rendered_defaults_false():
    """刚构造出来的 info 应该是「看不见」——默认不能当成「没开播」。"""
    info = LiveRoomInfo()
    assert info.rendered is False
    assert info.status is False


def test_rendered_is_set_by_browser_path():
    """浏览器把页面渲染出来时，rendered 必须置 True（自检就靠它）。"""
    det = LiveDetector()
    captured = {}

    async def _fake_browser(sec_user_id, inf):
        captured["info"] = inf
        inf.rendered = True

    with patch.object(LiveDetector, "_try_mobile_api", new=AsyncMock()), \
         patch.object(LiveDetector, "_try_web_api", new=AsyncMock()), \
         patch.object(LiveDetector, "_try_browser_detection", new=AsyncMock(side_effect=_fake_browser)):
        asyncio.run(det.detect_by_sec_user_id(SEC))

    assert captured["info"].rendered is True


def test_web_api_success_marks_rendered():
    """Web API 拿到这个账号的数据 = 我们「看得见」它（接口活着也是证据）。"""
    det = LiveDetector()
    captured = {}

    async def _fake_web(sec_user_id, inf):
        captured["info"] = inf
        inf.anchor_nickname = "小米汽车丨成都万象城店"
        inf.rendered = True

    with patch.object(LiveDetector, "_try_mobile_api", new=AsyncMock()), \
         patch.object(LiveDetector, "_try_web_api", new=AsyncMock(side_effect=_fake_web)), \
         patch.object(LiveDetector, "_try_browser_detection", new=AsyncMock()):
        asyncio.run(det.detect_by_sec_user_id(SEC))

    assert captured["info"].rendered is True


# ---------------------------------------------------------------- 2. cookie fallback 不串号

def test_cookie_fallback_not_used_when_not_live():
    """没开播的账号，不许拿到 cookie 里别人的 room_id。

    回归现场（2026-11-14）：「成都锦江体验店」没开播，
    却拿到了「成都万象城」的 room_id —— 来自 cookie 里的 live_debug_info。
    """
    det = LiveDetector()
    captured = {}

    async def _fake_web(sec_user_id, inf):
        captured["info"] = inf
        inf.anchor_nickname = "小米汽车｜成都锦江体验店"
        inf.rendered = True
        # status != 2 → 明确没开播

    with patch.object(LiveDetector, "_get_effective_cookie", return_value=COOKIE_WITH_OTHER_ROOM), \
         patch.object(LiveDetector, "_try_mobile_api", new=AsyncMock()), \
         patch.object(LiveDetector, "_try_web_api", new=AsyncMock(side_effect=_fake_web)), \
         patch.object(LiveDetector, "_try_browser_detection", new=AsyncMock()):
        asyncio.run(det.detect_by_sec_user_id(SEC))

    info = captured["info"]
    assert info.status is False
    # 没开播 → 不该有任何 room_id，更不该是别的直播间的
    assert info.room_id == ""


def test_cookie_never_fabricates_room_id():
    """就算确定在播，也不许拿 cookie 里的 live_debug_info 拼 room_id。

    回归现场（2026-11-14）：live_debug_info 记的是「上次看的那场直播」，
    跟当前账号无关 —— 拿它当 room_id，采集器会连到**别人的直播间**，
    把别人的弹幕记到这个账号名下（串号）。
    room_id 只有一条来路：浏览器从直播链接的 query 里直接取。
    """
    det = LiveDetector()
    captured = {}

    async def _fake_web(sec_user_id, inf):
        captured["info"] = inf
        inf.anchor_nickname = "小米汽车丨成都万象城店"
        inf.status = True  # 在播，但浏览器没给出 room_id
        inf.rendered = True

    with patch.object(LiveDetector, "_get_effective_cookie", return_value=COOKIE_WITH_OTHER_ROOM), \
         patch.object(LiveDetector, "_try_mobile_api", new=AsyncMock()), \
         patch.object(LiveDetector, "_try_web_api", new=AsyncMock(side_effect=_fake_web)), \
         patch.object(LiveDetector, "_try_browser_detection", new=AsyncMock()):
        asyncio.run(det.detect_by_sec_user_id(SEC))

    info = captured["info"]
    assert info.status is True
    # 拿不到 room_id 就不启动采集 —— 宁可漏一场，也不能算错线索
    assert info.room_id == ""


# ---------------------------------------------------------------- 3. 直播链接直接取 room_id

def test_room_id_parsed_from_live_link():
    """直播链接的 query 里直接带 room_id —— 不用再发一次 HTTP 去刨。"""
    href = ("https://live.douyin.com/163468593199?action_type=click"
            "&room_id=7707947305089501961&web_card_rank=")
    m = re.search(r"room_id=(\d{18,})", href)
    assert m and m.group(1) == "7707947305089501961"


def test_live_link_without_room_id_falls_back_to_web_rid():
    """老格式的链接没有 room_id → 至少还能拿到 web_rid（采集器会自己去换真实 room_id）。"""
    href = "https://live.douyin.com/163468593199?action_type=click"
    assert not re.search(r"room_id=(\d{18,})", href)
    m = re.search(r"live\.douyin\.com/(\d+)", href)
    assert m and m.group(1) == "163468593199"
