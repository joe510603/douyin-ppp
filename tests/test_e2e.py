"""快速端到端验证"""
import asyncio
import sys
sys.path.insert(0, '.')

from src.config import get_config
from src.collector.video_comment_collector import DouyinVideoCollector
from src.storage.db_storage import get_db, init_database
import sqlite3

async def test():
    await init_database()
    config = get_config()
    cookie = config.douyin_video.cookie or config.douyin.cookie
    if not cookie:
        print("❌ Cookie 为空")
        return

    collector = DouyinVideoCollector(cookie=cookie)

    # 搜索多个视频
    print("🔍 搜索: 问界m6")
    videos = await collector._search_videos("问界m6", max_count=5)
    print(f"✅ 搜索到 {len(videos)} 个视频:")
    seen_ids = set()
    for v in videos:
        vid = v.get("aweme_id", "")
        seen_ids.add(vid)
        print(f"  {vid}: {v.get('desc','')[:40]}")
    print(f"   唯一 aweme_id: {len(seen_ids)}")

    if videos:
        total = 0
        for i, v in enumerate(videos[:3]):  # 只取前3个视频
            aweme_id = v.get("aweme_id", "")
            title = v.get("desc", "")[:30]
            print(f"\n📝 [{i+1}] {title} (id={aweme_id})")
            cnt = await collector._scrape_video_comments(
                aweme_id=aweme_id,
                video_title=title,
                video_author=v.get("author", {}).get("nickname", ""),
                task_id="test_e2e",
            )
            print(f"   ✅ 抓取 {cnt} 条评论")
            total += cnt
            await asyncio.sleep(2)

    await collector.close()

    # 验证
    db_path = "data/db/douyin_ppp.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT video_id), COUNT(DISTINCT user_id) FROM video_comments WHERE task_id='test_e2e'")
    row = cur.fetchone()
    cur.execute("SELECT video_id, COUNT(*) FROM video_comments WHERE task_id='test_e2e' GROUP BY video_id")
    by_video = cur.fetchall()
    conn.close()
    print(f"\n📊 DB验证: 总{row[0]}条 / {row[1]}视频 / {row[2]}用户")
    for vid, cnt in by_video:
        print(f"   {vid}: {cnt}条")

if __name__ == "__main__":
    asyncio.run(test())
