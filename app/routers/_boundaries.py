"""FastAPI dependencies for the four external boundaries (11 D4).

Routes take a boundary through `Depends(get_...)` rather than importing a
module-level client, so tests replace it with `app.dependency_overrides` and
can then inspect exactly what was stored or sent. Each live client is built
once per process.
"""

from functools import cache

from app.services import mailer, scanner, sms, storage


@cache
def get_object_store() -> storage.ObjectStore:
    return storage.from_settings()


@cache
def get_scanner() -> scanner.Scanner:
    return scanner.from_settings()


@cache
def get_sms() -> sms.SmsSender:
    return sms.from_settings()


@cache
def get_email() -> mailer.EmailSender:
    return mailer.from_settings()
