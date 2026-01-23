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
            "你是专业的文档处理助手。你的任务是处理包含图片占位符的 markdown 文本。\n\n"
            "核心原则：\n"
            "1. 【保留语义】：保持原有内容的完整性和逻辑结构，严格保留正文文本。\n"
            "2. 【智能清洗】：识别并移除文档转换产生的噪音，如页眉、页脚、页码（如 'Page 1 of 10'）、水印残留、无意义的分隔符或乱码字符。\n"
            "3. 【仅处理图片】：将图片占位符（image://img_n）替换为对应图片的文字描述。\n\n"
            "输入格式：\n"
            "- 原始文本中图片位置用 image://img_n 占位符标记\n"
            "- 对应图片会按顺序提供\n\n"
            "输出格式：\n"
            "- 输出处理后的 Markdown，正文内容保持不变（除清洗噪音外）\n"
            "- 图片占位符替换为图片内容的文字描述"
        ),
    )
    task_prompt: str = Field(
        default=(
            "请处理以下内容，要求：\n\n"
            "1. **智能清洗噪音**：\n"
            "   - 删除所有页码（如 '1 / 20', '- 5 -' 等）\n"
            "   - 删除页眉页脚信息（如文件名、日期、公司机密声明等出现在页边缘的内容）\n"
            "   - 删除文档转换产生的无意义字符或乱码\n"
            "   - **注意**：必须仔细辨别，严禁误删正文内容、章节标题或正文中的数字列表\n\n"
            "2. **严格保留原文**：除上述清洗外，保持原有文字内容、段落结构、标题层级完全不变\n\n"
            "3. **图片描述替换**：将 image://img_n 占位符替换为图片内容描述：\n"
            "   - 简单示意/装饰图：1-2句概括\n"
            "   - UI截图：描述界面主体功能和关键元素\n"
            "   - 流程图/架构图：用列表描述主要节点和关系\n"
            "   - 表格/数据图：尽量以文字表格还原数据\n"
            "   - 若图片无法识别，标注 [图片内容不可识别]\n\n"
            "4. **禁止改写**：不要修改原文措辞，不要重新组织段落，不要添加总结\n\n"
            "5. **纯净输出**：直接输出处理后的 Markdown，不要使用代码块包裹，不要额外解释"
        ),
    )
    chunk_max_tokens: int = Field(
        default=8000,
        ge=1000,
        description="每个 chunk 的最大 tokens（文本切分）",
    )
    chunk_overlap_tokens: int = Field(
        default=200,
        ge=0,
        description="chunk 间的重叠 tokens（用于上下文）",
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


class LangfuseSettings(BaseModel):
    """Langfuse 可观测性配置（可选，配置密钥即自动启用）"""
    public_key: str = Field(
        default="",
        description="Langfuse Public Key",
    )
    secret_key: str = Field(
        default="",
        description="Langfuse Secret Key",
    )
    host: str = Field(
        default="https://cloud.langfuse.com",
        description="Langfuse Host URL",
    )


class Settings(BaseSettings):
    model: ModelSettings = ModelSettings()
    storage: StorageSettings = StorageSettings()
    download: DownloadSettings = DownloadSettings()
    log: LogSettings = LogSettings()
    sse: SSESettings = SSESettings()
    langfuse: LangfuseSettings = LangfuseSettings()

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
