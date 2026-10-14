"""Give the root admin a new password, from the server (13 D3).

    docker compose exec api uv run python scripts/reset_root.py

The console never resets the root admin's password (an admin could otherwise
take over the account by resetting it and signing in first), so this is the
recovery path when the root admin has forgotten it. The password is typed
twice at a prompt. Every session the root admin holds ends.
"""

import getpass
import sys

from sqlalchemy.orm import Session

from app.database import engine
from app.services import bootstrap


def main() -> int:
    if not sys.stdin.isatty():
        print("Run this from an interactive terminal: the password is read at a prompt.")
        return 2
    password = getpass.getpass("New root admin password (at least 15 characters): ")
    if getpass.getpass("Type it again: ") != password:
        print("The two passwords differ. Nothing was changed.")
        return 1
    with Session(engine) as db:
        try:
            user_id = bootstrap.reset_root_password(db, password=password)
        except bootstrap.BootstrapRefused as exc:
            print(f"Refused: {exc}. Nothing was changed.")
            return 1
        db.commit()
    print(f"Root admin (user {user_id}) has a new password; every session was signed out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
