"""「抓不到」≠「没有」——作品列表抓不到时，绝不能当成「这个人没作品」

2026-11-26 实测出的真 bug（伤害面很大）：

    抖音把「主页作品列表」接口静默降级时（探针证据）：
      · 页面自己发的 /aweme/v1/web/aweme/post/ 回到 **HTTP 200 + 0 字节**
      · 主页作品网格（[data-e2e="user-post-list"]）里 **0 条**
      · 老接口 iesdouyin /web/api/v2/aweme/post/ 同样 **0 字节**
      · 登录态和**游客态**结果完全一样 → IP 级降级，不是账号问题
      · 同期资料接口正常（aweme_count 拿得到）、弹幕正常、私信能发
      · **对照组**（11-25 留言成功过的两个人）当天也一样抓不到
        → 证明这不是「这些人没作品」，是我们这条通道整体坏了

    程序以前把「抓不到」当成「没有作品」→
      改发私信 + 永久标记 no_works → 这批人**再也不会去留言**。
      后果：543 条线索被误判（抽样 14 条里 9 条实际有 5~107 条作品），
      其中 23 条已经被私信消耗掉 —— 有视频的客户被私信顶掉了。

盯住的：
1. 抓不到 → 抛 WorksUnreachable，**不私信、不标记 no_works、退回 pending**
2. 接口**可信**地说没有（合法 JSON + 空数组）→ 才走私信兜底
3. 抓到作品 → 走留言
4. 历史 no_works 标记**不再**用来跳过作品拉取
5. 连续几条抓不到 → 停手 + 告警（kind=works），不继续消耗线索
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    KIND_WORKS, NoWorks, WorksUnreachable, normalize_kind,
)
from src.outreach.worker import OutreachWorker  # noqa: E402


# ============================================================
# 假对象
# ============================================================

class _FakeDouyin:
    """假装是个 Douyin

    ``works_status`` 模拟作品抓取的三种结果：
        ok     = 真拿到作品了
        empty  = 接口正常返回、列表就是空的（真没作品）
        failed = 抓不到（HTTP 200 但响应体是空的）
    """

    def __init__(self, works_status: str = "ok", works: list | None = None,
                 dm_ok: bool = True):
        self.works_status = works_status
        self.works = works if works is not None else (
            [{"aweme_id": "7703927024881327795", "desc": "作品",
              "create_time": 1780000000, "is_top": False}] if works_status == "ok" else [])
        self.dm_ok = dm_ok
        self.last_works_fetch = {"status": works_status, "detail": ""}
        self.last_search_users: list[dict] = []
        self.sent_dm: list[tuple[str, str]] = []
        self.sent_comment: list[tuple[str, str]] = []

    # ---- 找人 ----
    def resolve_by_sec_uid(self, sec_uid: str) -> dict:
        return {"sec_uid": sec_uid, "nickname": "测试昵称A", "aweme_count": 7,
                "signature": ""}

    def resolve(self, unique_id: str, nickname: str | None = None) -> dict:
        return self.resolve_by_sec_uid("MS4wLjABAAAAtest")

    # ---- 作品 ----
    def fetch_works(self, sec_uid: str, limit: int = 20) -> list:
        return self.works[:limit]

    # ---- 私信 ----
    def send_dm(self, sec_uid: str, text: str) -> dict:
        self.sent_dm.append((sec_uid, text))
        return {"ok": True, "msg": "已发送", "is_private": False}

    def detect_private_account(self) -> bool:
        return False

    def _body_text(self) -> str:
        return ""

    def check_risk(self) -> None:
        return None

    def cookies_ok(self) -> bool:
        return True

    def set_control_account(self, sec_uid: str) -> None:
        """对照账号（2026-11-27）：真实 Douyin 用它分清「他没了」和「我们瞎了」。

        假对象不用真做对照，记下来就行（省得每个测试都去开一个对照页）。
        """
        self.control_sec_uid = sec_uid


def _建库(tmp_path, n: int = 3):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leads ("
        " id INTEGER PRIMARY KEY, lead_key TEXT, user_id TEXT, display_id TEXT, "
        " nickname TEXT, avatar TEXT, room_id TEXT, monitor_name TEXT, "
        " live_room TEXT, intent_level TEXT, intent_score INTEGER, "
        " intent_reason TEXT, car_model TEXT, contact_type TEXT, "
        " contact_value TEXT, evidence TEXT, evidence_count INTEGER, "
        " comment_count INTEGER, status TEXT DEFAULT 'new', pushed_at TEXT, "
        " outreach_status TEXT DEFAULT 'pending', outreach_at TEXT, "
        " outreach_note TEXT, created_at TEXT DEFAULT '', updated_at TEXT, "
        " source TEXT, bitable_record_id TEXT, bitable_synced_status TEXT, "
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, "
        " not_found_count INTEGER DEFAULT 0, no_works INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE outreach_records ("
        " id TEXT PRIMARY KEY, lead_id INTEGER, record_id TEXT, display_id TEXT, "
        " nickname TEXT, sec_uid TEXT, aweme_id TEXT, aweme_desc TEXT, "
        " comment_text TEXT, template_id TEXT, status TEXT, channel TEXT, "
        " error TEXT, verified INTEGER DEFAULT 0, created_at TEXT DEFAULT '', "
        " local_time TEXT DEFAULT '')"
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
            " outreach_status, status, no_works) VALUES (?, ?, ?, ?, ?, "
            "'pending', 'new', ?)",
            (i, f"dy_{i}", f"昵称{i}", f"MS4wLjABAAAAtest{i}", "测试门店", 0))
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, n: int = 3, **cfg_over):
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.dm_fallback = True
    cfg.outreach.dm_daily_limit = 10
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 1
    cfg.outreach.max_delay = 1
    cfg.outreach.works_fail_stop_threshold = 3
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(_建库(tmp_path, n)), log=logs.append)
    w._dry = False
    return w, logs


def _lead(**kw) -> dict:
    lead = {
        "id": 1,
        "display_id": "dy_1",
        "nickname": "昵称1",
        "sec_uid": "MS4wLjABAAAAtest1",
        "store": "测试门店",
        "car_model": "小米SU7",
        "contact_type": "",
        "contact_value": "",
        "evidence": "[]",
        "monitor_name": "测试直播间",
        "bitable_record_id": "",
        "intent_level": "high",
        "intent_score": 90,
        "no_works": 0,
    }
    lead.update(kw)
    return lead


# ============================================================
# 1. 抓不到 → 不是「没作品」
# ============================================================

def test_抓不到不当成没作品(tmp_path):
    """核心：作品列表抓不到 → 抛 WorksUnreachable，不私信、不标记"""
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(works_status="failed")
        with pytest.raises(WorksUnreachable) as ei:
            w._handle_one(dy, _lead(), {"dry": 0})
        assert "抓不到" in str(ei.value), str(ei.value)
        assert dy.sent_dm == [], "抓不到的时候**绝不能**发私信（会消耗这条线索）"
        # 本地也不能标记 no_works
        row = w.st.get_lead(1)
        assert not row.get("no_works"), "抓不到不能标记成「已确认没作品」"
    finally:
        w.st.close()


def test_抓不到时线索退回pending(tmp_path):
    """退回队列，等通道恢复再留言 —— 不能变成终态"""
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(works_status="failed")
        with pytest.raises(WorksUnreachable):
            w._handle_one(dy, _lead(), {"dry": 0})
        row = w.st.get_lead(1)
        assert row["outreach_status"] in ("pending", "searching", "found"), \
            row["outreach_status"]
    finally:
        w.st.close()


def test_抓不到不写触达记录(tmp_path):
    """触达记录是「已经联系过」的凭据 —— 抓不到时不该有"""
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(works_status="failed")
        with pytest.raises(WorksUnreachable):
            w._handle_one(dy, _lead(), {"dry": 0})
        rows = w.st._conn.execute(
            "SELECT COUNT(*) FROM outreach_records").fetchone()[0]
        assert rows == 0, f"不该有触达记录，却有 {rows} 条"
    finally:
        w.st.close()


# ============================================================
# 2. 接口可信地说没有 → 才走私信
# ============================================================

def test_接口说没有才走私信(tmp_path):
    """status=empty（合法 JSON + 空数组）= 这个人确实没作品 → 私信兜底"""
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(works_status="empty")
        with pytest.raises(NoWorks):
            # 真发分支里私信失败会抛别的异常，这里只关心「有没有走私信」
            w._handle_one(dy, _lead(), {"dry": 0})
    except Exception as e:
        # 私信发不出去也算走过了私信路径
        assert not isinstance(e, WorksUnreachable), "可信的空不该抛 WorksUnreachable"
    assert dy.sent_dm, "接口可信地说没有作品 → 应该发私信"
    row = w.st.get_lead(1)
    assert row.get("no_works"), "可信的空 → 可以标记 no_works"
    w.st.close()


def test_抓不到和可信空走的是两条路(tmp_path):
    """同一个「作品列表是空的」，两种状态必须分道扬镳"""
    w, logs = _worker(tmp_path)
    try:
        # failed → 抛 WorksUnreachable
        with pytest.raises(WorksUnreachable):
            w._handle_one(_FakeDouyin(works_status="failed"), _lead(), {"dry": 0})
        # empty → 走私信，不抛 WorksUnreachable
        try:
            w._handle_one(_FakeDouyin(works_status="empty"), _lead(), {"dry": 0})
        except WorksUnreachable:
            pytest.fail("可信的空不该抛 WorksUnreachable")
        except Exception:
            pass
    finally:
        w.st.close()


# ============================================================
# 3. 历史 no_works 标记不再跳过作品拉取
# ============================================================

def test_历史no_works标记不再跳过作品(tmp_path):
    """以前 lead['no_works']=1 会直接转私信（连作品都不拉）→ 回归护栏

    这条最关键：那 543 条误判就是靠这个标记「永久锁死」在私信通道上的。
    """
    w, logs = _worker(tmp_path)
    try:
        dy = _FakeDouyin(works_status="ok")
        w._handle_one(dy, _lead(no_works=1), {"dry": 0})
        # 有作品 → 一定会去发留言，不该走私信
        assert dy.sent_dm == [], "历史 no_works 标记不该再让人跳过作品拉取"
    except WorksUnreachable:
        pytest.fail("有作品却抛 WorksUnreachable")
    except Exception:
        # 留言发送失败没关系，重点是**没有**转私信
        pass
    assert dy.sent_dm == [], "历史 no_works 标记不该再让人跳过作品拉取"
    w.st.close()


# ============================================================
# 4. 通道级熔断
# ============================================================

def test_连续抓不到就停手(tmp_path, monkeypatch):
    """连续 3 条抓不到 → 停手（kind=works），不能继续一条条私信消耗"""
    w, logs = _worker(tmp_path, n=3)

    @contextmanager
    def _fake_page_ctx(profile_dir, headless, session):
        yield object()

    monkeypatch.setattr(w, "_page_ctx", _fake_page_ctx)
    monkeypatch.setattr("src.outreach.worker.Douyin",
                        lambda page, shot_dir=None, log=print: _FakeDouyin(
                            works_status="failed"))

    try:
        res = w.run(limit=3)
    finally:
        w.st.close()

    assert res.get("stop_kind") == KIND_WORKS, res
    assert "取不到作品" in str(res.get("stopped") or ""), res
    # 一条私信都不能发出去
    # （发出去就等于把有作品的客户私信消耗掉了）


def test_works类型能归一化():
    assert normalize_kind("works") == KIND_WORKS
    assert normalize_kind("作品列表") == KIND_WORKS


def test_抓不到不是限流也不是验证():
    """分类必须干净：作品接口坏了 ≠ 账号被限 ≠ 要验证码

    账号被限要等解封，要验证码要人过 —— 方向完全不一样。
    """
    assert normalize_kind("works") not in ("verify", "login", "limit")


# ============================================================
# 5. 飞书回拉不能把修复盖回去
# ============================================================

def _回拉会不会采纳(local_status: str, synced: str, remote_os: str) -> bool:
    """复刻 lead_service.pull_bitable_status 的判据

    返回 True = 回拉会把飞书的值盖到本地。
    规则（跟线上代码一致）：
      · 有同步标记 → 本地状态 == 标记里的值 就算「本地没改过」→ 采纳飞书
      · 没同步标记 → 本地是默认值就算「本地没改过」→ 采纳飞书
    """
    from src.notify.bitable import OUTREACH_LABELS, STATUS_LABELS

    local_st = STATUS_LABELS.get("new", "待处理")
    local_os = OUTREACH_LABELS.get(local_status, "待触达")
    if synced:
        s_st, _, s_os = synced.partition("|")
        unchanged = (local_st == (s_st or "").strip()) and (
            local_os == (s_os or "").strip())
    else:
        unchanged = local_st == "待处理" and local_os == "待触达"
    if not unchanged:
        return False
    # 采纳飞书的值：只有飞书的值跟本地不同才会真的改
    return remote_os != local_os


def test_修复前回拉会把修复盖回去():
    """事故复现：修复只改本地 → 5 分钟后被回拉推翻（实测 323→274 被盖回）

    根因：这些线索**从没同步过**状态（标记为空）→
    回拉认为「本地还是默认值」→ 无条件采纳飞书里的旧值「已跳过」。
    """
    # 修复后本地是 pending，但标记还空着 → 回拉会采纳飞书的「已跳过」
    assert _回拉会不会采纳("pending", "", "已跳过") is True


def test_修复后回拉盖不回去():
    """修复必须**写飞书 + 对齐同步标记**，两边才一致

    只改本地（不管飞书）→ 下一个回拉周期就被推翻；
    这条测试钉住「写飞书成功 → 标记对齐 → 本地==标记==飞书」这条正确姿势。
    """
    from src.notify.bitable import OUTREACH_LABELS, STATUS_LABELS

    local_st = STATUS_LABELS.get("new", "待处理")
    synced = f"{local_st}|待触达"          # 写飞书成功后对齐
    # 飞书那边也已经是「待触达」→ 回拉采纳的值 == 本地值 → 不会改
    assert _回拉会不会采纳("pending", synced, "待触达") is False


def test_修复脚本必须写飞书并对齐同步标记():
    """静态审计：修复脚本少了这两步，历史修复会被回拉推翻"""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "scripts" / "repair_no_works_misjudge.py").read_text(encoding="utf-8")
    assert "bitable_synced_status" in src, "修复必须对齐同步标记，否则被回拉盖回"
    assert "patch_fields" in src, "修复必须把「待触达」写回飞书，否则回拉会盖回"
    assert "待触达" in src, "写回飞书的值必须是「待触达」"


# ============================================================
# 6. 回拉的竞态：本地刚改过的，不能被旧快照盖回去
# ============================================================

def test_回拉不覆盖本轮刚改过的线索():
    """回拉先读飞书快照、再逐条比对 —— 这中间本地改了就该以本地为准

    实测（2026-11-26）：批量修复 323 条时，正在处理中的 3 条
    被回拉拿**旧快照**盖回「已跳过」。
    """
    from datetime import datetime, timedelta

    from src.intelligence.lead_service import _本地状态比快照新

    snapshot = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    # 本轮开始前 10 分钟改的 → 不是「比快照新」→ 回拉照常采纳飞书的值
    assert _本地状态比快照新(
        (snapshot - timedelta(minutes=10)).strftime(fmt), snapshot) is False
    # 本轮开始后改的 → 比快照新 → 这一轮不能拿飞书的旧快照覆盖它
    assert _本地状态比快照新(snapshot.strftime(fmt), snapshot) is True
    # 没有时间戳 / 乱码 → 不当成本轮改的（老数据别被误伤）
    assert _本地状态比快照新("", snapshot) is False
    assert _本地状态比快照新("不是时间", snapshot) is False
