"""Settings loaded from the repo-root .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env")

PAYPAL_API_BASE = {
    "SANDBOX": "https://api-m.sandbox.paypal.com",
    "PRODUCTION": "https://api-m.paypal.com",
}


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_environment: str
    paypal_webhook_id: str = ""  # from the developer dashboard or scripts/register_webhook.py

    @property
    def paypal_api_base(self) -> str:
        return PAYPAL_API_BASE[self.paypal_environment]


def get_settings() -> Settings:
    environment = os.getenv("PAYPAL_ENVIRONMENT", "SANDBOX").upper()
    if environment != "SANDBOX":
        # Hackathon rules require sandbox; refuse anything else rather than move real money.
        raise RuntimeError(f"PAYPAL_ENVIRONMENT must be SANDBOX, got {environment!r}")
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_environment=environment,
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
    )
