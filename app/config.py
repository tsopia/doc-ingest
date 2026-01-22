from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "DOC_INGEST__"


class ModelSettings(BaseModel):
    api_key: str = Field(
        default="",
    )
    base_url: str = Field(
        default="",
    )
    model_name: str = Field(
        default="gpt-4o",
    )
    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
    )
    timeout_seconds: int = Field(
        default=120,
        ge=10,
        description="模型调用超时时间（秒）",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="模型调用最大重试次数",
    )
    retry_min_wait: int = Field(
        default=1,
        ge=1,
        description="重试最小等待时间（秒）",
    )
    retry_max_wait: int = Field(
        default=10,
        ge=1,
        description="重试最大等待时间（秒）",
    )
    max_input_tokens: int = Field(
        default=25000,
        ge=1000,
        description="模型输入最大 tokens，超过此值将拒绝请求",
    )
    system_prompt: str = Field(
        default=(
            "你是专业的文档整理助手。你的任务是将原始 markdown 文本整理为结构清晰、内容完整的文档。\n\n"
            "输入说明：\n"
            "- 原始文本中图片位置用 image://img_n 占位符标记\n"
            "- 对应图片会按顺序提供\n\n"
            "输出要求：\n"
            "- 结合文本与图片内容，输出整理后的 Markdown\n"
            "- 图片内容需转化为文字描述，融入上下文，不保留占位符"
        ),
    )
    task_prompt: str = Field(
        default=(
            "请将内容整理为结构清晰的 Markdown，要求：\n\n"
            "1. **信息保真**：保持原有信息不丢失，不新增虚构内容\n\n"
            "2. **图片处理**：根据图片类型采用适当描述，完全替换占位符：\n"
            "   - 简单示意/装饰图：1-2句概括\n"
            "   - UI截图：描述界面主体功能和关键元素\n"
            "   - 流程图/架构图：用列表或分点描述主要节点和关系\n"
            "   - 表格/数据图：尽量以文字表格还原数据\n"
            "   - 若图片无法识别，标注 [图片内容不可识别]\n\n"
            "3. **结构优化**：优化段落层级与标题（必要时补充小标题）\n\n"
            "4. **表格保留**：表格保持 Markdown 表格格式\n\n"
            "5. **纯净输出**：直接输出 Markdown，不要使用代码块包裹，不要额外解释"
        ),
    )


class StorageSettings(BaseModel):
    provider: str = Field(
        default="oss",
        description="存储提供商: oss, s3, minio, cos",
    )
    endpoint: str = Field(
        default="",
        description="存储服务端点",
    )
    access_key_id: str = Field(
        default="",
        description="访问密钥ID",
    )
    access_key_secret: str = Field(
        default="",
        description="访问密钥",
    )
    bucket: str = Field(
        default="",
        description="存储桶名称",
    )
    region: str = Field(
        default="",
        description="区域（S3/COS需要）",
    )
    prefix: str = Field(
        default="doc-ingest/",
        description="对象前缀",
    )
    secure: bool = Field(
        default=True,
        description="是否使用HTTPS",
    )
    url_ttl_seconds: int = Field(
        default=1800,
        ge=0,
        description="签名URL过期时间（秒）",
    )


class DownloadSettings(BaseModel):
    timeout_seconds: int = Field(
        default=30,
        ge=1,
    )



class LogSettings(BaseModel):
    level: str = Field(
        default="INFO",
    )


class SSESettings(BaseModel):
    heartbeat_interval: int = Field(
        default=5,
        ge=1,
        le=30,
        description="心跳间隔（秒），用于长时间处理阶段"
    )
    long_stage_threshold: int = Field(
        default=10,
        ge=5,
        description="触发心跳的最小阶段耗时（秒）"
    )


class Settings(BaseSettings):
    model: ModelSettings = ModelSettings()
    storage: StorageSettings = StorageSettings()
    download: DownloadSettings = DownloadSettings()
    log: LogSettings = LogSettings()
    sse: SSESettings = SSESettings()

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
