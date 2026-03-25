"""애플리케이션 설정 -- config.yaml + .env 로딩."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _BASE_DIR / "config.yaml"
_ENV_PATH = _BASE_DIR / ".env"  # .env sits inside alert/ package

# ---------------------------------------------------------------------------
# Nested config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SourceConfig:
    """Single crawler source configuration."""

    enabled: bool = True
    base_url: str = ""


@dataclass
class CrawlerConfig:
    """Crawler-wide settings."""

    timeout: int = 30
    retry_count: int = 3
    retry_delay: int = 5
    user_agent: str = "AgriAlert/1.0"
    sources: Dict[str, SourceConfig] = field(default_factory=dict)


@dataclass
class TelegramConfig:
    """Telegram notifier settings."""

    enabled: bool = True
    parse_mode: str = "HTML"
    max_message_length: int = 4096
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class EmailConfig:
    """Email notifier settings."""

    enabled: bool = True
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_tls: bool = True
    digest_hour: int = 18
    sender: str = ""
    password: str = ""
    recipients: list[str] = field(default_factory=list)


@dataclass
class NotifierConfig:
    """Aggregated notifier settings."""

    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass
class AnalyzerConfig:
    """Analyzer/Claude settings."""

    keyword_threshold: float = 0.3
    claude_threshold: float = 0.3
    claude_model: str = "claude-sonnet-4-5-20250929"
    max_claude_calls_per_run: int = 50
    api_key: str = ""


@dataclass
class KeywordsConfig:
    """Keyword lists by category."""

    must_match: list[str] = field(default_factory=list)
    boost: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class ScheduleConfig:
    """Scheduler settings."""

    cron_hours: list[int] = field(default_factory=lambda: [9, 15])
    timezone: str = "Asia/Seoul"


@dataclass
class ClassifierConfig:
    """Domain classifier settings."""
    enabled: bool = True
    method: str = "keyword"
    claude_classify_threshold: float = 0.6


@dataclass
class VectorConfig:
    """Vector search settings."""
    enabled: bool = True
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    dimensions: int = 256
    similarity_threshold: float = 0.75
    top_k: int = 5


@dataclass
class ObsidianConfig:
    """Obsidian vault sync settings."""
    enabled: bool = True
    vault_path: str = ""
    announcement_folder: str = "11.(주)어반정글/영업/공고"
    research_folder: str = "Research/공고분석"
    min_relevance_for_sync: float = 0.5
    author: str = "이상민"


@dataclass
class HistoryConfig:
    """Application history settings."""
    enabled: bool = True
    auto_create_on_relevant: bool = True
    default_priority: int = 2


@dataclass
class ResearchConfig:
    """Research document generator settings."""
    enabled: bool = True
    schedule: str = "quarterly"
    claude_model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 4096


@dataclass
class N8nConfig:
    """n8n webhook settings."""
    enabled: bool = False
    webhook_url: str = ""
    notify_on: list[str] = field(default_factory=lambda: ["new_relevant", "status_change"])


@dataclass
class KnowledgeConfig:
    """Top-level knowledge layer configuration."""
    enabled: bool = True
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    vector: VectorConfig = field(default_factory=VectorConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    n8n: N8nConfig = field(default_factory=N8nConfig)


@dataclass
class AppConfig:
    """Top-level application configuration."""

    name: str = "농업공고알림봇"
    version: str = "1.0.0"
    log_level: str = "INFO"
    data_dir: str = "data"
    database_url: str = ""

    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    keywords: KeywordsConfig = field(default_factory=KeywordsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)

    # Convenience: absolute path to data directory
    @property
    def data_path(self) -> Path:
        return _BASE_DIR / self.data_dir


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _build_crawler_config(raw: Dict[str, Any]) -> CrawlerConfig:
    sources_raw: Dict[str, Any] = raw.pop("sources", {})
    sources = {
        name: SourceConfig(**vals) for name, vals in sources_raw.items()
    }
    return CrawlerConfig(**raw, sources=sources)


def _build_notifier_config(raw: Dict[str, Any]) -> NotifierConfig:
    tg = TelegramConfig(**raw.get("telegram", {}))
    em = EmailConfig(**raw.get("email", {}))
    return NotifierConfig(telegram=tg, email=em)


def _build_knowledge_config(raw: Dict[str, Any]) -> KnowledgeConfig:
    """Build KnowledgeConfig from raw YAML dict."""
    if not raw:
        return KnowledgeConfig(enabled=False)

    classifier_raw = raw.get("classifier", {})
    vector_raw = raw.get("vector", {})
    obsidian_raw = raw.get("obsidian", {})
    history_raw = raw.get("history", {})
    research_raw = raw.get("research", {})
    n8n_raw = raw.get("n8n", {})

    return KnowledgeConfig(
        enabled=raw.get("enabled", True),
        classifier=ClassifierConfig(**classifier_raw) if classifier_raw else ClassifierConfig(),
        vector=VectorConfig(**vector_raw) if vector_raw else VectorConfig(),
        obsidian=ObsidianConfig(**obsidian_raw) if obsidian_raw else ObsidianConfig(),
        history=HistoryConfig(**history_raw) if history_raw else HistoryConfig(),
        research=ResearchConfig(**research_raw) if research_raw else ResearchConfig(),
        n8n=N8nConfig(**n8n_raw) if n8n_raw else N8nConfig(),
    )


def _load_yaml() -> Dict[str, Any]:
    """Load and return the raw YAML configuration."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration file not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a YAML mapping at the top level")
    return data


