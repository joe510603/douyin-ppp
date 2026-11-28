"""「页面说不存在」≠「这个人没了」—— 必须拿对照账号验证

事故现场（2026-11-27 20:57，线上抓的）
-------------------------------------
干跑即时触达时，同一条线索（悲伤Kitty猫）被判成：

    「抖音号已失效（主页提示用户不存在/已注销）」

但这个人 5 分钟前还**渲染得好好的**（昵称、作品都拿到了），
10 分钟后又正常 —— **账号好好的，是我们瞎了**。

根因：抖音把我们的**整个会话**降级时，主页被换成「用户不存在」的壳页：

    标题：「小米汽车 | 成都龙湖滨江体验店的抖音 - 抖音」  ← 账号名还在！
    正文：533 字 = 站点导航 + 法务页脚，末尾一句「用户不存在」

壳页里写着「用户不存在」→ 老代码直接判「账号失效」→ 线索记 not_found
→ 累计 3 次就判 skipped **终态** → **真客户被冤死**。

修法（2026-11-15 的教训：官方号对照才是决定性证据）：
    拿一个**必然存在**的账号（自家门店号）做对照。
      对照也打不开 → 是我们被降级 → 这条退回队列，不消耗、不判死
      对照正常     → 他才是真没了 → 照旧判 not_found

盯住的：
1. 对照账号也坏 → 抛 SessionDegraded（不是 LeadNotFound）
2. 对照账号正常 → 照旧 LeadNotFound（真失效的人还是要能判死）
3. 没有对照账号 → **不下**「账号没了」的结论（宁可多试，不能冤死客户）
4. SessionDegraded 走的是 WorksUnreachable 那条路 → 退回队列 + 计数 + 告警
"""

from __future__ import annotations

import pathlib
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    DOUYIN, Douyin, LeadNotFound, SessionDegraded,
)


class _FakePage:
    """假的 Playwright 页面"""

    def __init__(self, body: str = "", title: str = ""):
        self._body = body
        self._title = title
        self.url = DOUYIN + "/user/xxx"

    def evaluate(self, script: str, *_a):
        # 验证码探测那几段 JS 要返回布尔值；这里统一说「没有验证码」
        if "captcha" in script or "verifycenter" in script:
            return False
        # 作品网格兜底要返回 [[aid, desc], ...]
        if "scroll-list" in script or "data-e2e" in script:
            return []
        return self._body

    def title(self) -> str:
        return self._title

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def goto(self, url: str, **_kw) -> None:
        self.url = url

    @property
    def mouse(self):
        class _M:
            def wheel(self, *_a):
                return None
        return _M()

    # ---- _capture 要用的响应监听接口 ----
    def on(self, _event: str, _handler) -> None:
        return None

    def remove_listener(self, _event: str, _handler) -> None:
        return None


class _FakeDouyin(Douyin):
    """可控的 Douyin：对照账号页 vs 目标账号页"""

    def __init__(self, control_body: str = "", control_title: str = "",
                 target_body: str = "", target_title: str = ""):
        super().__init__(_FakePage(target_body, target_title), log=lambda m: None)
        self.control_body = control_body
        self.control_title = control_title
        self.target_body = target_body
        self.target_title = target_title
        self.opened: list[str] = []

    def ensure_login(self) -> None:
        return None

    def _goto(self, url: str, wait: str = "domcontentloaded", timeout: int = 45000,
              **kw) -> None:
        self.opened.append(url)
        self.page.url = url
        if self._control_sec_uid and self._control_sec_uid in url:
            self.page._body = self.control_body
            self.page._title = self.control_title
        else:
            self.page._body = self.target_body
            self.page._title = self.target_title


# 降级壳页（实测：533 字，站点框架 + 末尾「用户不存在」）
_SHELL_BODY = ("精选\n推荐\nAI抖音\n关注\n朋友\n我的\n直播\n放映厅\n短剧\n"
               "下载抖音精选\n2026 © 抖音\n京ICP备16016397号-3\n"
               "网络谣言曝光台\n网上有害信息举报\n违法和不良信息举报 400-140-2108\n"
               "算法推荐专项举报\n广告投放\n用户服务协议\n隐私政策\n账号找回\n"
               "联系我们\n加入我们\n友情链接\n站点地图\n下载抖音\n抖音电商\n搜索\n"
               "充钻石\n客户端\n壁纸\n通知\n消息\n投稿\n用户不存在")
_NORMAL_BODY = (
    "小米汽车 | 成都龙湖滨江体验店\n"
    "抖音号：xiaomijdcdljhx\n"
    "关注 128　粉丝 12.3万　获赞 45.6万\n"
    "简介：成都龙湖滨江体验店，欢迎预约试驾\n"
    "作品 12\n最新作品\n"
    "小米SU7 全新上市 到店有礼\n"
    "问界M9 试驾预约\n"
    "小米YU7 发布会回顾\n"
)


