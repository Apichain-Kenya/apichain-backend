"""P3b-D: the pure media/phone checks and the four boundaries (11 §7).

Nothing here touches a network. The live adapters are exercised through
socket and HTTP fakes at their protocol edge, so the parsing that decides
"clean" or "sent" is tested, not mocked away.
"""

import datetime as dt
import socket
import struct
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import media, phone
from app.services.dev_fakes import EICAR, MemoryObjectStore, SignatureScanner
from app.services.scanner import ClamdScanner, ScannerUnavailable, parse_reply
from app.services.sms import AfricasTalkingSms, SendFailed
from app.services.storage import content_disposition

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"0" * 32

# --- media.sniff ------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (PDF, "application/pdf"),
        (b"<!doctype html><script>alert(1)</script>", None),
        (b"MZ\x90\x00", None),  # a Windows executable, whatever it is named
        (b"", None),
        (b"%PD", None),  # truncated signature
    ],
)
def test_sniff_reads_the_bytes_not_the_name(data, expected):
    assert media.sniff(data[: media.SNIFF_BYTES]) == expected


def test_a_pdf_named_png_sniffs_as_pdf():
    # The filename and the declared type are attacker-chosen; only this counts.
    assert media.sniff(PDF) == "application/pdf"


def test_object_keys_are_content_addressed():
    digest = media.sha256(PNG)
    assert media.object_key(digest) == f"sha256/{digest.hex()}"
    assert media.object_key(media.sha256(PNG)) == media.object_key(digest)


# --- media.sanitize_filename --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("id card.png", "id card.png"),
        ("..\\..\\windows\\system32\\x.exe", "x.exe"),
        ("/etc/passwd", "passwd"),
        ("a\x00b.pdf", "ab.pdf"),
        ("gpj‮.exe", "gpj.exe"),  # the right-to-left override is dropped
        ("  lots   of\tspace .pdf ", "lots of space .pdf"),
        ("", "document"),
        (None, "document"),
        ("../..", "document"),
        ("Ñjeri’s permit.pdf", "Ñjeri’s permit.pdf"),  # real names survive
    ],
)
def test_filenames_are_made_safe_to_store_and_show(raw, expected):
    assert media.sanitize_filename(raw) == expected


def test_a_long_filename_is_bounded_and_keeps_its_extension():
    cleaned = media.sanitize_filename("a" * 400 + ".pdf")
    assert len(cleaned) == 255
    assert cleaned.endswith(".pdf")


# --- phone.to_e164_ke ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0712345678", "+254712345678"),
        ("0712 345 678", "+254712345678"),
        ("0712-345-678", "+254712345678"),
        ("254712345678", "+254712345678"),
        ("+254712345678", "+254712345678"),
        ("+254 112 345 678", "+254112345678"),
        ("0112345678", "+254112345678"),
        ("(0712) 345678", "+254712345678"),
    ],
)
def test_kenyan_mobiles_normalize_to_e164(raw, expected):
    assert phone.to_e164_ke(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "12345", "+14155550123", "0212345678", "07123456789", "+2547123456", "phone"],
)
def test_anything_else_is_unsendable(raw):
    assert phone.to_e164_ke(raw) is None


# --- scanner ------------------------------------------------------------------


def test_the_signature_scanner_flags_eicar_and_passes_a_png():
    scanner = SignatureScanner()
    assert scanner.scan(PNG).clean is True
    flagged = scanner.scan(b"prefix " + EICAR + b" suffix")
    assert flagged.clean is False and flagged.signature
    assert flagged.engine == "fake"


@pytest.mark.parametrize(
    ("reply", "clean", "signature"),
    [
        (b"stream: OK\x00", True, None),
        (b"stream: Eicar-Test-Signature FOUND\x00", False, "Eicar-Test-Signature"),
    ],
)
def test_clamd_replies_are_parsed(reply, clean, signature):
    assert parse_reply(reply) == (clean, signature)


