"""Explicit-opt-in stand-ins for the four boundaries (P3b-D, 11 D4).

Each is selected only by setting its backend to `fake`, and each leaves its
name on whatever it touches (`provider='fake'`, `scan_engine='fake'`), so a
row produced by a fake is identifiable forever, not only at deploy time.

Two of them are useful outside tests. `LogSms` is the SMS default because no
Africa's Talking credentials exist; it logs the message (including any
verification code, which is the only way to use the flow in dev). And
`SignatureScanner` lets a machine without Docker exercise uploads; it detects
the EICAR test string, so the infected path still behaves like a scanner's.
"""

import datetime as dt
import logging
import uuid
from typing import ClassVar
from urllib.parse import quote

from app.services.scanner import ScanResult

logger = logging.getLogger("apichain.dev_fakes")

# The EICAR anti-malware test string, assembled at import so the contiguous
# signature never sits in a source file (a desktop antivirus would quarantine
# this module, which is the opposite of helpful).
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS" + "-TEST-FILE!$H+H*").encode()


class SignatureScanner:
    """Flags the EICAR test string and nothing else. NOT antivirus."""

    engine = "fake"

    def scan(self, data: bytes) -> ScanResult:
        if EICAR in data:
            return ScanResult(clean=False, engine=self.engine, signature="Eicar-Test-Signature")
        return ScanResult(clean=True, engine=self.engine)


class MemoryObjectStore:
    """A process-local dict. Uploads vanish on restart; dev only."""

    _shared: ClassVar["MemoryObjectStore | None"] = None

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts = 0

    @classmethod
    def shared(cls) -> "MemoryObjectStore":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if key not in self.objects:
            self.objects[key] = (data, content_type)
            self.puts += 1

    def presign_get(self, key: str, *, ttl: dt.timedelta, content_type: str, filename: str) -> str:
        expires = int((dt.datetime.now(dt.UTC) + ttl).timestamp())
        return (
            f"https://storage.invalid/{key}?expires={expires}"
            f"&response-content-type={quote(content_type, safe='')}"
            f"&response-content-disposition=attachment"
            f"&filename={quote(filename, safe='')}"
        )


class LogSms:
    provider = "fake"

    def send(self, to: str, body: str) -> str:
        logger.info("[fake sms] to=%s body=%s", to, body)
        return f"fake-{uuid.uuid4().hex[:12]}"


class LogEmail:
    provider = "fake"

    def send(self, to: str, subject: str, body: str) -> str:
        logger.info("[fake email] to=%s subject=%s body=%s", to, subject, body)
        return f"<fake-{uuid.uuid4().hex[:12]}@apichain.invalid>"
