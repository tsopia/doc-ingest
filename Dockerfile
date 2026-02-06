# ==========================================
# 阶段 1: 构建阶段 (构建虚拟环境)
# ==========================================
FROM registry.cn-hangzhou.aliyuncs.com/synocodes-qa/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# 1. 首先安装依赖 (利用缓存层)
# 只复制 lock 文件，确保这一层只有在依赖变更时才重新构建
COPY pyproject.toml uv.lock ./

# 使用缓存挂载加速后续构建
# --no-install-project: 只安装依赖，暂不安装项目本身
# --frozen: 严格按照 lock 文件安装，不自动更新
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2. 安装项目本身 (非缓存层)
# 这一层在代码变更时会重新构建，但非常快 (无需下载)
# ⚠️ 注意: 必须复制 README.md，因为 pyproject.toml 中配置了 [project] readme = "README.md"
# 如果缺少该文件，uv sync 安装本项目时会报错
COPY README.md ./
COPY app ./app

# 将项目本身安装到环境中
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ==========================================
# 阶段 2: 生产环境 (精简 Runtime)
# ==========================================
FROM registry.cn-hangzhou.aliyuncs.com/synocodes-qa/python:3.11-slim-bookworm

WORKDIR /app

# 1. 安装系统依赖 (ffmpeg)
# 这一层会被长久缓存，除非 apt 参数变更
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. 从 builder 阶段复制构建好的虚拟环境
# 这里包含了所有依赖 + 已安装的项目代码 + 二进制文件
# 完全离线，无需再运行 pip install
COPY --from=builder /app/.venv /app/.venv

# 3. 复制应用代码
# 虽然 .venv 里已经安装了包，但通常还需要源代码文件来支持运行时的资源加载等
COPY app ./app

# 4. 设置环境变量使用 .venv
ENV PATH="/app/.venv/bin:$PATH"

# 暴露端口
EXPOSE 80

# 使用 .venv 中的 uvicorn 运行
# 由于我们已经安装了项目，app.main 可以被正确导入
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "1"]
