"""Central configuration. The only module allowed to read environment variables (R10)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # app
    app_env: str = "dev"
    app_base_url: str = "http://localhost:8000"
    api_prefix: str = "/api/v1"
    frontend_origin: str = "http://localhost:5173"
    log_level: str = "INFO"
    log_dir: Path = BASE_DIR / "logs"

    # storage
    database_url: str = f"sqlite:///{(BASE_DIR / 'hr_agent.db').as_posix()}"
    checkpoint_db: Path = BASE_DIR / "checkpoints.sqlite"
    upload_dir: Path = BASE_DIR / "uploads"
    max_upload_mb: int = Field(default=10, ge=1, le=100)

    # auth
    jwt_secret: str = "dev-secret-change-me-before-production-32chars"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=480, ge=5, le=10080)
    hr_admin_email: str = "hr@example.com"
    hr_admin_password: str = "change-me-hr"
    hr_manager_email: str = "manager@example.com"
    hr_manager_password: str = "change-me-manager"
    ceo_email: str = "ceo@example.com"
    ceo_password: str = "change-me-ceo"

    # internal service auth (Kestra -> backend)
    # Deliberately separate from the user JWT. Empty means all internal calls are rejected.
    internal_service_token: str = ""

    # llm
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    llm_tier: str = "free"
    llm_timeout_seconds: int = Field(default=60, ge=5, le=300)
    llm_min_interval_seconds: float = Field(default=4.0, ge=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    prompt_version: str = "v1"

    # kestra orchestration
    kestra_url: str = "http://localhost:8080"
    # Kestra OSS 1.3 uses HTTP Basic auth. The token is kept for deployments
    # that expose an Enterprise API token; basic auth wins when both are set.
    kestra_api_username: str = ""
    kestra_api_password: str = ""
    kestra_api_token: str = ""
    kestra_namespace: str = "hr.recruitment"
    kestra_flow_id: str = "hr_screening_and_decisions"
    kestra_timeout_seconds: int = Field(default=30, ge=5, le=120)
    kestra_max_retries: int = Field(default=2, ge=0, le=10)
    kestra_retry_base_seconds: float = Field(default=0.5, ge=0, le=30)
    # Kestra is the orchestrator and all LLM reasoning runs as Kestra AI tasks.
    use_kestra_llm: bool = True

    # email
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_timeout_seconds: int = 30
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_reply_to: str = ""
    smtp_timeout_seconds: int = 30
    email_dry_run: bool = True

    # mcp email server
    mcp_email_url: str = "https://mcpemails.com/api/mcp"
    mcp_email_token: str = ""
    mcp_inbox_email: str = ""

    # routing / policy
    hr_manager_notify_email: str = "manager@example.com"
    ceo_report_email: str = "ceo@example.com"
    alert_email: str = ""
    alert_webhook_url: str = ""
    default_min_score: int = Field(default=60, ge=0, le=100)
    resume_min_chars: int = Field(default=200, ge=0)
    retention_days: int = Field(default=180, ge=1)
    scheduler_enabled: bool = False
    poll_interval_minutes: int = Field(default=15, ge=1)

    @field_validator("jwt_secret")
    @classmethod
    def _secret_long_enough(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return level

    @field_validator("jwt_algorithm")
    @classmethod
    def _valid_algorithm(cls, value: str) -> str:
        if value not in {"HS256", "HS384", "HS512"}:
            raise ValueError("JWT_ALGORITHM must be HS256, HS384 or HS512")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def seed_accounts(self) -> list[dict[str, str]]:
        """Accounts seeded idempotently on startup (G1)."""
        return [
            {
                "email": self.hr_admin_email.lower(),
                "password": self.hr_admin_password,
                "role": "hr",
                "full_name": "HR Admin",
            },
            {
                "email": self.hr_manager_email.lower(),
                "password": self.hr_manager_password,
                "role": "hr_manager",
                "full_name": "HR Manager",
            },
            {
                "email": self.ceo_email.lower(),
                "password": self.ceo_password,
                "role": "ceo",
                "full_name": "CEO",
            },
        ]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.jwt_secret.startswith("dev-"):
        raise RuntimeError("Refusing to start in production with the development JWT secret")
    return settings