def _stub_capture(dy, hits: int = 0, empties: int = 0) -> None:
    """把 _capture 换成「什么都没抓到」的版本（并照常执行动作）

    真实 _capture 会：执行 action（开主页）→ 等响应 → 把证据写进 evidence。
    """
    def _cap(url, action, **kw):
        ev = kw.get("evidence")
        if ev is not None:
            ev.update({"hits": hits, "empty_bodies": empties,
                       "statuses": [], "parsed": 0})
        action()
        return []
    dy._capture = _cap


def _dy(**kw) -> _FakeDouyin:
    d = _FakeDouyin(**kw)
    d.set_control_account("MS4wLjABAAAACONTROL")
    return d


# ============================================================
# 1. 对照也坏 → 不是对方的问题
# ============================================================

def test_对照账号也坏就别说对方没了():
    """核心：壳页写着「用户不存在」，但对照账号也打不开 → 是我们被降级"""
    dy = _dy(control_body=_SHELL_BODY, control_title="小米汽车 | 成都龙湖滨江体验店的抖音 - 抖音",
             target_body=_SHELL_BODY, target_title="小米汽车 | 成都龙湖滨江体验店的抖音 - 抖音")
    with pytest.raises(SessionDegraded) as ei:
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")
    assert ei.value.diag == "session", ei.value.diag
    assert "降级" in str(ei.value), str(ei.value)


def test_对照账号坏的时候连主页都不用开():
    """会话都坏了 → 不用再开目标主页（省时间，也不该产生误判）"""
    dy = _dy(control_body=_SHELL_BODY, control_title="壳页",
             target_body=_SHELL_BODY, target_title="壳页")
    with pytest.raises(SessionDegraded):
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")
    assert not any("TARGET" in u for u in dy.opened), \
        f"会话坏了就不该去开目标主页，实际开了 {dy.opened}"


# ============================================================
# 2. 对照正常 → 对方真的没了，照旧判死
# ============================================================

def test_对照正常时照旧判对方没了():
    """真失效的抖音号还是得能判死（不然死号永远占队列）"""
    dy = _dy(control_body=_NORMAL_BODY, control_title="小米汽车 | 成都龙湖滨江体验店的抖音 - 抖音",
             target_body=_SHELL_BODY, target_title="壳页")
    with pytest.raises(LeadNotFound):
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")


# ============================================================
# 3. 资料接口是权威判据（2026-11-28 实测加）
# ============================================================

def test_资料接口说人还在就不能判死():
    """主页是空壳、写着「用户不存在」，但**资料接口说这个人还在**

    2026-11-28 实测：悲伤Kitty猫 的主页变成壳页（正文只剩站点框架、
    末尾挂着「用户不存在」），可标题是「悲伤Kitty猫的抖音」、
    资料接口也查得到他 —— **账号好好的，是我们没加载出来**。
    这种时候判「账号没了」→ not_found → 累计 3 次判死 → 真客户被冤死。
    """
    dy = _dy(control_body=_NORMAL_BODY, control_title="正常",
             target_body=_SHELL_BODY, target_title="壳页")
    dy._profile_info = lambda sec_uid: {
        "sec_uid": sec_uid, "nickname": "悲伤Kitty猫",
        "unique_id": "Matlc520", "signature": "", "aweme_count": 0,
    }
    with pytest.raises(SessionDegraded) as ei:
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")
    assert "资料接口" in str(ei.value), str(ei.value)


def test_资料接口说人没了且对照正常才判死():
    """两道判据都不成立 → 他才是真没了"""
    dy = _dy(control_body=_NORMAL_BODY, control_title="正常",
             target_body=_SHELL_BODY, target_title="壳页")
    dy._profile_info = lambda sec_uid: None      # 接口查不到（可能也被限流）
    with pytest.raises(LeadNotFound):
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")


def test_资料接口查不到时不能当成账号没了():
    """接口限流返回空 ≠ 账号没了 → 还要问对照账号"""
    dy = _dy(control_body=_SHELL_BODY, control_title="壳页",
             target_body=_SHELL_BODY, target_title="壳页")
    dy._profile_info = lambda sec_uid: None
    with pytest.raises(SessionDegraded):
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")


# ============================================================
# 4. 没有对照账号 → 不下结论
# ============================================================

def test_没有对照账号就不判死():
    """没有对照账号时，宁可不下「账号没了」的结论

    冤死一个真客户，比多试几次的代价大得多。
    """
    dy = _FakeDouyin(target_body=_SHELL_BODY, target_title="壳页")
    # 没有 set_control_account
    with pytest.raises(SessionDegraded):
        dy.resolve_by_sec_uid("MS4wLjABAAAATARGET")


