"""Central configuration. The only place that reads environment variables."""
import os

from dotenv import load_dotenv
from pydantic import BaseModel


class Settings(BaseModel):
    # AI
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-sonnet-4-5"
    AI_MAX_RETRIES: int = 2
    # Slack
    SLACK_WEBHOOK_URL: str = ""
    # Alerting
    PRICE_CHANGE_THRESHOLD_PCT: float = 5.0
    # Scraper
    SCRAPER_TIMEOUT_SECONDS: int = 30
    SCRAPER_USER_AGENT: str = "Mozilla/5.0 (compatible; PriceMonitorBot/1.0)"
    PLAYWRIGHT_HEADLESS: bool = True
    SCRAPER_MAX_RETRIES: int = 2
    # Paths
    DB_PATH: str = "./data/products.db"
    PDF_STORAGE_DIR: str = "./data/pdfs"
    REPORT_OUTPUT_DIR: str = "./reports"
    TARGETS_CONFIG_PATH: str = "./config/targets.json"
    LOG_FILE_PATH: str = "./logs/run.log"
    # Scheduling
    RUN_HOUR_UTC: int = 6
    USE_INTERNAL_SCHEDULER: bool = False


def get_settings() -> Settings:
    """Load .env (if present) and return validated Settings.

    Real environment variables take precedence over .env values.
    Missing variables fall back to the defaults in ARCHITECTURE.md section 4.
    """
    load_dotenv()
    values = {
        name: os.environ[name]
        for name in Settings.model_fields
        if name in os.environ and os.environ[name] != ""
    }
    return Settings(**values)
