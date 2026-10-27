"""OAuth client-credentials token for the PayPal REST API, cached until shortly before expiry."""

import time

import httpx

from intentguard.config import Settings

# Refresh this many seconds before PayPal says the token expires.
_EXPIRY_MARGIN_S = 300


class MissingCredentialsError(RuntimeError):
    pass


class TokenProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        if not (self._settings.paypal_client_id and self._settings.paypal_client_secret):
            raise MissingCredentialsError(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set. Copy .env.example to .env and fill them in."
            )
        response = httpx.post(
            f"{self._settings.paypal_api_base}/v1/oauth2/token",
            auth=(self._settings.paypal_client_id, self._settings.paypal_client_secret),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + body["expires_in"] - _EXPIRY_MARGIN_S
        return self._token

    @property
    def expires_at(self) -> float:
        return self._expires_at