# ============================================================
# 4. 对照结果要缓存（降级是持续状态，不用每条都查）
# ============================================================

def test_对照结果缓存10分钟():
    dy = _dy(control_body=_NORMAL_BODY, control_title="正常",
             target_body=_NORMAL_BODY, target_title="正常")
    dy._control_renders()
    n = len([u for u in dy.opened if "CONTROL" in u])
    assert n == 1, n
    dy._control_renders()
    assert len([u for u in dy.opened if "CONTROL" in u]) == 1, \
        "10 分钟内不该重复对照（每条线索都查一遍太贵）"
    # 超过缓存时间 → 重新查
    dy._control_checked_at = time.time() - 1200
    dy._control_renders()
    assert len([u for u in dy.opened if "CONTROL" in u]) == 2, \
        "缓存过期要重新对照"


def test_对照被降级时也要缓存():
    dy = _dy(control_body=_SHELL_BODY, control_title="壳页",
             target_body=_SHELL_BODY, target_title="壳页")
    assert dy._control_renders() is False
    n = len([u for u in dy.opened if "CONTROL" in u])
    assert dy._control_renders() is False
    assert len([u for u in dy.opened if "CONTROL" in u]) == n, \
        "降级期间也该缓存（否则每条线索都去撞一次）"


# ============================================================
# 5. 作品数 0 + 会话正常 → 真没作品（不是「抓不到」）
# ============================================================

def test_资料说作品数0且会话正常就判真没作品():
    """抖音对 0 作品的号**压根不发**作品请求

    2026-11-28 实测：悲伤Kitty猫 aweme_count=0 → 作品请求一次都没发；
    天令 aweme_count=3 → 正常发了，抓到 3 条。
    所以「作品请求没来」不能一律当「抓不到」——
    否则这条线索会在队列里永远转圈（每轮白花 24 秒）。
    """
    dy = _dy(control_body=_NORMAL_BODY, control_title="正常",
             target_body=_NORMAL_BODY, target_title="悲伤Kitty猫的抖音 - 抖音")
    dy._profile_info = lambda sec_uid: {
        "sec_uid": sec_uid, "nickname": "悲伤Kitty猫",
        "unique_id": "Matlc520", "signature": "", "aweme_count": 0,
    }
    _stub_capture(dy)
    works = dy._fetch_works_page("MS4wLjABAAAATARGET")
    assert works == [], works
    st = dy.last_works_fetch
    assert st["status"] == "empty", st
    assert "作品数是 0" in st["detail"], st


def test_会话坏的时候不能判真没作品():
    """会话坏 + 作品数 0 → 不能当成「真没作品」（可能是我们瞎了）"""
    dy = _dy(control_body=_SHELL_BODY, control_title="壳页",
             target_body=_SHELL_BODY, target_title="壳页")
    dy._profile_info = lambda sec_uid: {
        "sec_uid": sec_uid, "nickname": "悲伤Kitty猫",
        "unique_id": "Matlc520", "signature": "", "aweme_count": 0,
    }
    _stub_capture(dy)
    dy._fetch_works_page("MS4wLjABAAAATARGET")
    st = dy.last_works_fetch
    assert st["status"] == "failed", st
    assert st["diag"] == "session", st


def test_作品数不是0就不能判真没作品():
    """资料说 aweme_count>0 → 说明他是有作品的 → 抓不到就是抓不到"""
    dy = _dy(control_body=_NORMAL_BODY, control_title="正常",
             target_body=_NORMAL_BODY, target_title="正常")
    dy._profile_info = lambda sec_uid: {
        "sec_uid": sec_uid, "nickname": "天令",
        "unique_id": "lyj312690", "signature": "", "aweme_count": 3,
    }
    _stub_capture(dy)
    dy._fetch_works_page("MS4wLjABAAAATARGET")
    st = dy.last_works_fetch
    assert st["status"] == "failed", st
    assert st["diag"] == "endpoint", st


# ============================================================
# 6. worker 要真的把对照账号给到 Douyin
# ============================================================

def test_worker会挑自家门店号当对照():
    """对照账号必须是「必然存在」的 —— 门店官方号最合适"""
    from src.outreach.worker import OutreachWorker
    import sqlite3

    tmp = pathlib.Path("/tmp") / "t_ctrl.db"
    if tmp.exists():
        tmp.unlink()
    cfg = get_config().model_copy(deep=True)
    w = OutreachWorker(cfg, str(tmp), log=lambda m: None)
    try:
        sec = w._control_sec_uid()
        assert sec, "应该能挑出一个自家门店号当对照"
        # 必须是配置里真实存在的那个
        own = {(m.sec_user_id or "") for m in (cfg.monitors or [])}
        assert sec in own, sec
    finally:
        w.st.close()
        if tmp.exists():
            tmp.unlink()
