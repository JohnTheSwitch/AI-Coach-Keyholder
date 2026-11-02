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
    ollama_read_timeout_seconds: float = 30.0     # a large local model needs more (for example 120 for a 32B model)
    ollama_keep_alive: str | None = None          # e.g. "30m": keep the model loaded between messages
    conversation_structured_output: bool = False  # Phase F1: JSON replies (reply + optional side data)
    enable_coaching_memory: bool = False          # Phase F2: store and use Coaching Memory (needs structured output)
    enable_ai_suggestions: bool = False           # Phase F3: the write-only idea box for the keyholder (needs structured output)

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
    chaster_oauth_scopes: str = "locks"      # space-separated; the keyholder account needs "locks keyholder" to list managed locks
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

    # Rules Engine (Phase 2). STRICT DRY RUN: when enabled it only records
    # auditable Decisions; it never calls Chaster. OFF unless explicitly enabled.
    enable_rules_engine: bool = False

    # Chaster execution plane (Phase 3). Master switch, OFF by default. Even when
    # on, nothing is sent unless a real transport is available, both identities
    # below are set, and the runtime halt (execution_control) is cleared.
    enable_chaster_execution: bool = False
    chaster_execution_wearer_user_id: str | None = None      # internal user whose lock is observed
    chaster_execution_keyholder_user_id: str | None = None   # internal user holding the keyholder OAuth connection
    chaster_execution_interval_seconds: int = 60

    # General
    log_level: str = "INFO"
    quiet_hours_start: str = "22:00"   # for a future scheduler (Phase 5)
    quiet_hours_end: str = "07:00"

    # v0.39 scheduling windows (schedule_windows/). All local-time rules use APP_TIMEZONE.
    app_timezone: str = "Europe/Prague"
    workday_start: str = "07:00"
    workday_end: str = "16:30"
    holiday_country: str = "CZ"
    weekend_release_start: str = "10:00"   # release base time on weekends / public holidays
    # Adaptive release: target = WORKDAY_END + a pseudorandom offset in this range (minutes).
    unlock_window_min_offset_minutes: int = 30
    unlock_window_max_offset_minutes: int = 210
    # Safety backstop: a barrier held longer than this is released at the next valid
    # window regardless of tokens (placeholder value pending owner tuning).
    max_barrier_hold_hours: int = 72
    # Proactive supervision (trust-driven state; consumes trust_domain.recalculated). OFF by default.
    enable_supervision: bool = False
    # v0.39 Phase 2 (all OFF by default).
    enable_release_engine: bool = False       # periodic Single Barrier release tick
    enable_check_ins: bool = False            # proactive Discord check-ins (needs a trust domain + user)
    check_in_trust_domain: str | None = "routine"  # the Trust domain whose score sets the level and takes the evidence
    # Check-in deadlines (minutes). Strict Supervision Mode (Trust < 0.40) uses the shorter strict values.
    checkin_response_timeout_minutes: int = 60
    checkin_follow_up_minutes: int = 30
    checkin_strict_response_timeout_minutes: int = 30
    checkin_strict_follow_up_minutes: int = 15
    enable_multi_wearer: bool = False         # v0.39 Phase E: tenant registry and platform database (the running system is unchanged when off)
    platform_db_path: Path | None = None      # default: platform.sqlite next to the main database
    tenant_cap: int = 5                       # most non-removed tenants, tenant zero included (protects Chaster API rate limits)
    owner_user_id: str | None = None          # internal user id of the owner; the only identity `python -m cli freeze-lock` accepts
    enable_chaster_messaging: bool = False    # Chaster chat channel (opt-in per user); needs the bot account connected with `messaging`
    chaster_chat_poll_seconds: int = 30
    chaster_api_budget_per_minute: int = 30   # shared request budget of the keyholder account (multi-wearer)
    daily_evaluation_trust_domain: str = "routine"   # Trust domain of the clean-day streak bonus and the pool's Trust factor
    check_in_user_id: str | None = None       # internal user id; defaults to CHASTER_EXECUTION_WEARER_USER_ID

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
            ollama_read_timeout_seconds=float(get("OLLAMA_READ_TIMEOUT_SECONDS", "30") or "30"),
            ollama_keep_alive=get("OLLAMA_KEEP_ALIVE") or None,
            conversation_structured_output=(get("CONVERSATION_STRUCTURED_OUTPUT", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            enable_coaching_memory=(get("ENABLE_COACHING_MEMORY", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            enable_ai_suggestions=(get("ENABLE_AI_SUGGESTIONS", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            db_path=Path(db_path_str) if db_path_str else DEFAULT_DB_PATH,
            backup_retention_count=int(retention_str) if retention_str else 14,
            chaster_api_token=get("CHASTER_API_TOKEN"),
            apple_health_api_key=get("APPLE_HEALTH_API_KEY"),
            chaster_oauth_scopes=get("CHASTER_OAUTH_SCOPES", "locks") or "locks",
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
            enable_rules_engine=(get("ENABLE_RULES_ENGINE", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            enable_chaster_execution=(get("ENABLE_CHASTER_EXECUTION", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            chaster_execution_wearer_user_id=get("CHASTER_EXECUTION_WEARER_USER_ID"),
            chaster_execution_keyholder_user_id=get("CHASTER_EXECUTION_KEYHOLDER_USER_ID"),
            chaster_execution_interval_seconds=int(get("CHASTER_EXECUTION_INTERVAL_SECONDS", "60") or "60"),
            log_level=get("LOG_LEVEL", "INFO"),
            quiet_hours_start=get("QUIET_HOURS_START", "22:00"),
            quiet_hours_end=get("QUIET_HOURS_END", "07:00"),
            app_timezone=get("APP_TIMEZONE", "Europe/Prague") or "Europe/Prague",
            workday_start=get("WORKDAY_START", "07:00") or "07:00",
            workday_end=get("WORKDAY_END", "16:30") or "16:30",
            holiday_country=get("HOLIDAY_COUNTRY", "CZ") or "CZ",
            weekend_release_start=get("WEEKEND_RELEASE_START", "10:00") or "10:00",
            unlock_window_min_offset_minutes=int(get("UNLOCK_WINDOW_MIN_OFFSET_MINUTES", "30") or "30"),
            unlock_window_max_offset_minutes=int(get("UNLOCK_WINDOW_MAX_OFFSET_MINUTES", "210") or "210"),
            max_barrier_hold_hours=int(get("MAX_BARRIER_HOLD_HOURS", "72") or "72"),
            enable_supervision=(get("ENABLE_SUPERVISION", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            enable_release_engine=(get("ENABLE_RELEASE_ENGINE", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            enable_check_ins=(get("ENABLE_CHECK_INS", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            check_in_trust_domain=get("CHECK_IN_TRUST_DOMAIN", "routine") or "routine",
            checkin_response_timeout_minutes=int(get("CHECKIN_RESPONSE_TIMEOUT_MINUTES", "60") or "60"),
            checkin_follow_up_minutes=int(get("CHECKIN_FOLLOW_UP_MINUTES", "30") or "30"),
            checkin_strict_response_timeout_minutes=int(get("CHECKIN_STRICT_RESPONSE_TIMEOUT_MINUTES", "30") or "30"),
            checkin_strict_follow_up_minutes=int(get("CHECKIN_STRICT_FOLLOW_UP_MINUTES", "15") or "15"),
            check_in_user_id=get("CHECK_IN_USER_ID"),
            enable_multi_wearer=(get("ENABLE_MULTI_WEARER", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            platform_db_path=Path(get("PLATFORM_DB_PATH")) if get("PLATFORM_DB_PATH") else None,
            tenant_cap=int(get("TENANT_CAP", "5") or "5"),
            owner_user_id=get("OWNER_USER_ID"),
            enable_chaster_messaging=(get("ENABLE_CHASTER_MESSAGING", "false") or "false").strip().lower() in ("1", "true", "yes", "on"),
            chaster_chat_poll_seconds=int(get("CHASTER_CHAT_POLL_SECONDS", "30") or "30"),
            chaster_api_budget_per_minute=int(get("CHASTER_API_BUDGET_PER_MINUTE", "30") or "30"),
            daily_evaluation_trust_domain=get("DAILY_EVALUATION_TRUST_DOMAIN", "routine") or "routine",
        )
