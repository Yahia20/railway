"""Configuration. Every value comes from the environment; nothing is hardcoded.

`Settings.validate_for()` is deliberately strict: a worker that starts with a
missing key and fails on the first real job is worse than one that refuses to
boot. Railway restarts a crashed container, so failing loudly at startup is the
correct behaviour.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else value


def _flag(key: str, default: bool = False) -> bool:
    return (_env(key) or str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Railway Postgres -------------------------------------------------
    # Railway injects DATABASE_URL. Inside the project use the private URL:
    # it does not traverse the public internet and does not bill egress.
    database_url: str | None = field(default_factory=lambda: _env("DATABASE_URL"))
    db_pool_min: int = field(default_factory=lambda: int(_env("DB_POOL_MIN", "1")))
    db_pool_max: int = field(default_factory=lambda: int(_env("DB_POOL_MAX", "8")))

    # --- Auth between n8n and this worker ---------------------------------
    worker_api_key: str | None = field(default_factory=lambda: _env("WORKER_API_KEY"))

    # --- DeepSeek ---------------------------------------------------------
    deepseek_api_key: str | None = field(default_factory=lambda: _env("DEEPSEEK_API_KEY"))
    deepseek_model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-chat"))

    # --- ASR --------------------------------------------------------------
    asr_backend: str = field(default_factory=lambda: _env("ASR_BACKEND", "space"))
    cohere_api_key: str | None = field(default_factory=lambda: _env("COHERE_API_KEY"))
    asr_chunk_seconds: float = field(default_factory=lambda: float(_env("ASR_CHUNK_SECONDS", "40")))

    # --- Sources ----------------------------------------------------------
    bitrix_portal_domain: str | None = field(default_factory=lambda: _env("BITRIX_PORTAL_DOMAIN"))
    bitrix_webhook_token: str | None = field(default_factory=lambda: _env("BITRIX_WEBHOOK_TOKEN"))
    bitrix_webhook_secret: str | None = field(default_factory=lambda: _env("BITRIX_WEBHOOK_SECRET"))

    drive_folder_id: str | None = field(default_factory=lambda: _env("DRIVE_CALLS_FOLDER_ID"))
    drive_credentials_json: str | None = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_JSON"))

    # --- Regional ---------------------------------------------------------
    # Not guessable: 0500000000 is a valid Saudi mobile and meaningless in Egypt.
    default_phone_region: str = field(default_factory=lambda: _env("DEFAULT_PHONE_REGION", "SA"))
    pbx_tz_offset_hours: int = field(default_factory=lambda: int(_env("PBX_TZ_OFFSET_HOURS", "3")))

    # --- Pipeline behaviour ----------------------------------------------
    # A conversation grows over days. Re-running the judge on every webhook is
    # expensive and produces churn; running once misses the outcome. Re-analyse
    # when the thread has new messages AND has been quiet for this long.
    reanalysis_idle_minutes: int = field(
        default_factory=lambda: int(_env("REANALYSIS_IDLE_MINUTES", "30")))
    score_bot_only_conversations: bool = field(
        default_factory=lambda: _flag("SCORE_BOT_ONLY_CONVERSATIONS", False))
    work_dir: str = field(default_factory=lambda: _env("WORK_DIR", "/tmp/customer360"))

    def validate_for(self, *capabilities: str) -> None:
        """Fail fast, naming the exact env var that is missing."""
        required: dict[str, list[tuple[str, str | None]]] = {
            "db": [("DATABASE_URL", self.database_url)],
            "judge": [("DEEPSEEK_API_KEY", self.deepseek_api_key)],
            "api": [("WORKER_API_KEY", self.worker_api_key)],
            "calls": [("DRIVE_CALLS_FOLDER_ID", self.drive_folder_id),
                      ("GOOGLE_SERVICE_ACCOUNT_JSON", self.drive_credentials_json)],
            "chats": [("BITRIX_PORTAL_DOMAIN", self.bitrix_portal_domain),
                      ("BITRIX_WEBHOOK_TOKEN", self.bitrix_webhook_token)],
        }
        missing = [
            name
            for cap in capabilities
            for name, value in required.get(cap, [])
            if not value
        ]
        if self.asr_backend == "cohere_api" and not self.cohere_api_key:
            missing.append("COHERE_API_KEY")
        if missing:
            raise RuntimeError(
                "missing required environment variables: " + ", ".join(sorted(set(missing)))
            )


settings = Settings()
