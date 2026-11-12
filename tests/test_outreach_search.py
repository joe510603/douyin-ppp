"""抖音搜索：超时余量 + 限流短路

背景（2026-11-12 实测）
------------------------
抖音的**用户列表接口** ``/aweme/v1/web/discover/search/`` 响应要 **20 秒**
（同一会话里其它搜索接口都是 6 毫秒返回）。而采集的超时原来正好是 20000ms
→ 响应在临界点才回来，经常**刚好错过** → 被判成「搜不到人」。

两个后果，第二个更要命：
1. 每条线索白等 20~40 秒；
2. 搜失败 → 这一页十几个用户的 sec_uid 缓存不下来
   → 后面那十几条线索还是得搜 → 每条每轮都重复付这 20 秒（死循环）。

修法：
- 超时放宽到 40000ms（给足余量）
- 探针结论「接口被限流」时，**在搜索之前**就短路（明知白等就别搜）
"""
from __future__ import annotations

import time

from src.outreach.douyin_api import Douyin


def _douyin() -> Douyin:
    """只要用到不碰 page 的方法，page 传个占位符就行"""
    return Douyin(page=None, log=lambda m: None)


# ============================================================
# 超时余量
# ============================================================

def test_搜索超时必须大于接口的实际响应时间():
    """用户列表接口实测 20 秒才响应，超时必须留足余量

    原来正好 20000ms = 响应在临界点回来 → 经常刚好错过。
    """
    import inspect
    from src.outreach import douyin_api

    src = inspect.getsource(douyin_api.Douyin._search)
    assert "timeout=40000" in src, "搜索超时必须 ≥ 40 秒（实测响应 20 秒）"


def test_不再强求搜到目标才停():
    """响应到了就停，别为「这个人根本不存在」白等满超时"""
    import inspect
    from src.outreach import douyin_api

    src = inspect.getsource(douyin_api.Douyin._search)
    assert '"user_list" in j' in src, "expect 应该在响应到了时就成立"


# ============================================================
# 限流短路
# ============================================================

def test_一开始不知道接口废没废():
    d = _douyin()
    assert d._search_known_throttled() is False


def test_探针判定限流后_搜索前就短路():
    """探针说接口废了 → 下一条线索连搜都不用搜"""
    d = _douyin()
    # 假装刚刚探针判定失败
    d._search_healthy = False
    d._search_health_at = time.time()
    assert d._search_known_throttled() is True


def test_限流结论过期得比正常结论快():
    """抖音限流是概率性的，可能几十秒就恢复 —— 限流结论不该留 5 分钟"""
    d = _douyin()
    assert d.SEARCH_UNHEALTHY_TTL < d.SEARCH_HEALTH_TTL


def test_限流结论过期之后就不短路了():
    """过期了就重新搜 —— 限流可能已经恢复了"""
    d = _douyin()
    d._search_healthy = False
    d._search_health_at = time.time() - (d.SEARCH_UNHEALTHY_TTL + 5)
    assert d._search_known_throttled() is False


def test_接口正常时永远不短路():
    d = _douyin()
    d._search_healthy = True
    d._search_health_at = time.time()
    assert d._search_known_throttled() is False


def test_探针缓存_健康结论用长有效期():
    """接口正常时，探针结论留 5 分钟（少做无谓的探针搜索）"""
    d = _douyin()
    d._search_healthy = True
    d._search_health_at = time.time()
    assert d._health_ttl() == d.SEARCH_HEALTH_TTL
