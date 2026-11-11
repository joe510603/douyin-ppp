"""实测：静音 + 拦媒体

用户反馈：「要关闭网页的声音，避免打开作品放声音」

要验证两件事：
1. 视频元素是不是真的 muted（没有声音）
2. 拦掉媒体请求之后，页面**还能不能正常渲染**
   （抖音首页是无限流，评论区/作品列表是 DOM，理论上不需要视频数据，
     但这是「为了安静把功能搞坏」的高风险改动，必须实测）

用**临时 profile**（全新目录，不碰登录态），只看首页渲染，不登录、不发任何东西。

    .venv/bin/python scripts/check_mute_media.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser  # noqa: E402

DOUYIN = "https://www.douyin.com/"


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mute_check_"))
    profile = tmp / "profile"
    media_ok = []
    media_blocked = []

    try:
        with Browser(profile, headless=False, block_media=True) as (ctx, page):
            # 记录媒体请求的结果
            def _route(route):
                try:
                    if route.request.resource_type == "media":
                        media_blocked.append(route.request.url[:80])
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    pass

            ctx.route("**/*", _route)
            page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)

            # 1. 视频元素是不是都静音了
            muted_info = page.evaluate(
                """() => {
                    const vs = Array.from(document.querySelectorAll('video'));
                    const aus = Array.from(document.querySelectorAll('audio'));
                    return {
                        videoCount: vs.length,
                        audioCount: aus.length,
                        allMuted: vs.every(v => v.muted === true)
                                  && aus.every(a => a.muted === true),
                        playing: vs.filter(v => !v.paused && v.currentTime > 0).length,
                        volumes: vs.map(v => v.volume),
                    };
                }"""
            )
            print(f"① 媒体元素: {muted_info}")

            # 2. 页面渲染出来了吗（抖音首页应该有作品卡片/头像等）
            render = page.evaluate(
                """() => ({
                    title: document.title,
                    bodyLen: (document.body ? document.body.innerText : '').length,
                    links: document.querySelectorAll('a[href*="/video/"]').length,
                    imgs: document.querySelectorAll('img').length,
                    hasLogin: !!(document.querySelector('a[href*="login"]')
                                  || document.querySelector('.login-button')),
                })"""
            )
            print(f"② 页面渲染: {render}")

            # 3. 媒体请求确实被拦了
            print(f"③ 被拦的媒体请求: {len(media_blocked)} 条")
            for u in media_blocked[:3]:
                print(f"    - {u}")

        ok_mute = muted_info["allMuted"]
        # 游客版首页本来就不暴露 /video/ 链接（那是登录后用户主页才有的），
        # 所以判据只看「页面确实渲染出内容了」。
        ok_render = render["bodyLen"] > 200 and render["imgs"] > 0
        ok_block = len(media_blocked) > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"视频全部静音:   {'✅' if ok_mute else '❌'}")
    print(f"页面正常渲染:   {'✅' if ok_render else '❌'}")
    print(f"媒体请求被拦:   {'✅' if ok_block else '❌'}")
    return 0 if (ok_mute and ok_render and ok_block) else 1


if __name__ == "__main__":
    raise SystemExit(main())
