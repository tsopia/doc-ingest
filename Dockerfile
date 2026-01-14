# ========================
# 第一阶段：构建 wheel
# ========================
FROM ghcr.io/astral-sh/uv:python3.11-bookworm AS builder

WORKDIR /app

# 复制 pyproject.toml、uv.lock、README 和源码
COPY pyproject.toml uv.lock README.md ./
COPY app ./app

# 同步依赖并构建 wheel
RUN uv sync --frozen --no-dev \
    && uv build --wheel --out-dir ./dist

# ========================
# 第二阶段：生产运行镜像
# ========================
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖（ffmpeg）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 复制 builder 阶段生成的 wheel 并安装
COPY --from=builder /app/dist/*.whl ./dist/
RUN pip install --no-cache-dir dist/*.whl \
    && rm -rf dist/*.whl

# 暴露端口
EXPOSE 80

# 多进程启动 uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "1"]
