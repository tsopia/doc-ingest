"""SSE event generator for streaming responses."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from loguru import logger

from app.config import get_settings
from app.utils.sse_events import SSEEventType


class SSEEventGenerator:
    """SSE 事件生成器"""

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.start_time = time.time()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._should_heartbeat = False
        self._heartbeat_queue: Optional[asyncio.Queue] = None

    def create_event(
        self,
        event_type: str,
        message: str,
        progress: Optional[int] = None,
        data: Optional[dict] = None
    ) -> str:
        """
        创建 SSE 事件字符串

        Args:
            event_type: 事件类型
            message: 人类可读消息
            progress: 进度百分比 (0-100)
            data: 附加数据

        Returns:
            格式化的 SSE 事件字符串
        """
        event_data = {
            "type": event_type,
            "trace_id": self.trace_id,
            "timestamp": int(time.time() * 1000),
            "message": message,
        }

        if progress is not None:
            event_data["progress"] = progress

        if data:
            event_data["data"] = data

        return f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

    @asynccontextmanager
    async def stage(
        self,
        stage_name: str,
        message: str,
        progress_start: int,
        progress_end: int,
        enable_heartbeat: bool = False,
        heartbeat_threshold: int = 10,  # 触发心跳的最小耗时（秒）
        stage_data: Optional[dict] = None,  # 阶段完成时的附加数据
    ) -> AsyncGenerator[None, None]:
        """
        阶段上下文管理器，自动发送开始和完成事件

        Args:
            stage_name: 阶段名称（如 "converting"）
            message: 阶段消息
            progress_start: 起始进度
            progress_end: 结束进度
            enable_heartbeat: 是否启用心跳（True=立即启用, False=根据耗时自动决定）
            heartbeat_threshold: 触发心跳的最小耗时（秒），默认10秒
            stage_data: 阶段完成事件中包含的附加数据

        Yields:
            None
        """
        stage_start_time = time.monotonic()

        # 发送阶段开始事件
        if self._heartbeat_queue:
            await self._heartbeat_queue.put(
                self.create_event(
                    f"stage:{stage_name}",
                    message,
                    progress_start
                )
            )

        # 记录日志
        logger.info(
            "SSE stage START: stage={} message={} progress={} trace_id={}",
            stage_name,
            message,
            progress_start,
            self.trace_id
        )

        # 延迟启动心跳任务（等待 threshold 秒后再决定是否启用）
        delayed_heartbeat_task = None
        if enable_heartbeat and self._heartbeat_queue:
            async def delayed_heartbeat_starter():
                """延迟启动心跳任务"""
                await asyncio.sleep(heartbeat_threshold)
                # 如果阶段还未完成，启动心跳
                if not self._heartbeat_task:
                    self._should_heartbeat = True
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(progress_start, progress_end, stage_name)
                    )
                    logger.info(
                        "SSE heartbeat enabled: stage={} elapsed_threshold={}s trace_id={}",
                        stage_name,
                        heartbeat_threshold,
                        self.trace_id
                    )

            delayed_heartbeat_task = asyncio.create_task(delayed_heartbeat_starter())

        try:
            # 执行阶段任务
            yield
        finally:
            # 取消延迟启动任务（如果还未执行）
            if delayed_heartbeat_task and not delayed_heartbeat_task.done():
                delayed_heartbeat_task.cancel()
                try:
                    await delayed_heartbeat_task
                except asyncio.CancelledError:
                    pass

            # 停止心跳
            if self._heartbeat_task:
                self._should_heartbeat = False
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._heartbeat_task = None

            # 计算阶段耗时
            stage_elapsed_ms = int((time.monotonic() - stage_start_time) * 1000)

            # 构建完成事件的 data
            done_data = {"elapsed_ms": stage_elapsed_ms}
            if stage_data:
                done_data.update(stage_data)

            # 发送阶段完成事件
            if self._heartbeat_queue:
                await self._heartbeat_queue.put(
                    self.create_event(
                        f"stage:{stage_name}_done",
                        f"{message} - 完成",
                        progress_end,
                        done_data
                    )
                )

            # 记录日志
            logger.info(
                "SSE stage DONE: stage={} message={} progress={} elapsed_ms={} trace_id={}",
                stage_name,
                message,
                progress_end,
                stage_elapsed_ms,
                self.trace_id
            )

    async def _heartbeat_loop(self, progress_start: int, progress_end: int, stage_name: str):
        """
        心跳循环，定期发送心跳事件

        Args:
            progress_start: 起始进度
            progress_end: 结束进度
            stage_name: 阶段名称
        """
        settings = get_settings().sse
        interval = settings.heartbeat_interval
        elapsed = 0

        try:
            while self._should_heartbeat:
                await asyncio.sleep(interval)
                elapsed += interval

                if self._heartbeat_queue:
                    await self._heartbeat_queue.put(
                        self.create_event(
                            SSEEventType.HEARTBEAT,
                            "处理中...",  # 简化消息
                            None  # 不需要进度
                        )
                    )

                    logger.info(
                        "SSE heartbeat: stage={} elapsed={}s trace_id={}",
                        stage_name,
                        elapsed,
                        self.trace_id
                    )
        except asyncio.CancelledError:
            # 正常取消，不记录错误
            pass

    def set_queue(self, queue: asyncio.Queue):
        """设置事件队列"""
        self._heartbeat_queue = queue
