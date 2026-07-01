"""应用配置入口。

所有环境变量都通过 Settings 读取，API 和 Worker 共用同一套配置，避免本地开发、
测试和云端部署时出现“两个服务读到不同配置”的问题。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    """从项目根目录 .env 加载的运行时配置。"""

    app_env: str = "local"
    database_url: str
    redis_url: str
    agent_task_queue: str = "novelforge:agent_tasks"
    s3_endpoint: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    llm_provider: str = "openai_compatible"
    llm_base_url: str = ""
    llm_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """缓存配置对象，避免每次依赖注入都重复解析 .env。"""
    return Settings()


settings = get_settings()
