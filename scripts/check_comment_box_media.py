"""实测：拦媒体之后，**作品页的评论区**还能不能正常渲染

这才是触达真正用的页面
----------------------
触达的流程是：搜人 → 打开他的主页 → 挑一个作品 → 打开作品页 → 找评论输入框 → 发。

所以「拦媒体会不会搞坏功能」的关键不在首页，在**作品页的评论区**。
评论输入框是 DOM 元素，理论上不依赖视频数据，但必须实测——
「为了安静把功能搞坏」是最亏的改法。

用真实 profile 打开一个已知作品，两种配置各开一次，只看 DOM。
不登录、不搜人、不发任何东西。

    .venv/bin/python scripts/check_comment_box_media.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser  # noqa: E402

DOUYIN = "https://www.douyin.com"
# 触达历史里真实发过评论的作品（只打开来看，不发）
WORK = "7705634241514447225"


def probe(block_media: bool) -> dict:
    with Browser(pathlib.Path("data/outreach_profile"), headless=False,
                 block_media=block_media, offscreen=True) as (ctx, page):
        page.goto(f"{DOUYIN}/video/{WORK}", wait_until="domcontentloaded",
                  timeout=45000)
        page.wait_for_timeout(6000)

        info = page.evaluate(
            """() => {
                const txt = document.body ? document.body.innerText : '';
                const inputs = Array.from(document.querySelectorAll('textarea, input'));
                const contenteditable = document.querySelectorAll('[contenteditable="true"]');
                return {
                    title: document.title,
                    url: location.href,
                    bodyLen: txt.length,
                    hasCommentWord: txt.includes('评论'),
                    inputCount: inputs.length,
                    editableCount: contenteditable.length,
                    // 评论区的标志物
                    commentArea: !!document.querySelector('[class*="comment"]'),
                    videos: document.querySelectorAll('video').length,
                    allMuted: Array.from(document.querySelectorAll('video,audio'))
                              .every(v => v.muted === true),
                    playing: Array.from(document.querySelectorAll('video'))
                              .filter(v => !v.paused && v.currentTime > 0).length,
                };
            }"""
        )
        print(f"  {'拦媒体' if block_media else '不拦媒体'}: {info}")
        return info


def main() -> int:
    print("▶ 打开一个真实作品页，只看评论区 DOM，不发任何东西\n")
    a = probe(block_media=True)
    print()
    b = probe(block_media=False)

    print()
    print(f"拦媒体: body {a['bodyLen']} 字 / 输入框 {a['inputCount']} 个 / "
          f"可编辑 {a['editableCount']} 个 / 评论DOM {a['commentArea']}")
    print(f"不拦  : body {b['bodyLen']} 字 / 输入框 {b['inputCount']} 个 / "
          f"可编辑 {b['editableCount']} 个 / 评论DOM {b['commentArea']}")
    print(f"静音两组都生效: {'✅' if a['allMuted'] and b['allMuted'] else '❌'}")

    ok = (a["bodyLen"] > 500 and a["inputCount"] + a["editableCount"] > 0
          and b["bodyLen"] > 500 and b["inputCount"] + b["editableCount"] > 0)
    print(f"两组评论区都正常渲染: {'✅' if ok else '❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
