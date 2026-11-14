"""把「真浏览器」的登录态复制给检测器用。

为什么不能只注入 cookie（2026-11-14 实测）
---------------------------------------
抖音判的是**整个浏览器环境**，不是某一个 cookie。实测三组对照：

  Edge + 全新 profile + 注入登录态 cookie   → 页面只剩 4 个字符（空白壳）
  Edge + outreach_profile 的最小副本        → 页面正常渲染，还带直播间入口

差别不在 cookie 本身（副本里本来就是同一份 cookie），
而在 profile 里的 localStorage / 设备指纹 / 站点状态这些**累积出来的痕迹**。
只搬 cookie 会被当成异常环境，页面降级成壳页。

为什么这事这么严重
------------------
降级后的壳页里**没有直播间入口**，于是：
  「这个账号没开播」和「我们看不见这个账号」变成同一件事。
检测器报「未检测到直播」→ 不启动采集 → **整场直播的弹幕全丢**，
而且日志上一片正常，看不出任何异常（2026-11-14 下午就是这样丢了一整天的）。

所以这里把「登录态」当成必须保住的东西，不是可选优化。

复制哪些文件
------------
只复制**必要的那几项**（实测总共 ~900KB，不用拷 2GB 的整个 profile）：

  Local State              Edge 用它定位 cookie 的加密密钥（配合 Keychain）
  Default/Cookies          登录态本体（sessionid 等）
  Default/Preferences      站点偏好
  Default/Network/Cookies  新版 Chromium 的 cookie 路径
  Default/Local Storage    抖音的风控/设备状态 ← 少了它页面就会降级
  Default/Session Storage

⚠ cookie 是用**系统钥匙串**里的密钥加密的（macOS 上 Edge 的
"Microsoft Edge Safe Storage"）。所以**必须用 Edge 打开这个副本**才解得开，
拿 Playwright 自带的 Chromium 打开只会看到一堆解不开的密文。
"""

from __future__ import annotations

import pathlib
import shutil
import time
from typing import Optional

from .logger import get_logger

log = get_logger("profile_seed")

# 复制这个清单就够了（其余是缓存/历史记录，又大又没用）
SEED_FILES = (
    "Local State",
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Preferences",
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Local Storage",
    "Default/Session Storage",
)

# 目录名里带这些片段的，整棵目录都拷（Local Storage 是目录）
SEED_DIRS = ("Default/Local Storage", "Default/Session Storage")


def seed_profile(src: pathlib.Path | str, dst: pathlib.Path | str) -> bool:
    """把 src profile 的最小必要文件复制到 dst。

    Args:
        src: 源 profile 目录（比如 data/outreach_profile）
        dst: 目标目录（比如 data/detect_profiles/slot0）

    Returns:
        是否复制成功（源目录不存在或没有 cookie 都算失败）
    """
    src = pathlib.Path(src).expanduser()
    dst = pathlib.Path(dst).expanduser()
    if not src.is_dir():
        log.warning(f"profile 源目录不存在，跳过复制: {src}")
        return False

    copied = 0
    for rel in SEED_FILES:
        s = src / rel
        if not s.exists():
            continue
        t = dst / rel
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            if s.is_dir():
                shutil.copytree(s, t, dirs_exist_ok=True)
            else:
                shutil.copy2(s, t)
            copied += 1
        except Exception as e:
            log.debug(f"复制 {rel} 失败: {e}")

    # cookie 是关键：没有它，副本就是个空壳（页面会降级）
    has_cookie = (dst / "Default" / "Cookies").exists() or \
                 (dst / "Default" / "Network" / "Cookies").exists()
    if not has_cookie:
        log.warning(f"profile 副本里没有 cookie，登录态没复制过来: {dst}")
        return False
    log.info(f"profile 登录态已复制: {src} → {dst}（{copied} 个文件）")
    return True


class SeededProfiles:
    """给检测器准备 N 份「登录态副本」，一人一份，互不抢 profile 锁。

    为什么要 N 份：检测是并发的（默认 4 个浏览器同时跑），
    同一个 profile 目录开第二个浏览器会被 Chromium 锁住。
    """

    def __init__(self, src: str, base_dir: str, slots: int = 4,
                 refresh_minutes: int = 60):
        self.src = pathlib.Path(src).expanduser()
        self.base = pathlib.Path(base_dir).expanduser()
        self.slots = max(1, int(slots))
        self.refresh_seconds = max(0, int(refresh_minutes)) * 60
        self._seeded_at: float = 0.0
        self._counter = 0
        self._ok = False

    def _slot_dir(self, index: int) -> pathlib.Path:
        return self.base / f"slot{index % self.slots}"

    def ensure(self, force: bool = False) -> bool:
        """确保所有 slot 都有登录态副本。返回是否可用。"""
        now = time.time()
        if (not force and self._ok and self._seeded_at
                and now - self._seeded_at < self.refresh_seconds):
            return True
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(f"建 profile 目录失败: {e}")
            return False
        ok = True
        for i in range(self.slots):
            if not seed_profile(self.src, self._slot_dir(i)):
                ok = False
        self._ok = ok
        self._seeded_at = now if ok else 0.0
        if ok:
            log.info(f"检测用登录态 profile 就绪：{self.slots} 份（源 {self.src}）")
        else:
            log.error(f"检测用登录态 profile 准备失败（源 {self.src}），检测可能看不见直播间")
        return ok

    def next_dir(self) -> Optional[pathlib.Path]:
        """轮换取一个 slot 目录（并发检测时各自一份，避免抢锁）。"""
        if not self.ensure():
            return None
        idx = self._counter % self.slots
        self._counter += 1
        return self._slot_dir(idx)

    def refresh(self) -> bool:
        """强制重新复制（登录态换了、或检测不出来的时候调）。"""
        self._ok = False
        return self.ensure(force=True)
