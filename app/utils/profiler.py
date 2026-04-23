"""
请求级性能 profiler

在文档处理的每个阶段记录内存快照（RSS/VMS）和耗时，
最终输出结构化的 profiling 报告用于分析。

用法：
    profiler = RequestProfiler(trace_id="xxx")
    profiler.start()

    profiler.stage_begin("downloading")
    # ... do work ...
    profiler.stage_end("downloading")

    report = profiler.finish()
    # report 是一个 dict，包含每个阶段的耗时和内存变化
"""

import os
import time
from dataclasses import dataclass, field
from typing import Any

import psutil
from loguru import logger


@dataclass
class MemSnapshot:
    """内存快照"""
    rss_mb: float
    vms_mb: float
    timestamp: float  # monotonic

    @staticmethod
    def capture(proc: psutil.Process) -> "MemSnapshot":
        mem = proc.memory_info()
        return MemSnapshot(
            rss_mb=round(mem.rss / 1024 / 1024, 2),
            vms_mb=round(mem.vms / 1024 / 1024, 2),
            timestamp=time.monotonic(),
        )


@dataclass
class StageRecord:
    """单阶段记录"""
    name: str
    begin_time: float = 0.0
    end_time: float = 0.0
    begin_mem: MemSnapshot | None = None
    end_mem: MemSnapshot | None = None
    extra: dict = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> int:
        if self.end_time and self.begin_time:
            return int((self.end_time - self.begin_time) * 1000)
        return 0

    @property
    def rss_delta_mb(self) -> float:
        if self.begin_mem and self.end_mem:
            return round(self.end_mem.rss_mb - self.begin_mem.rss_mb, 2)
        return 0.0


class RequestProfiler:
    """
    请求级 profiler

    在每个阶段的 begin/end 时刻采集内存快照，
    finish() 时输出完整的阶段耗时 + 内存变化报告。
    """

    def __init__(self, trace_id: str = "") -> None:
        self._trace_id = trace_id
        self._proc = psutil.Process(os.getpid())
        self._start_time: float = 0.0
        self._start_mem: MemSnapshot | None = None
        self._peak_rss_mb: float = 0.0
        self._stages: list[StageRecord] = []
        self._current_stage: StageRecord | None = None

    def start(self) -> None:
        """开始 profiling"""
        self._start_time = time.monotonic()
        self._start_mem = MemSnapshot.capture(self._proc)
        self._peak_rss_mb = self._start_mem.rss_mb

    def stage_begin(self, name: str) -> None:
        """标记阶段开始"""
        mem = MemSnapshot.capture(self._proc)
        self._update_peak(mem.rss_mb)
        self._current_stage = StageRecord(
            name=name,
            begin_time=time.monotonic(),
            begin_mem=mem,
        )

    def stage_end(self, name: str, **extra) -> None:
        """标记阶段结束"""
        mem = MemSnapshot.capture(self._proc)
        self._update_peak(mem.rss_mb)

        if self._current_stage and self._current_stage.name == name:
            self._current_stage.end_time = time.monotonic()
            self._current_stage.end_mem = mem
            self._current_stage.extra = extra
            self._stages.append(self._current_stage)

            logger.info(
                "profiler: stage={} elapsed_ms={} rss_begin={:.1f}MB rss_end={:.1f}MB "
                "rss_delta={:+.1f}MB trace_id={}",
                name,
                self._current_stage.elapsed_ms,
                self._current_stage.begin_mem.rss_mb,
                mem.rss_mb,
                self._current_stage.rss_delta_mb,
                self._trace_id,
            )
            self._current_stage = None
        else:
            # 没有匹配的 begin，单独记录一次快照
            record = StageRecord(
                name=name,
                begin_time=time.monotonic(),
                end_time=time.monotonic(),
                begin_mem=mem,
                end_mem=mem,
                extra=extra,
            )
            self._stages.append(record)

    def finish(self) -> dict[str, Any]:
        """
        结束 profiling，返回完整报告并以日志输出摘要。

        Returns:
            包含每个阶段耗时、内存变化、峰值 RSS 等信息的 dict
        """
        end_time = time.monotonic()
        end_mem = MemSnapshot.capture(self._proc)
        self._update_peak(end_mem.rss_mb)

        total_ms = int((end_time - self._start_time) * 1000)

        stages_summary = []
        for s in self._stages:
            entry = {
                "stage": s.name,
                "elapsed_ms": s.elapsed_ms,
                "rss_begin_mb": s.begin_mem.rss_mb if s.begin_mem else None,
                "rss_end_mb": s.end_mem.rss_mb if s.end_mem else None,
                "rss_delta_mb": s.rss_delta_mb,
            }
            if s.extra:
                entry["extra"] = s.extra
            stages_summary.append(entry)

        report = {
            "trace_id": self._trace_id,
            "total_elapsed_ms": total_ms,
            "rss_start_mb": self._start_mem.rss_mb if self._start_mem else None,
            "rss_end_mb": end_mem.rss_mb,
            "rss_peak_mb": self._peak_rss_mb,
            "stages": stages_summary,
        }

        # 结构化日志输出摘要
        stage_lines = []
        for s in stages_summary:
            line = (
                f"  {s['stage']:<20s}  {s['elapsed_ms']:>7d}ms  "
                f"RSS {s['rss_begin_mb']:.1f} → {s['rss_end_mb']:.1f} MB  "
                f"(Δ{s['rss_delta_mb']:+.1f})"
            )
            stage_lines.append(line)

        logger.info(
            "profiler: request complete trace_id={}\n"
            "  total_elapsed={}ms  peak_rss={:.1f}MB  "
            "rss_start={:.1f}MB → rss_end={:.1f}MB\n{}",
            self._trace_id,
            total_ms,
            self._peak_rss_mb,
            self._start_mem.rss_mb if self._start_mem else 0,
            end_mem.rss_mb,
            "\n".join(stage_lines),
        )

        return report

    def _update_peak(self, rss_mb: float) -> None:
        if rss_mb > self._peak_rss_mb:
            self._peak_rss_mb = rss_mb
