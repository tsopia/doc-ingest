"""
可观测性工具（Langfuse 可选集成）

根据配置决定是否启用 Langfuse：
- 未配置：使用原生 OpenAI 客户端 + 透传装饰器
- 已配置：使用 Langfuse 包装的客户端 + 监控装饰器
"""

import inspect
from functools import wraps
from typing import Any, Callable, TypeVar

from loguru import logger

F = TypeVar('F', bound=Callable[..., Any])

# Langfuse 单例实例缓存
_langfuse_client = None


def _get_langfuse_settings():
    """获取 Langfuse 配置"""
    from app.config import get_settings
    return get_settings().langfuse


def _get_langfuse():
    """
    获取 Langfuse 客户端单例
    
    Returns:
        Langfuse 实例，未配置或初始化失败时返回 None
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client
    
    try:
        settings = _get_langfuse_settings()
        if not (settings.public_key and settings.secret_key):
            logger.debug("Langfuse disabled: credentials not configured")
            return None
        
        from langfuse import Langfuse
        
        logger.info(f"Initializing Langfuse client: host={settings.host}")
        _langfuse_client = Langfuse(
            public_key=settings.public_key,
            secret_key=settings.secret_key,
            host=settings.host,
            timeout=60,  # 增加超时到 60 秒（高延迟网络）
            flush_interval=5,  # 每 5 秒刷新一次（更频繁上传）
        )
        logger.info(f"Langfuse client initialized: host={settings.host}")
        return _langfuse_client
    except Exception as e:
        logger.error(
            f"Failed to initialize Langfuse client: {type(e).__name__}: {e}\n"
            f"Host: {settings.host if 'settings' in locals() else 'unknown'}"
        )
        return None


def flush_langfuse():
    """刷新 Langfuse 数据（用于 shutdown）"""
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
            logger.info("Langfuse data flushed")
        except Exception as e:
            logger.error(f"Failed to flush Langfuse data: {type(e).__name__}: {e}")


def check_langfuse_connectivity_async():
    """
    异步检查 Langfuse 连接性（后台运行，不阻塞启动）
    
    在后台线程中验证网络连接，记录诊断信息但不影响服务启动
    """
    import threading
    
    def _check():
        import time
        time.sleep(2)  # 等待服务完全启动
        
        client = _get_langfuse()
        if client is None:
            logger.info("Langfuse connectivity check skipped: client not initialized")
            return
        
        try:
            settings = _get_langfuse_settings()
            logger.info(f"Background: Testing Langfuse connectivity to {settings.host}...")
            
            # 发送测试 trace
            test_trace = client.trace(name="startup-connectivity-test")
            test_trace.update(output={"status": "background_check", "timestamp": time.time()})
            client.flush()
            
            logger.info("✓ Langfuse connectivity verified in background")
        except Exception as e:
            logger.warning(
                f"Background Langfuse connectivity test failed: {type(e).__name__}: {e}\n"
                f"Note: This may be due to high latency. If traces appear in Langfuse dashboard later, ignore this warning.\n"
                f"If no traces appear, check:\n"
                f"  - Network: curl -I {settings.host}/api/public/health\n"
                f"  - DNS: nslookup {settings.host.replace('https://', '').replace('http://', '')}"
            )
    
    thread = threading.Thread(target=_check, daemon=True)
    thread.start()


def observe(name: str = None, **kwargs) -> Callable[[F], F]:
    """
    条件装饰器：根据配置决定是否启用 Langfuse 监控

    Args:
        name: 可观测性追踪名称
        **kwargs: 传递给 Langfuse observe 的其他参数

    Returns:
        装饰器函数
    """
    # 检查 Langfuse 是否可用
    if _get_langfuse() is None:
        return lambda func: func
    
    try:
        from langfuse import observe as lf_observe
    except ImportError:
        logger.warning("Langfuse package not installed, using passthrough")
        return lambda func: func

    def wrapper(func: F) -> F:
        decorated = lf_observe(name=name, **kwargs)(func)
        func_name = func.__name__

        if inspect.isasyncgenfunction(func):
            @wraps(func)
            async def async_gen_wrapper(*args, **kw):
                async for item in decorated(*args, **kw):
                    yield item
            return async_gen_wrapper

        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kw):
                return await decorated(*args, **kw)
            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kw):
            return decorated(*args, **kw)
        return sync_wrapper

    return wrapper


def get_openai_client(**kwargs):
    """
    获取 OpenAI 客户端：根据配置决定是否使用 Langfuse 包装

    Args:
        **kwargs: 传递给 OpenAI 客户端的参数（api_key, base_url 等）

    Returns:
        OpenAI 客户端实例
    """
    if _get_langfuse() is not None:
        try:
            from langfuse.openai import OpenAI as LangfuseOpenAI
            logger.debug("Using Langfuse-wrapped OpenAI client")
            return LangfuseOpenAI(**kwargs)
        except ImportError:
            logger.warning("Langfuse package not installed, using native OpenAI client")

    from openai import OpenAI
    logger.debug("Using native OpenAI client")
    return OpenAI(**kwargs)