def _load_env_secrets(cfg: AppConfig) -> None:
    """Overlay secrets from environment variables onto *cfg*."""
    # Telegram
    cfg.notifier.telegram.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg.notifier.telegram.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    # Anthropic / Claude
    cfg.analyzer.api_key = os.getenv("ANTHROPIC_API_KEY", "")

    # Email
    cfg.notifier.email.sender = os.getenv("EMAIL_SENDER", "")
    cfg.notifier.email.password = os.getenv("EMAIL_PASSWORD", "")
    recipients_raw = os.getenv("EMAIL_RECIPIENTS", "")
    if recipients_raw:
        cfg.notifier.email.recipients = [
            r.strip() for r in recipients_raw.split(",") if r.strip()
        ]

    # Bizinfo API key (stored on crawler source for convenience)
    bizinfo_key = os.getenv("BIZINFO_API_KEY", "")
    if bizinfo_key and "bizinfo" in cfg.crawler.sources:
        # Attach as an extra attribute -- crawlers can read it.
        cfg.crawler.sources["bizinfo"].api_key = bizinfo_key  # type: ignore[attr-defined]

    # data.go.kr API key (shared by G2B, K-Startup, etc.)
    data_go_kr_key = os.getenv("DATA_GO_KR_API_KEY", "")
    if data_go_kr_key:
        for source_name in ("g2b", "kstartup", "subsidy24", "forest_service"):
            if source_name in cfg.crawler.sources:
                cfg.crawler.sources[source_name].api_key = data_go_kr_key  # type: ignore[attr-defined]

    # Database URL (PostgreSQL)
    cfg.database_url = os.getenv("DATABASE_URL", "")

    # Knowledge Layer secrets
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        cfg.knowledge.vector.openai_api_key = openai_key  # type: ignore[attr-defined]

    obsidian_path = os.getenv("OBSIDIAN_VAULT_PATH", "")
    if obsidian_path:
        cfg.knowledge.obsidian.vault_path = obsidian_path

    n8n_url = os.getenv("N8N_WEBHOOK_URL", "")
    if n8n_url:
        cfg.knowledge.n8n.webhook_url = n8n_url


def _build_config() -> AppConfig:
    """Build the full AppConfig from YAML + env."""
    load_dotenv(_ENV_PATH)

    raw = _load_yaml()

    app_raw = raw.get("app", {})
    crawler_raw = raw.get("crawler", {})
    notifier_raw = raw.get("notifier", {})
    analyzer_raw = raw.get("analyzer", {})
    keywords_raw = raw.get("keywords", {})
    schedule_raw = raw.get("schedule", {})
    knowledge_raw = raw.get("knowledge", {})

    cfg = AppConfig(
        name=app_raw.get("name", "농업공고알림봇"),
        version=app_raw.get("version", "1.0.0"),
        log_level=app_raw.get("log_level", "INFO"),
        data_dir=app_raw.get("data_dir", "data"),
        crawler=_build_crawler_config(crawler_raw),
        notifier=_build_notifier_config(notifier_raw),
        analyzer=AnalyzerConfig(
            keyword_threshold=analyzer_raw.get("keyword_threshold", 0.3),
            claude_threshold=analyzer_raw.get("claude_threshold", 0.3),
            claude_model=analyzer_raw.get("claude_model", "claude-sonnet-4-5-20250929"),
            max_claude_calls_per_run=analyzer_raw.get("max_claude_calls_per_run", 50),
        ),
        keywords=KeywordsConfig(
            must_match=keywords_raw.get("must_match", []),
            boost=keywords_raw.get("boost", []),
            exclude=keywords_raw.get("exclude", []),
        ),
        schedule=ScheduleConfig(
            cron_hours=schedule_raw.get("cron_hours", [9, 15]),
            timezone=schedule_raw.get("timezone", "Asia/Seoul"),
        ),
        knowledge=_build_knowledge_config(knowledge_raw),
    )

    _load_env_secrets(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_config_instance: Optional[AppConfig] = None


def get_config(*, reload: bool = False) -> AppConfig:
    """Return the singleton AppConfig, building it on first call.

    Args:
        reload: Force re-reading config files when ``True``.

    Returns:
        The application configuration.
    """
    global _config_instance
    if _config_instance is None or reload:
        _config_instance = _build_config()
    return _config_instance
