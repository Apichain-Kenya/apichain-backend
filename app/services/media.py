"""Pure checks on uploaded bytes (P3b-D, 04 §5.6, 11 §7). No I/O.

**Sniffing is by magic bytes, never by the client's word.** The declared
`Content-Type` and the filename extension are both attacker-chosen; the first
bytes of the file are what a viewer will actually interpret. Three types are
allowed (04 §5.6: PDF, JPG, PNG for identity documents). Anything else is
`None` and the upload is refused.

There is deliberately no libmagic here: `python-magic` needs a native library,
and three fixed signatures do not justify a dependency whose install cannot
be proven on this machine (Docker is down).
"""

import hashlib
import re
import unicodedata

# 04 §5.6: about 10 MB per file, 50 MB per farmer.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SUBJECT_BYTES = 50 * 1024 * 1024

# Long enough to hold the longest signature below.
SNIFF_BYTES = 8

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

ALLOWED_TYPES = frozenset(mime for _, mime in _SIGNATURES)

_MAX_FILENAME = 255
_WHITESPACE = re.compile(r"\s+")


def sniff(head: bytes) -> str | None:
    """The MIME type the leading bytes declare, or None if not allowed."""
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime
    return None


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def object_key(content_hash: bytes) -> str:
    """Content-addressed: identical bytes share one object (11 D3)."""
    return f"sha256/{content_hash.hex()}"


def sanitize_filename(name: str | None) -> str:
    """A display name that is safe to store and to put in a header.

    Kept only as metadata (the object key is the hash), so this never decides
    where anything is written; it decides what a later reader is shown. Drops
    any directory part on either separator, every control and format character
    (U+0000, the bidi overrides that make `gpj.exe` read as `exe.jpg`),
    collapses whitespace, and bounds the length while keeping the extension.
    """
    raw = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    # Whitespace first (a tab is a control character and would otherwise be
    # deleted rather than read as a space), then every remaining C* character.
    spaced = _WHITESPACE.sub(" ", raw)
    kept = "".join(ch for ch in spaced if unicodedata.category(ch)[0] != "C")
    cleaned = _WHITESPACE.sub(" ", kept).strip().strip(".")
    if not cleaned:
        return "document"
    if len(cleaned) > _MAX_FILENAME:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            cleaned = stem[: _MAX_FILENAME - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:_MAX_FILENAME]
    return cleaned
