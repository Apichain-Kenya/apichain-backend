"""The antivirus boundary (P3b-D, 04 §5.6, 11 D5).

ClamAV's daemon, spoken to directly over its documented TCP protocol:
`zINSTREAM`, then the file as length-prefixed chunks (4-byte big-endian
length, then the bytes), then a zero-length chunk; the daemon answers
`stream: OK`, `stream: <Signature> FOUND`, or `... ERROR`. That is about forty
lines, which is why this does not depend on the `clamd` PyPI package (last
released years ago).

**Fail-closed.** Anything other than a clear OK or a clear FOUND, including a
connection refused, a timeout, or an ERROR reply, raises `ScannerUnavailable`,
and the upload answers 503 having stored nothing. A scanner that is down must
never read as a file that is clean.

The engine string (`ClamAV 1.4.1/27400`, engine version / signature database
version) is recorded on the document, so a later rescan knows which documents
were checked against which signatures.
"""

import logging
import socket
import struct
from dataclasses import dataclass
from typing import Protocol

from app.config import settings

logger = logging.getLogger("apichain.scanner")

_CHUNK = 64 * 1024


class ScannerUnavailable(RuntimeError):
    """The scanner could not give a verdict. Nothing may be stored."""


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    engine: str
    signature: str | None = None


class Scanner(Protocol):
    def scan(self, data: bytes) -> ScanResult: ...


def parse_reply(reply: bytes) -> tuple[bool, str | None]:
    """(clean, signature) from an INSTREAM reply; raises on anything else."""
    text = reply.rstrip(b"\x00\n").decode("utf-8", errors="replace").strip()
    if text.endswith(" OK"):
        return True, None
    if text.endswith(" FOUND"):
        body = text.split(":", 1)[-1].strip()
        return False, body.removesuffix(" FOUND").strip()
    raise ScannerUnavailable(f"unexpected scanner reply: {text[:80]!r}")


class ClamdScanner:
    def __init__(self, *, host: str, port: int, timeout: float) -> None:
        self._host, self._port, self._timeout = host, port, timeout

    def _exchange(self, chunks: list[bytes]) -> bytes:
        try:
            with socket.create_connection((self._host, self._port), self._timeout) as sock:
                for chunk in chunks:
                    sock.sendall(chunk)
                reply = b""
                while not reply.endswith(b"\x00"):
                    part = sock.recv(4096)
                    if not part:
                        break
                    reply += part
                return reply
        except OSError as exc:
            logger.warning("clamd unreachable: %s", type(exc).__name__)
            raise ScannerUnavailable("scanner unreachable") from exc

    def version(self) -> str:
        reply = self._exchange([b"zVERSION\x00"]).rstrip(b"\x00").decode(errors="replace")
        # "ClamAV 1.4.1/27400/Tue Oct  7 08:25:02 2026" -> "ClamAV 1.4.1/27400"
        return "/".join(reply.strip().split("/")[:2]) or "ClamAV"

    def scan(self, data: bytes) -> ScanResult:
        chunks = [b"zINSTREAM\x00"]
        for start in range(0, len(data), _CHUNK):
            piece = data[start : start + _CHUNK]
            chunks.append(struct.pack(">I", len(piece)) + piece)
        chunks.append(struct.pack(">I", 0))
        clean, signature = parse_reply(self._exchange(chunks))
        return ScanResult(clean=clean, engine=self.version(), signature=signature)


def from_settings() -> Scanner:
    if settings.scanner_backend == "fake":
        from app.services.dev_fakes import SignatureScanner

        logger.warning("scanner_backend=fake: documents are NOT virus-scanned")
        return SignatureScanner()
    return ClamdScanner(
        host=settings.clamd_host,
        port=settings.clamd_port,
        timeout=settings.clamd_timeout_seconds,
    )
