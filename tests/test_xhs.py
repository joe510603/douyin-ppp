"""XHS 端到端验证 — 测试 Playwright 拦截 + 搜索 + 评论抓取"""
import asyncio
import sys
sys.path.insert(0, '.')

from src.config import get_config, reload_config
from src.collector.xhs_comment_collector import XhsCommentCollector
from src.storage.db_storage import get_db, init_database
from src.utils.xhs_cookie_manager import fetch_xhs_cookie_by_browser, save_xhs_cookie
import sqlite3


async def test():
    await init_database()
    config = get_config()

    # Step 1: 尝试从配置获取 cookie
    cookie = config.xhs.cookie
    if not cookie:
        print("⚠️ config.xhs.cookie 为空，尝试通过 Playwright 获取...")
        cookie = await asyncio.to_thread(fetch_xhs_cookie_by_browser)
        if cookie and "web_session" in cookie and "a1" in cookie:
            save_xhs_cookie(cookie)
            reload_config()
            config = get_config()
            cookie = config.xhs.cookie
            print(f"✅ Playwright 获取 Cookie 成功，长度: {len(cookie)}")
        else:
            print("❌ Playwright 未获取到有效 Cookie")
            return

    # Step 2: 测试搜索
    print("\n🔍 测试 XHS 搜索...")
    collector = XhsCommentCollector(cookie=cookie)

    kw = "问界m6"
    notes = await collector._search_notes(kw, max_count=5)
    print(f"\n✅ 搜索结果: {len(notes)} 条笔记")

    if notes:
        for n in notes[:3]:
            nid = n.get("note_id", "") or n.get("id", "")
            title = n.get("display_title", "") or n.get("title", "")[:40]
            print(f"  {nid}: {title}")

    # Step 3: 测试评论抓取
    if notes:
        note = notes[0]
        note_id = note.get("note_id", "") or note.get("id", "")
        title = note.get("display_title", "") or note.get("title", "")[:30]
        author = ""
        if note.get("user"):
            author = note["user"].get("nickname", "")

        print(f"\n📝 测试评论抓取: {title} (id={note_id})")
        cnt = await collector._scrape_note_comments(
            note_id=note_id,
            note_title=title,
            note_author=author,
            task_id="test_xhs",
            source_keyword=kw,
        )
        print(f"   抓取评论数: {cnt}")

    await collector.close()

    # Step 4: 验证数据库
    db_path = "data/db/douyin_ppp.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT video_id) FROM video_comments WHERE task_id='test_xhs' AND source='xiaohongshu'")
    row = cur.fetchone()
    conn.close()
    print(f"\n📊 DB验证: 总{row[0]}条 / {row[1]}视频")
    if row[0] > 0:
        print("✅ XHS 抓取验证成功！")
    else:
        print("❌ 无数据入库")


if __name__ == "__main__":
    asyncio.run(test())
