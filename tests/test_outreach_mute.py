"""触达静音 —— 打开作品时抖音会直接放视频带声音

用户反馈：「要关闭网页的声音，避免打开作品放声音」

两层防线
--------
1. **JS 强制静音**（`browser._MUTE_AUDIO`）—— 保证没声音。
   拦网络请求管不住 MediaSource / blob / 缓存里的音频，
   所以静音必须由 JS 兜底：改掉 `play()` 让它先静音再播，
   再用 MutationObserver 盯着后续插入的媒体元素（抖音是无限流，元素一直在换）。
2. **网络层拦媒体**（`outreach.block_media`）—— 省流量省 CPU。
   这一层是可选的：实测拦掉之后作品页评论区照常渲染
   （见 `scripts/check_comment_box_media.py`），但静音不依赖它。

这里只测「代码层面装没装对」；真浏览器效果用
`.venv/bin/python scripts/check_comment_box_media.py` 验。
"""
from __future__ import annotations

from src.config import get_config
from src.outreach.browser import _MUTE_AUDIO


def test_静音脚本存在且覆盖三种情况():
    s = _MUTE_AUDIO
    # 1. play() 之前先静音
    assert "HTMLMediaElement.prototype.play" in s
    assert "muted = true" in s
    # 2. 已经在页面上的媒体元素
    assert "querySelectorAll('video,audio')" in s
    # 3. 后续新插入的元素（无限流一直在换）
    assert "MutationObserver" in s


def test_静音脚本不依赖任何外部资源():
    """init script 是注入到页面里的，不能有 fetch/import 之类的东西"""
    for bad in ("fetch(", "import ", "require(", "XMLHttpRequest"):
        assert bad not in _MUTE_AUDIO, bad


def test_默认拦媒体():
    cfg = get_config()
    assert cfg.outreach.block_media is True, "拦媒体默认开（省流量，实测不影响评论区）"


def test_拦媒体开关可关():
    """静音不依赖这一项，关掉也照样静音"""
    cfg = get_config()
    cfg.outreach.block_media = False
    assert cfg.outreach.block_media is False
    assert "HTMLMediaElement.prototype.play" in _MUTE_AUDIO
