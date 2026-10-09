"""
core/config.py

Central configuration and secret loading. Deliberately without a
dependency on python-dotenv -- our own minimal loader gives us full
control over error messages (an explicit error for a missing required
variable, rather than a silent None that only surfaces deep inside a
Discord/Ollama call).

The .env file must NEVER go into git -- see .env.example as a template.
The Discord token, any Chaster/Apple Health keys, and other sensitive
values live exclusively in .env, never hardcoded.

Usage:
    from core.config import Config
    config = Config.load()
    print(config.discord_token)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "coach_keyholder.db"


class ConfigError(Exception):
    """Raised when a required configuration value is missing."""


def _parse_env_file(path: Path) -> dict[str, str]:
    """
    Minimalist .env parser: KEY=VALUE per line, # as a comment, blank
    lines ignored, values are not further processed (no escape
    sequences) -- sufficient for secrets and simple configuration.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value

    return values


@dataclass
class Config:
    # Discord
    discord_token: str
    discord_command_prefix: str = "!"

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"  # TBD until a specific model is confirmed

    # Database
    db_path: Path = DEFAULT_DB_PATH
    backup_retention_count: int = 14  # how many recent automatic backups to keep

    # Integrations (optional -- Phase 7, just scaffolded for now)
    chaster_api_token: str | None = None
    apple_health_api_key: str | None = None

    # CHASTER-01A -- OAuth connection foundation. All optional: the
    # bot must start normally, with the Chaster callback listener
    # simply not started (CoachKeyholderBot.setup_hook() checks this),
    # if any of these is unset -- see chaster/README.md.
    #
    # Non-secrets:
    chaster_client_id: str | None = None
    chaster_redirect_uri: str | None = None            # the full public callback URL
    chaster_callback_bind_host: str = "127.0.0.1"      # localhost-only, never a public bind address
    chaster_callback_bind_port: int = 8420
    #
    # Secrets -- never in source control, logs, Discord, Working
    # Memory, or model prompts:
    chaster_client_secret: str | None = None
    chaster_token_encryption_key: str | None = None

    # PC-local emergency-unlock safety plane (design/architecture doc
    # Section 25c) -- intentionally a SEPARATE process from the
    # Discord bot (chaster/emergency_unlock_server.py, not wired into
    # CoachKeyholderBot at all), so it stays usable even if the bot
    # process itself is hung/crashed/malfunctioning. All optional --
    # the emergency server refuses to start at all if the secret is
    # unset (fails loudly, never runs unauthenticated).
    #
    # Non-secret:
    chaster_emergency_bind_host: str = "127.0.0.1"     # localhost-only, never a public bind address
    chaster_emergency_bind_port: int = 8421            # distinct from the OAuth callback's own 8420
    #
    # Secret -- deliberately its OWN, separate value: never the same
    # as chaster_client_secret or chaster_token_encryption_key, so
    # compromising one secret does not also compromise the emergency
    # path:
    chaster_emergency_unlock_secret: str | None = None

    # Lets the LLM call the read-only `get_chaster_status` tool
    # (conversation_engine/tools.py, chaster/llm_tools.py). OFF by
    # default: it needs an Ollama model that supports tool calling, and
    # puts provider-reported Chaster data in front of the model.
    chaster_llm_tools_enabled: bool = False

    # General
    log_level: str = "INFO"
    quiet_hours_start: str = "22:00"   # for a future scheduler (Phase 5)
    quiet_hours_end: str = "07:00"

    @classmethod
    def load(cls, env_path: Path | None = None) -> Config:
        """
        Loads configuration: real environment variables first (they
        take precedence, useful for CI/containers), then fills in from
        the .env file. Raises ConfigError if DISCORD_TOKEN is missing.
        """
        env_path = env_path or DEFAULT_ENV_PATH
        file_values = _parse_env_file(env_path)

        def get(key: str, default: str | None = None) -> str | None:
            return os.environ.get(key) or file_values.get(key) or default

        discord_token = get("DISCORD_TOKEN")
        if not discord_token:
            raise ConfigError(
                f"DISCORD_TOKEN is not set. Copy .env.example to .env "
                f"(expected path: {env_path}) and fill in the bot token."
            )

        db_path_str = get("DB_PATH")
        retention_str = get("BACKUP_RETENTION_COUNT", "14")

        return cls(
            discord_token=discord_token,
            discord_command_prefix=get("DISCORD_COMMAND_PREFIX", "!"),
            ollama_host=get("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=get("OLLAMA_MODEL", "llama3.1"),
            db_path=Path(db_path_str) if db_path_str else DEFAULT_DB_PATH,
            backup_retention_count=int(retention_str) if retention_str else 14,
            chaster_api_token=get("CHASTER_API_TOKEN"),
            apple_health_api_key=get("APPLE_HEALTH_API_KEY"),
            chaster_client_id=get("CHASTER_CLIENT_ID"),
            chaster_redirect_uri=get("CHASTER_REDIRECT_URI"),
            chaster_callback_bind_host=get("CHASTER_CALLBACK_BIND_HOST", "127.0.0.1") or "127.0.0.1",
            chaster_callback_bind_port=int(get("CHASTER_CALLBACK_BIND_PORT", "8420") or "8420"),
            chaster_client_secret=get("CHASTER_CLIENT_SECRET"),
            chaster_token_encryption_key=get("CHASTER_TOKEN_ENCRYPTION_KEY"),
            chaster_emergency_bind_host=get("CHASTER_EMERGENCY_BIND_HOST", "127.0.0.1") or "127.0.0.1",
            chaster_emergency_bind_port=int(get("CHASTER_EMERGENCY_BIND_PORT", "8421") or "8421"),
            chaster_emergency_unlock_secret=get("CHASTER_EMERGENCY_UNLOCK_SECRET"),
            chaster_llm_tools_enabled=(get("CHASTER_LLM_TOOLS_ENABLED", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            log_level=get("LOG_LEVEL", "INFO"),
            quiet_hours_start=get("QUIET_HOURS_START", "22:00"),
            quiet_hours_end=get("QUIET_HOURS_END", "07:00"),
        )
