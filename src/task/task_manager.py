"""异步抓取任务管理器 — 任务创建/进度追踪/取消/持久化"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional, Callable, Awaitable

from ..models.comment import ScrapeTask
from ..storage.db_storage import get_db
from ..utils.logger import get_logger

log = get_logger("task_manager")


class TaskManager:
    """
    异步抓取任务管理器。
    
    功能：
    - 创建/取消/查询抓取任务
    - 进度追踪与持久化
    - 并发控制（最多同时运行 N 个任务）
    """

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self._running_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cancel_flags: dict[str, bool] = {}  # task_id -> is_cancelled

    async def create_task(
        self,
        source: str,
        mode: str,
        params: str,
        runner: Callable[[str, str, str, "TaskManager"], Awaitable[int]],
    ) -> str:
        """
        创建并启动抓取任务。

        Args:
            source: 来源平台 (douyin / xiaohongshu)
            mode: 抓取模式 (keyword / account / video)
            params: JSON 参数
            runner: 异步执行函数 (task_id, source, params, task_manager) -> result_count

        Returns:
            任务 ID
        """
        task_id = uuid.uuid4().hex[:16]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 持久化到数据库
        scrape_task = ScrapeTask(
            id=task_id,
            source=source,
            mode=mode,
            params=params,
            status="pending",
            progress="{}",
            created_at=now,
            updated_at=now,
        )
        db = get_db()
        if db and db._db:
            await db.insert_scrape_task(scrape_task)

        # 启动异步任务
        atask = asyncio.create_task(
            self._run_task(task_id, source, mode, params, runner),
            name=f"scrape-{task_id}",
        )
        self._running_tasks[task_id] = atask
        self._cancel_flags[task_id] = False

        log.info(f"创建抓取任务: {task_id} ({source}/{mode})")
        return task_id

    async def _run_task(
        self,
        task_id: str,
        source: str,
        mode: str,
        params: str,
        runner: Callable,
    ) -> int:
        """执行抓取任务"""
        async with self._semaphore:
            db = get_db()
            try:
                # 更新状态为 running
                await self._update_status(task_id, "running")

                # 执行抓取
                result_count = await runner(task_id, source, params, self)

                # 检查是否被取消
                if self._cancel_flags.get(task_id, False):
                    await self._update_status(task_id, "cancelled")
                    log.info(f"任务 {task_id} 已取消")
                else:
                    await self._update_status(task_id, "completed", result_count=result_count)
                    log.info(f"任务 {task_id} 完成，抓取 {result_count} 条评论")

                return result_count

            except asyncio.CancelledError:
                await self._update_status(task_id, "cancelled")
                log.info(f"任务 {task_id} 被取消")
                return 0
            except Exception as e:
                log.error(f"任务 {task_id} 失败: {e}", exc_info=True)
                await self._update_status(task_id, "failed", error=str(e))
                return 0
            finally:
                self._running_tasks.pop(task_id, None)
                self._cancel_flags.pop(task_id, None)

    async def _update_status(
        self,
        task_id: str,
        status: str,
        progress: Optional[str] = None,
        result_count: Optional[int] = None,
        error: Optional[str] = None,
    ):
        """更新任务状态到数据库"""
        db = get_db()
        if db and db._db:
            await db.update_scrape_task(task_id, status=status, progress=progress, result_count=result_count, error=error)

    async def update_progress(self, task_id: str, found: int = 0, scraped: int = 0, total: int = 0):
        """更新任务进度（由采集器调用）"""
        progress = json.dumps({"found": found, "scraped": scraped, "total": total})
        await self._update_status(task_id, "running", progress=progress)

    def is_cancelled(self, task_id: str) -> bool:
        """检查任务是否已被取消"""
        return self._cancel_flags.get(task_id, False)

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        self._cancel_flags[task_id] = True
        atask = self._running_tasks.get(task_id)
        if atask and not atask.done():
            atask.cancel()
            try:
                await atask
            except asyncio.CancelledError:
                pass
        await self._update_status(task_id, "cancelled")
        log.info(f"已取消任务: {task_id}")
        return True

    async def get_task_info(self, task_id: str) -> Optional[dict]:
        """获取任务信息"""
        db = get_db()
        if db and db._db:
            return await db.get_scrape_task(task_id)
        return None

    async def list_tasks(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """查询任务列表"""
        db = get_db()
        if db and db._db:
            return await db.query_scrape_tasks(source=source, status=status, limit=limit, offset=offset)
        return [], 0

    async def delete_task(self, task_id: str) -> bool:
        """删除任务记录"""
        # 先取消运行中的任务
        if task_id in self._running_tasks:
            await self.cancel_task(task_id)
        db = get_db()
        if db and db._db:
            return await db.delete_scrape_task(task_id)
        return False

    async def close(self):
        """关闭任务管理器，取消所有运行中任务"""
        for task_id in list(self._running_tasks.keys()):
            await self.cancel_task(task_id)
        log.info("任务管理器已关闭")


# 全局单例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager


async def close_task_manager():
    global _task_manager
    if _task_manager:
        await _task_manager.close()
        _task_manager = None
