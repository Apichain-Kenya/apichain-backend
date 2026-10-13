"""Phase 4a live walk against the compose stack: real Postgres/PostGIS, MinIO,
ClamAV, Mailpit and Nominatim. Prints one line per check; exits non-zero on any
failure. First run 2026-10-13: 23/23.

    docker compose --profile av up -d --build      # wait for clamd to start
    docker compose exec api uv run --no-sync alembic upgrade head
    docker compose exec -e PYTHONPATH=/srv api uv run --no-sync python scripts/seed_dev_officer.py
    python scripts/live_walk.py

Uses only the standard library, so it runs from any Python on the host.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:8000"
COOKIE = "__Secure-apichain_rt"
results = []


def call(method, path, body=None, token=None, cookie=None, headers=None, raw=None, ctype=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        h["Content-Type"] = ctype
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = f"{COOKIE}={cookie}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read(), r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), e.headers.get_all("Set-Cookie") or []


def js(b):
    try:
        return json.loads(b)
    except Exception:
        return b


def cookie_of(set_cookies):
    for c in set_cookies:
        if c.startswith(COOKIE + "="):
            return c.split(";")[0].split("=", 1)[1], c
    return None, None


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def key():
    return {"Idempotency-Key": str(uuid.uuid4())}


CLIENT = {"X-ApiChain-Client": "pwa"}
suffix = str(int(time.time()))[-6:]
phone = f"+2547{suffix}01"

# 1. Officer signs in; the refresh token arrives only as the cookie.
s, h, b, sc = call(
    "POST", "/v2/auth/login", {"identifier": "officer-live", "password": "live-check-pw"}
)
officer = js(b).get("access_token")
_, raw_cookie = cookie_of(sc)
check(
    "officer login 200, token in body, no refresh_token in body",
    s == 200 and officer and "refresh_token" not in js(b),
    s,
)
check(
    "refresh cookie is HttpOnly; Secure; SameSite=Strict; Path=/v2/auth",
    bool(raw_cookie)
    and all(
        a in raw_cookie.lower() for a in ["httponly", "secure", "samesite=strict", "path=/v2/auth"]
    ),
    raw_cookie,
)

# 2. Officer enrols a farmer (consent captured at enrolment).
s, h, b, _ = call(
    "POST",
    "/v2/farmers",
    {
        "first_name": "Wanjiru",
        "last_name": "Kamau",
        "phone": phone,
        "email": f"wanjiru{suffix}@example.test",
        "password": "farm-pw-1234",
        "consent_granted": True,
        "consent_text_version": "enrol-v1",
    },
    token=officer,
    headers=key(),
)
farmer = js(b)
check("officer enrols a farmer 201", s == 201, s)
farmer_id = farmer.get("id")

# 3. The farmer signs in; /me knows who they are.
s, h, b, sc = call("POST", "/v2/auth/login", {"identifier": phone, "password": "farm-pw-1234"})
ftoken = js(b).get("access_token")
fcookie, _ = cookie_of(sc)
s2, _, b2, _ = call("GET", "/v2/me", token=ftoken)
check("farmer /me returns farmer_id", s2 == 200 and js(b2).get("farmer_id") == farmer_id, js(b2))

# 4. Refresh: needs the header; rotates; a lost reply inside 60 s is forgiven.
s, *_ = call("POST", "/v2/auth/refresh", cookie=fcookie)
check("refresh without X-ApiChain-Client is 403", s == 403, s)
s, h, b, sc = call("POST", "/v2/auth/refresh", cookie=fcookie, headers=CLIENT)
c2, _ = cookie_of(sc)
check("refresh rotates the cookie", s == 200 and c2 and c2 != fcookie, s)
s, h, b, sc = call("POST", "/v2/auth/refresh", cookie=fcookie, headers=CLIENT)  # old cookie again
c3, _ = cookie_of(sc)
check("lost-reply retry with the old cookie inside 60 s still works (D5)", s == 200 and c3, s)
s, *_ = call("POST", "/v2/auth/refresh", cookie=c3, headers=CLIENT)
check("the family survived: the new cookie refreshes", s == 200, s)
ftoken = js(b).get("access_token")

# 5. Writes need a key; the apiary gets a locality from rounded coordinates.
s, *_ = call(
    "POST",
    "/v2/apiaries",
    {"farmer_id": farmer_id, "name": "Ridge", "latitude": "-0.428713", "longitude": "36.951247"},
    token=ftoken,
)
check("a write without Idempotency-Key is 422", s == 422, s)
s, h, b, _ = call(
    "POST",
    "/v2/apiaries",
    {
        "farmer_id": farmer_id,
        "name": "Ridge",
        "latitude": "-0.428713",
        "longitude": "36.951247",
        "hive_count": 6,
    },
    token=ftoken,
    headers=key(),
)
apiary = js(b)
check("farmer creates an apiary 201", s == 201, s)
check("locality looked up (Nominatim, 2 dp)", bool(apiary.get("locality")), apiary.get("locality"))

# 6. Batch + harvest, and a replay of the same key makes nothing new.
bkey = key()
batch_body = {
    "farmer_id": farmer_id,
    "apiary_id": apiary["id"],
    "metadata": {
        "honey_type": "acacia",
        "expected_yield_kg": "28.00",
        "harvest_window_start": "2026-10-13",
        "harvest_window_end": "2026-10-13",
        "apiary_management_method": "organic",
    },
}
s, h, b, _ = call("POST", "/v2/batches", batch_body, token=ftoken, headers=bkey)
batch = js(b)
s2, h2, b2, _ = call("POST", "/v2/batches", batch_body, token=ftoken, headers=bkey)
check(
    "batch 201, and the same key replays the same batch",
    s == 201 and js(b2).get("id") == batch.get("id"),
    (s, s2),
)
s, *_ = call(
    "POST",
    f"/v2/batches/{batch['id']}/harvest",
    {"harvest_date": "2026-10-13", "quantity_kg": "35.00", "hive_ids": []},
    token=ftoken,
    headers=key(),
)
check("harvest recorded 201", s == 201, s)

# 7. Reads.
s, h, b, _ = call("GET", "/v2/batches", token=ftoken)
items = js(b).get("items", [])
check(
    "farmer lists exactly their one batch",
    s == 200 and [i["id"] for i in items] == [batch["id"]],
    len(items),
)
s, h, b, _ = call("GET", f"/v2/batches/{batch['id']}", token=ftoken)
detail = js(b)
check(
    "batch detail: HARVESTED, stages attributed to the farmer",
    s == 200
    and detail["state"] == "HARVESTED"
    and {x["actor_role"] for x in detail["stages"]} == {"farmer"},
    [x["action"] for x in detail.get("stages", [])],
)
s, h, b, _ = call("GET", f"/v2/apiaries?farmer_id={farmer_id}", token=ftoken)
check(
    "farmer lists their apiary with exact coordinates",
    s == 200 and js(b)[0]["latitude"].startswith("-0.428713"),
    s,
)

# 8. The anonymous jar check.
s, h, b, _ = call("GET", f"/v2/public/batches/{batch['public_id']}/verify")
v = js(b)
check(
    "public /verify 200 with place, no exact coordinates",
    s == 200 and v.get("place") == apiary.get("locality") and "0.428713" not in b.decode(),
    v.get("place"),
)
check(
    "/verify blocks carry recorded_on",
    v["verification"]["harvest"]["recorded_on"] is not None,
    v["verification"]["harvest"]["recorded_on"],
)

# 9. Documents into MinIO (needs document_upload consent first).
s, *_ = call(
    "POST",
    f"/v2/farmers/{farmer_id}/consents",
    {"purpose": "document_upload", "granted": True, "text_version": "docs-v1"},
    token=ftoken,
    headers=key(),
)
check("farmer grants document_upload consent", s in (200, 201), s)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600"
    "00001f15c4890000000d49444154789c6360000002000154a24f5d"
    "0000000049454e44ae426082"
)
boundary = "----apichainlive"
CRLF = "\r\n"
parts = [
    f"--{boundary}",
    'Content-Disposition: form-data; name="doc_type"',
    "",
    "farm_photo",
    f"--{boundary}",
    'Content-Disposition: form-data; name="file"; filename="hive.png"',
    "Content-Type: image/png",
    "",
    "",
]
form = CRLF.join(parts).encode() + png + f"{CRLF}--{boundary}--{CRLF}".encode()
s, h, b, _ = call(
    "POST",
    f"/v2/farmers/{farmer_id}/documents",
    token=ftoken,
    raw=form,
    ctype=f"multipart/form-data; boundary={boundary}",
)
doc = js(b)
check(
    "document uploads into MinIO (201)", s == 201, (s, doc if s != 201 else doc.get("scan_status"))
)
if s == 201:
    s, h, b, _ = call("GET", f"/v2/documents/{doc['id']}/download-url", token=ftoken)
    url = js(b).get("url") if s == 200 else None
    got = None
    if url:
        with urllib.request.urlopen(url, timeout=15) as r:
            got = r.read()
    check("signed URL serves the same bytes from MinIO", got == png, s)

# 10. A verification code to Mailpit.
s, h, b, _ = call(
    "POST", f"/v2/farmers/{farmer_id}/verifications", {"channel": "email"}, token=ftoken
)
check("verification code sent by email (202)", s == 202, (s, js(b)))
time.sleep(1)
try:
    with urllib.request.urlopen("http://localhost:8025/api/v1/messages", timeout=10) as r:
        msgs = json.loads(r.read())
    to = [m["To"][0]["Address"] for m in msgs.get("messages", [])]
    check("Mailpit received the code email", f"wanjiru{suffix}@example.test" in to, to[:3])
except Exception as e:
    check("Mailpit received the code email", False, repr(e))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
