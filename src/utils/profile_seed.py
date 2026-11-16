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
import threading
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

    【2026-11-15】限流是**按 profile 目录**记的，不是按 IP：
    实测同一目录第 1 个账号正常渲染，第 2 个开始就被降级成壳页；
    等 75 秒也不恢复；而**删掉目录重建**（或换个全新目录）立刻恢复正常。
    → 所以 slot 用过一轮之后要**整个删掉重建**，不能只覆盖复制
      （覆盖复制会保留 Edge 运行时写进去的降级标记，删掉重建才会清干净）。
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
        # 每个 slot 上次用完的时刻（0 = 还没用过）
        self._used_at: dict[int, float] = {}
        # 🔒 并发安全：检测是 4 个线程同时跑的。
        #   轮转取号可能让两个线程拿到同一个 slot（→ 抢 profile 锁 → 一启动就失败）。
        #   所以这里用锁 + 「占用中」集合：正在用的 slot 不再分配给别人。
        self._lock = threading.Lock()
        self._in_use: set[int] = set()

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
        idx = self.next_index()
        return self.slot_path(idx) if idx is not None else None

    def next_index(self) -> Optional[int]:
        """取一个**没人占用**的 slot 序号（并发安全）。

        找不到空闲 slot 时返回 None —— 调用方宁可这次不用副本，
        也不要两个线程抢同一个目录（Chromium 会直接报 profile 被锁）。
        """
        if not self.ensure():
            return None
        with self._lock:
            for _ in range(self.slots):
                idx = self._counter % self.slots
                self._counter += 1
                if idx not in self._in_use:
                    self._in_use.add(idx)
                    return idx
        return None

    def release(self, index: int) -> None:
        """用完了：把这个 slot 标记为可再用（**不删目录**）。"""
        with self._lock:
            self._in_use.discard(index)

    def slot_path(self, index: int) -> pathlib.Path:
        """slot 序号 → 目录路径。"""
        return self._slot_dir(index)

    def recycle_slot(self, index: int) -> bool:
        """这个 slot 用过了 → **整个删掉重建**，清掉 Edge 写进去的降级标记。

        【2026-11-15 实测】限流是按 profile 目录记的：
          · 同一目录第 1 个账号正常渲染，第 2 个开始被降级成壳页
          · 等 75 秒也不恢复（不是时间窗口）
          · 删掉目录重建 → 立刻恢复正常
        所以每次用完一个 slot 就重建它，让每个账号都用干净 profile 上场。

        重建失败不致命（下次还会再试），返回是否成功。
        """
        try:
            ok = self.reset_slot(index)
            if ok:
                log.debug(f"slot{index} 已重建（清掉上一次的降级标记）")
            else:
                log.warning(f"slot{index} 重建失败（限流标记可能没清掉）")
        except Exception as e:
            log.warning(f"重建 slot{index} 失败: {e}")
            ok = False
        finally:
            # 无论重建成功与否都要释放占用，否则这个 slot 永远分配不出去
            self.release(index)
        return ok

    def reset_slot(self, index: int) -> bool:
        """把某个 slot **整个删掉重建**（清掉 Edge 写进去的降级标记）。

        为什么必须删目录而不是覆盖复制：
        Edge 运行时会在 profile 里写下这次会话的痕迹（降级标记就在里头），
        覆盖复制只换掉那 6 个种子文件，**残留的痕迹还在** →
        实测覆盖复制后依然被限，删目录重建才恢复。

        Args:
            index: slot 序号。

        Returns:
            是否重建成功。
        """
        dst = self._slot_dir(index)
        try:
            if dst.exists():
                # ⚠ 不能用 ignore_errors=True —— 删不掉时会被**静默吞掉**，
                #   表现就是「以为回收了，其实 slot 里还留着上次的降级标记」，
                #   而日志上一片正常（跟当初那个「登录态有效但其实没登录」一个毛病）。
                #   这里如实报告删了多少、剩多少。
                shutil.rmtree(dst)
        except Exception as e:
            log.warning(f"删除旧 profile 目录失败（{dst}）: {e}")
            # 删不掉就退一步：至少把里面的缓存/Service Worker 清掉，
            # 降级标记主要就写在 Local Storage 和缓存里。
            try:
                for sub in ("Default/Cache", "Default/Code Cache",
                            "Default/Service Worker", "Default/GPUCache"):
                    p = dst / sub
                    if p.exists():
                        shutil.rmtree(p, ignore_errors=True)
                log.info(f"slot{index} 目录删不掉，已清理缓存目录（降级标记可能仍在）")
            except Exception as e2:
                log.debug(f"清理 slot{index} 缓存也失败: {e2}")
            return False
        ok = seed_profile(self.src, dst)
        if ok:
            self._used_at[index] = time.time()
        return ok

    def reset_all(self) -> int:
        """把所有 slot 删掉重建（检测大面积降级时的自愈手段）。"""
        n = 0
        for i in range(self.slots):
            if self.reset_slot(i):
                n += 1
        self._seeded_at = time.time() if n else 0.0
        self._ok = bool(n)
        return n

    def refresh(self) -> bool:
        """强制重新复制（登录态换了、或检测不出来的时候调）。"""
        self._ok = False
        return self.ensure(force=True)

