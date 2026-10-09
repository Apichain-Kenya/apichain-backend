"""The SMS boundary (P3b-D, 04 §3.5, §5.9 boundary 5, 11 D4).

Africa's Talking is the recommended Kenyan provider (04 §3.5): KES billing and
high delivery on Safaricom and Airtel. No credentials are known to exist, so
the default backend is the log-only dev fake and every row it touches says
`provider='fake'`. The live adapter is selected by `sms_backend=africastalking`.

No retry here: a failure raises `SendFailed` with a stable short code. The
verification endpoint answers 503; the milestone worker reschedules within a
bounded number of attempts (11 D8).
"""

import logging
from typing import Protocol

import requests

from app.config import settings

logger = logging.getLogger("apichain.sms")


class SendFailed(RuntimeError):
    """The provider did not accept the message. `code` is stored, not the text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SmsSender(Protocol):
    provider: str

    def send(self, to: str, body: str) -> str:
        """Send and return the provider's message id."""
        ...


class AfricasTalkingSms:
    provider = "africastalking"

    _LIVE = "https://api.africastalking.com/version1/messaging"
    _SANDBOX = "https://api.sandbox.africastalking.com/version1/messaging"

    def __init__(
        self, *, username: str, api_key: str, sender_id: str | None, sandbox: bool, timeout: float
    ) -> None:
        self._username, self._api_key = username, api_key
        self._sender_id, self._timeout = sender_id, timeout
        self._url = self._SANDBOX if sandbox else self._LIVE

    def send(self, to: str, body: str) -> str:
        data = {"username": self._username, "to": to, "message": body}
        if self._sender_id:
            data["from"] = self._sender_id
        try:
            response = requests.post(
                self._url,
                data=data,
                headers={"apiKey": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise SendFailed("provider_unreachable") from exc
        if response.status_code >= 400:
            raise SendFailed(f"provider_http_{response.status_code}")
        try:
            recipient = response.json()["SMSMessageData"]["Recipients"][0]
        except (ValueError, KeyError, IndexError) as exc:
            raise SendFailed("provider_bad_response") from exc
        if recipient.get("status") != "Success":
            raise SendFailed("provider_rejected")
        return str(recipient.get("messageId", ""))


def from_settings() -> SmsSender:
    if settings.sms_backend == "fake":
        from app.services.dev_fakes import LogSms

        return LogSms()
    return AfricasTalkingSms(
        username=settings.at_username,
        api_key=settings.at_api_key,
        sender_id=settings.at_sender_id,
        sandbox=settings.at_sandbox,
        timeout=settings.at_timeout_seconds,
    )
