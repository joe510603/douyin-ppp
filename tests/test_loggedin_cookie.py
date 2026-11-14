"""登录态要能续命，profile 要能复制

背景（2026-11-14）
------------------
检测器靠抖音 Web 端用户接口判开播，那接口**必须登录态**：

    未登录 cookie → HTTP 200 但 body **0 字节**（什么都拿不到）
    登录态 cookie → 25KB，含 room_data.status=2

登录态会过期。一过期，检测器对所有账号一律「未检测到直播」→
采集器永不启动 → **一整天的弹幕全丢**，而日志上一片正常。

两个机制要钉住：
1. ``seed_profile`` 只拷必要文件，但**必须把 cookie 带上**
   （cookie 是用 Edge 的钥匙串密钥加密的，副本也得用 Edge 打开）
2. ``LoggedInCookieProvider`` 读到没有 sessionid 的 cookie 时**不能采用**
   （那说明 profile 掉登录了，用它反而会让检测器拿不到房间）
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.utils.loggedin_cookie import LoggedInCookieProvider
from src.utils.profile_seed import SeededProfiles, seed_profile


def _make_source_profile(root: pathlib.Path) -> pathlib.Path:
    """造一个「像真的」的源 profile（只需要那几个关键文件）。"""
    src = root / "outreach_profile"
    (src / "Default" / "Network").mkdir(parents=True, exist_ok=True)
    (src / "Local State").write_text('{"os_crypt":{"encrypted_key":"xxx"}}')
    (src / "Default" / "Cookies").write_bytes(b"fake-cookies-db")
    (src / "Default" / "Preferences").write_text("{}")
    (src / "Default" / "Network" / "Cookies").write_bytes(b"fake-network-cookies-db")
    (src / "Default" / "Local Storage").mkdir(parents=True, exist_ok=True)
    (src / "Default" / "Local Storage" / "leveldb").mkdir(exist_ok=True)
    (src / "Default" / "Local Storage" / "leveldb" / "000001.log").write_bytes(b"lsdata")
    return src


def test_seed_profile_copies_login_state(tmp_path):
    """复制后必须带上 cookie —— 少了它页面就会降级成壳页。"""
    src = _make_source_profile(tmp_path)
    dst = tmp_path / "slot0"

    assert seed_profile(src, dst) is True
    assert (dst / "Default" / "Cookies").exists()
    assert (dst / "Default" / "Network" / "Cookies").exists()
    assert (dst / "Default" / "Local Storage" / "leveldb" / "000001.log").exists()
    assert (dst / "Local State").exists()


def test_seed_profile_fails_without_cookie(tmp_path):
    """源 profile 里没有 cookie → 明确失败（别让程序拿着空壳去检测）。"""
    src = tmp_path / "empty"
    src.mkdir()
    (src / "Preferences").write_text("{}")

    assert seed_profile(src, tmp_path / "dst") is False


def test_seed_profile_missing_source(tmp_path):
    """源目录不存在 → 失败而不是抛异常。"""
    assert seed_profile(tmp_path / "nope", tmp_path / "dst") is False


def test_seeded_profiles_rotates_slots(tmp_path):
    """并发检测要一人一份 profile（同一个目录开第二个浏览器会被锁）。"""
    src = _make_source_profile(tmp_path)
    provider = SeededProfiles(str(src), str(tmp_path / "slots"), slots=3, refresh_minutes=60)

    dirs = [provider.next_dir() for _ in range(6)]
    assert all(d is not None for d in dirs)
    assert len({str(d) for d in dirs}) == 3          # 3 个 slot 轮着用
    assert (tmp_path / "slots" / "slot0" / "Default" / "Cookies").exists()


def test_provider_rejects_cookie_without_sessionid(tmp_path):
    """读到没有 sessionid 的 cookie → 不采用（说明 profile 掉登录了）。"""
    provider = LoggedInCookieProvider(str(tmp_path / "nope"), str(tmp_path / "slots"), ttl_minutes=60)

    with patch.object(LoggedInCookieProvider, "_harvest", return_value="ttwid=abc; odin_tt=xyz"):
        assert provider.refresh(force=True) == ""
    assert provider.get() == ""


def test_provider_keeps_old_cookie_when_harvest_fails(tmp_path):
    """这次读失败，但之前有好的 → 保留旧的，别把能用的也丢了。"""
    provider = LoggedInCookieProvider(str(tmp_path / "nope"), str(tmp_path / "slots"), ttl_minutes=60)

    with patch.object(LoggedInCookieProvider, "_harvest", return_value="ttwid=abc; sessionid=S"):
        provider.refresh(force=True)
        assert "sessionid=S" in provider.get()

    with patch.object(LoggedInCookieProvider, "_harvest", return_value=""):
        provider.refresh(force=True)
        assert "sessionid=S" in provider.get()


def test_provider_excludes_room_specific_cookie():
    """live_debug_info 是**房间相关**的，带上它会把别的直播间的 room_id 串给当前账号。"""
    from src.utils.loggedin_cookie import _EXCLUDE_NAMES
    assert "live_debug_info" in _EXCLUDE_NAMES
