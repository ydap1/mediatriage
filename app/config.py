import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    tmdb_api_key: str = field(default_factory=lambda: os.environ.get("TMDB_API_KEY", ""))
    tmdb_image_base: str = field(
        default_factory=lambda: os.environ.get("TMDB_IMAGE_BASE", "https://image.tmdb.org/t/p/w342")
    )
    openrouter_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", "")
    )
    openrouter_model: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_MODEL", "openai/gpt-5.4-nano")
    )
    enable_ai_tags: bool = field(
        default_factory=lambda: os.environ.get("ENABLE_AI_TAGS", "false").lower() == "true"
    )
    db_path: str = field(
        default_factory=lambda: os.environ.get("DB_PATH", "/data/mediatriage.db")
    )
    app_password: str = field(
        default_factory=lambda: os.environ.get("APP_PASSWORD", "changeme")
    )
    secret_key: str = field(
        default_factory=lambda: os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    )
    session_max_age: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_MAX_AGE", "2592000"))
    )
    max_adds_per_hour: int = field(
        default_factory=lambda: int(os.environ.get("MAX_ADDS_PER_HOUR", "20"))
    )


settings = Settings()
