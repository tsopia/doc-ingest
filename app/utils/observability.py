"""
可观测性工具（Langfuse 可选集成）

根据配置决定是否启用 Langfuse：
- 未配置：使用原生 OpenAI 客户端 + 透传装饰器
- 已配置：使用 Langfuse 包装的客户端 + 监控装饰器
"""

from typing import Any, Callable, TypeVar
from functools import wraps

from loguru import logger

F = TypeVar('F', bound=Callable[..., Any])


def _langfuse_enabled() -> bool:
    """检查是否启用 Langfuse（根据密钥是否配置）"""
    try:
        from app.config import get_settings
        settings = get_settings().langfuse
        enabled = bool(settings.public_key and settings.secret_key)
        if enabled:
            logger.debug("Langfuse observability enabled")
        return enabled
    except Exception as e:
        logger.warning(f"Failed to check Langfuse settings: {e}")
        return False


def observe(name: str = None, **kwargs) -> Callable[[F], F]:
    """
    条件装饰器：根据配置决定是否启用 Langfuse 监控

    Args:
        name: 可观测性追踪名称
        **kwargs: 传递给 Langfuse observe 的其他参数

    Returns:
        装饰器函数
    """
    if _langfuse_enabled():
        try:
            from langfuse import observe as lf_observe
            return lf_observe(name=name, **kwargs)
        except ImportError:
            logger.warning("Langfuse enabled but package not installed, using passthrough")

    # 未启用或导入失败：透传装饰器
    def passthrough(func: F) -> F:
        return func

    return passthrough


def get_openai_client(**kwargs):
    """
    获取 OpenAI 客户端：根据配置决定是否使用 Langfuse 包装

    Args:
        **kwargs: 传递给 OpenAI 客户端的参数（api_key, base_url 等）

    Returns:
        OpenAI 客户端实例
    """
    if _langfuse_enabled():
        try:
            from langfuse.openai import OpenAI as LangfuseOpenAI
            logger.debug("Using Langfuse-wrapped OpenAI client")
            return LangfuseOpenAI(**kwargs)
        except ImportError:
            logger.warning("Langfuse enabled but package not installed, using native OpenAI client")

    # 未启用或导入失败：使用原生 OpenAI 客户端
    from openai import OpenAI
    logger.debug("Using native OpenAI client")
    return OpenAI(**kwargs)
