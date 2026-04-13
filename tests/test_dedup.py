"""快速验证：搜索去重 + 评论去重"""
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
        print("❌ Cookie 为空，请先配置")
        return

    collector = DouyinVideoCollector(cookie=cookie)

    # 测试关键词搜索
    print("🔍 测试搜索: 问界m6")
    videos = await collector._search_videos("问界m6", max_count=10)
    print(f"\n✅ 搜索结果: 共 {len(videos)} 个视频")

    # 去重验证
    aweme_ids = [v.get("aweme_id", "") for v in videos]
    unique_ids = set(aweme_ids)
    print(f"   去重后唯一 aweme_id: {len(unique_ids)}")
    if len(aweme_ids) != len(unique_ids):
        print(f"   ❌ 有重复! aweme_ids: {aweme_ids}")
    else:
        print(f"   ✅ 无重复 aweme_id")

    if videos:
        # 取第一个视频测试评论
        v = videos[0]
        aweme_id = v.get("aweme_id", "")
        title = v.get("desc", "")[:30]
        print(f"\n📝 测试评论抓取: {title} (id={aweme_id})")

        count = await collector._scrape_video_comments(
            aweme_id=aweme_id,
            video_title=title,
            video_author=v.get("author", {}).get("nickname", ""),
            task_id="test_dedup",
        )
        print(f"   抓取评论数: {count}")

    await collector.close()

    # 验证数据库
    db_path = "data/db/douyin_ppp.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM video_comments WHERE task_id='test_dedup'")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM video_comments WHERE task_id='test_dedup'")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT video_id) FROM video_comments WHERE task_id='test_dedup'")
    distinct_videos = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM video_comments WHERE task_id='test_dedup'")
    distinct_users = cur.fetchone()[0]
    cur.execute("SELECT video_id, user_id, content, COUNT(*) as cnt FROM video_comments WHERE task_id='test_dedup' GROUP BY video_id, user_id, content HAVING cnt > 1")
    dups = cur.fetchall()
    conn.close()

    print(f"\n📊 数据库验证:")
    print(f"   总记录: {total}")
    print(f"   唯一视频: {distinct_videos}")
    print(f"   唯一用户: {distinct_users}")
    print(f"   重复记录组: {len(dups)}")
    if dups:
        print(f"   ❌ 有重复! 前3组: {dups[:3]}")
    else:
        print(f"   ✅ 无重复记录")

if __name__ == "__main__":
    asyncio.run(test())
