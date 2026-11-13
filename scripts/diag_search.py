"""诊断：搜索到底为什么失败 —— 用**生产代码路径**实测

背景（2026-11-13）
------------------
用户抽查了 3 条被标「失败」的线索，**全都是能搜到的**，
而另一台机器上从来没遇到过搜索失败 / 弹验证。

要分清三种可能：
A. **接口被限流**（探针也空）—— 平台不给搜了，跟这个人无关
B. **接口正常但没点到「用户」tab** —— 页面没触发用户列表接口
C. **真的搜不到这个人** —— 号失效了

所以这个脚本把搜索过程**每一个** discover/search 响应都留下来，
连同耗时、user_list 长度、search_nil_type、目标匹配情况一起打印。

用法：
    .venv/bin/python scripts/diag_search.py                # 默认测几个之前失败的号
    .venv/bin/python scripts/diag_search.py 999999106 123  # 指定抖音号
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402

# 之前被标「失败」的（都是被限流误杀的），外加一个已知能搜到的对照
DEFAULT_TARGETS = [
    "test_user_d",
    "test_user_c",
    "999999110",
    "999999106",   # 对照组：2026-11-12 实测 7~8 秒找到、认对过人
]


def _dump_raw(raws: list[dict]) -> str:
    """把响应压缩成一行能看懂的摘要"""
    if not raws:
        return "（一个用户列表接口的响应都没抓到）"
    j = raws[0]
    users = j.get("user_list") or []
    nil = j.get("search_nil_info") or {}
    return (
        f"status_code={j.get('status_code')} cursor={j.get('cursor')} "
        f"has_more={j.get('has_more')} user_list={len(users)} "
        f"nil_type={nil.get('search_nil_type') or '-'} "
        f"顶层键={sorted(j.keys())[:14]}"
    )


def main() -> int:
    targets = sys.argv[1:] or DEFAULT_TARGETS
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    print(f"profile: {profile}")
    print(f"目标: {targets}\n")

    raw_capture: list[dict] = []

    with Browser(profile, headless=False, slow_mo=0, window_mode="background") as (_ctx, page):
        dy = Douyin(page, log=lambda m: print("   ", m))

        def _on_resp(r):
            try:
                if "discover/search" in (r.url or ""):
                    raw_capture.append({
                        "t": time.time(),
                        "status": r.status,
                        "text": r.text()[:200000],
                    })
            except Exception:
                pass

        page.on("response", _on_resp)

        print(f"登录态 cookies_ok: {dy.cookies_ok()}\n")
        for q in targets:
            raw_capture.clear()
            t0 = time.time()
            print(f"───── 搜「{q}」─────")
            try:
                info = dy.resolve(q, nickname=None)
            except Exception as e:
                print(f"   ❌ 抛异常 {type(e).__name__}: {str(e)[:120]}")
                print(f"   ⏱ {time.time()-t0:.1f}s")
                for raw in raw_capture[:2]:
                    try:
                        j = json.loads(raw["text"])
                        print("   原始响应:", _dump_raw([j]))
                    except Exception:
                        print("   原始响应: 解析失败", raw["text"][:120])
                continue
            dt = time.time() - t0
            print(f"   结果: {info}")
            print(f"   ⏱ {dt:.1f}s")
            for raw in raw_capture[:3]:
                try:
                    j = json.loads(raw["text"])
                    print("   原始响应:", _dump_raw([j]))
                    users = j.get("user_list") or []
                    for u in users[:6]:
                        uniq = u.get("unique_id") or ""
                        nick = (u.get("nickname") or "")[:16]
                        print(f"      · {uniq:<18} {nick}")
                except Exception:
                    print("   原始响应: 解析失败", raw["text"][:150])
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