@pytest.mark.parametrize("reply", [b"INSTREAM size limit exceeded. ERROR\x00", b"", b"garbage\x00"])
def test_anything_but_ok_or_found_is_a_scanner_failure(reply):
    with pytest.raises(ScannerUnavailable):
        parse_reply(reply)


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        part = conn.recv(n - len(data))
        if not part:
            raise ConnectionError("client closed early")
        data += part
    return data


def _read_command(conn: socket.socket) -> bytes:
    """One request, parsed the way clamd parses it: the command up to its
    NUL, then (for INSTREAM) length-prefixed chunks until a zero length. A
    payload containing four zero bytes must not end the stream early."""
    command = b""
    while not command.endswith(b"\x00"):
        command += _recv_exact(conn, 1)
    data = command
    if command == b"zINSTREAM\x00":
        while True:
            prefix = _recv_exact(conn, 4)
            data += prefix
            (length,) = struct.unpack(">I", prefix)
            if length == 0:
                break
            data += _recv_exact(conn, length)
    return data


def _fake_clamd(replies: dict[bytes, bytes]) -> tuple[int, list[bytes]]:
    """A one-port clamd that answers by command and records what it received."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received: list[bytes] = []

    def serve() -> None:
        for _ in range(len(replies)):
            conn, _ = server.accept()
            with conn:
                data = _read_command(conn)
                received.append(data)
                command = data.split(b"\x00", 1)[0]
                conn.sendall(replies[command])
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], received


def test_the_clamd_client_streams_length_prefixed_chunks():
    port, received = _fake_clamd(
        {b"zINSTREAM": b"stream: OK\x00", b"zVERSION": b"ClamAV 1.4.1/27400/Tue Oct 7\x00"}
    )
    result = ClamdScanner(host="127.0.0.1", port=port, timeout=5).scan(PNG)
    assert result.clean is True
    assert result.engine == "ClamAV 1.4.1/27400"
    stream = received[0]
    assert stream.startswith(b"zINSTREAM\x00" + struct.pack(">I", len(PNG)) + PNG)
    assert stream.endswith(struct.pack(">I", 0))


def test_an_unreachable_clamd_fails_closed():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # bound but never listening
    with pytest.raises(ScannerUnavailable):
        ClamdScanner(host="127.0.0.1", port=port, timeout=1).scan(PNG)


# --- storage ------------------------------------------------------------------


def test_the_memory_store_writes_each_hash_once():
    store = MemoryObjectStore()
    store.put("sha256/aa", PNG, "image/png")
    store.put("sha256/aa", PNG, "image/png")
    assert store.puts == 1
    url = store.presign_get(
        "sha256/aa", ttl=dt.timedelta(minutes=5), content_type="image/png", filename="a b.png"
    )
    query = parse_qs(urlparse(url).query)
    assert query["response-content-type"] == ["image/png"]


def test_the_attachment_header_encodes_any_filename_safely():
    header = content_disposition('evil"; filename=x.html\r\nX: y.png')
    assert header.startswith("attachment; filename*=UTF-8''")
    assert '"' not in header and "\r" not in header and "\n" not in header


def test_s3_urls_are_signed_for_the_public_host():
    from app.services.storage import S3ObjectStore

    store = S3ObjectStore(
        endpoint="minio:9000",
        public_endpoint="files.example.org",
        bucket="docs",
        access_key="k",
        secret_key="s",
        secure=False,
    )
    url = store.presign_get(
        "sha256/ab", ttl=dt.timedelta(minutes=5), content_type="image/png", filename="x.png"
    )
    parsed = urlparse(url)
    assert parsed.netloc == "files.example.org"
    query = parse_qs(parsed.query)
    assert query["response-content-type"] == ["image/png"]
    assert query["response-content-disposition"][0].startswith("attachment")
    assert query["X-Amz-Expires"] == ["300"]


# --- sms ----------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: object) -> None:
        self.status_code, self._body = status, body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _at(monkeypatch, response: _Response) -> list[dict]:
    calls: list[dict] = []

    def post(url, data, headers, timeout):
        calls.append({"url": url, "data": data, "headers": headers})
        return response

    monkeypatch.setattr("app.services.sms.requests.post", post)
    return calls


def _sender() -> AfricasTalkingSms:
    return AfricasTalkingSms(
        username="sandbox", api_key="key", sender_id=None, sandbox=True, timeout=5
    )


def test_africas_talking_success_returns_the_message_id(monkeypatch):
    ok = {"SMSMessageData": {"Recipients": [{"status": "Success", "messageId": "ATXid_1"}]}}
    calls = _at(monkeypatch, _Response(201, ok))
    assert _sender().send("+254712345678", "hi") == "ATXid_1"
    assert calls[0]["headers"]["apiKey"] == "key"
    assert "sandbox" in calls[0]["url"]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_Response(401, {}), "provider_http_401"),
        (_Response(201, ValueError("not json")), "provider_bad_response"),
        (_Response(201, {"SMSMessageData": {"Recipients": []}}), "provider_bad_response"),
        (
            _Response(201, {"SMSMessageData": {"Recipients": [{"status": "InvalidPhoneNumber"}]}}),
            "provider_rejected",
        ),
    ],
)
def test_africas_talking_failures_carry_a_stable_code(monkeypatch, response, code):
    _at(monkeypatch, response)
    with pytest.raises(SendFailed) as ei:
        _sender().send("+254712345678", "hi")
    assert ei.value.code == code


# --- settings -----------------------------------------------------------------


def test_an_unknown_backend_name_is_refused():
    with pytest.raises(ValidationError):
        Settings(scanner_backend="none")


def test_defaults_are_real_or_fail_closed():
    defaults = Settings(_env_file=None)
    assert defaults.scanner_backend == "clamd"
    assert defaults.storage_backend == "s3"
    assert defaults.email_backend == "smtp"


def test_a_large_file_is_streamed_in_bounded_chunks():
    port, received = _fake_clamd(
        {b"zINSTREAM": b"stream: OK\x00", b"zVERSION": b"ClamAV 1.4.1/27400/x\x00"}
    )
    payload = PNG + bytes(range(256)) * 600  # ~150 KiB: three chunks
    ClamdScanner(host="127.0.0.1", port=port, timeout=5).scan(payload)
    stream = received[0]
    assert stream.count(struct.pack(">I", 64 * 1024)) >= 2
    assert stream.endswith(struct.pack(">I", 0))


# --- security-review fixes ------------------------------------------------------


def test_the_log_only_sms_redacts_codes_and_numbers_by_default(caplog, monkeypatch):
    from app.config import settings
    from app.services.dev_fakes import LogSms

    monkeypatch.setattr(settings, "dev_log_message_bodies", False)
    with caplog.at_level("INFO", logger="apichain.dev_fakes"):
        LogSms().send("+254712345678", "ApiChain: your code is 482913.")
    assert "482913" not in caplog.text
    assert "+254712345678" not in caplog.text
    assert "678" in caplog.text  # enough to tell recipients apart


def test_a_developer_can_opt_in_to_full_bodies(caplog, monkeypatch):
    from app.config import settings
    from app.services.dev_fakes import LogSms

    monkeypatch.setattr(settings, "dev_log_message_bodies", True)
    with caplog.at_level("INFO", logger="apichain.dev_fakes"):
        LogSms().send("+254712345678", "code 482913")
    assert "482913" in caplog.text


def test_starttls_verifies_the_server_certificate(monkeypatch):
    import ssl

    from app.services.mailer import SmtpEmail

    seen: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            seen["context"] = context

        def login(self, user, password):
            pass

        def send_message(self, message):
            seen["sent"] = True

    monkeypatch.setattr("app.services.mailer.smtplib.SMTP", FakeSMTP)
    SmtpEmail(
        host="smtp.test",
        port=587,
        sender="a@b.test",
        username=None,
        password=None,
        starttls=True,
        timeout=5,
    ).send("c@d.test", "s", "b")
    context = seen["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname is True
