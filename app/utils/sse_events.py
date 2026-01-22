"""SSE event types and utilities for streaming responses."""

from enum import Enum


class SSEEventType:
    """SSE 事件类型常量"""

    # 流程控制事件
    STARTED = "started"
    PROGRESS = "progress"
    HEARTBEAT = "heartbeat"
    COMPLETE = "complete"
    ERROR = "error"

    # 模型流式输出
    MODEL_CHUNK = "model_chunk"

    # 阶段事件
    STAGE_CONVERTING = "stage:converting"
    STAGE_CONVERTING_DONE = "stage:converting_done"

    STAGE_EXTRACTING = "stage:extracting"
    STAGE_EXTRACTING_DONE = "stage:extracting_done"

    STAGE_UPLOADING = "stage:uploading"
    STAGE_UPLOADING_DONE = "stage:uploading_done"

    STAGE_MODEL_PROCESSING = "stage:model_processing"
    STAGE_MODEL_PROCESSING_DONE = "stage:model_processing_done"

    STAGE_STRUCTURING = "stage:structuring"
    STAGE_STRUCTURING_DONE = "stage:structuring_done"


class SSEStage(str, Enum):
    """处理阶段枚举"""
    CONVERTING = "converting"
    EXTRACTING = "extracting"
    UPLOADING = "uploading"
    MODEL_PROCESSING = "model_processing"
    STRUCTURING = "structuring"
