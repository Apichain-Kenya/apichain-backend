"""Create the first (root) admin (13 D3). The only way one is ever made.

    docker compose exec api uv run python scripts/create_admin.py \\
        --username groot --full-name "Grace Njeri"

The password is typed twice at a prompt, never passed as an argument or an
environment variable, so it lands in no shell history and no process list.
Refuses if a root admin already exists. Every other account is made by that
admin in the console.
"""

import argparse
import getpass
import sys

from sqlalchemy.orm import Session

from app.database import engine
from app.services import bootstrap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--username", required=True)
    parser.add_argument("--full-name", required=True)
    args = parser.parse_args()

    if not sys.stdin.isatty():
        print("Run this from an interactive terminal: the password is read at a prompt.")
        return 2
    password = getpass.getpass("Password (at least 15 characters): ")
    if getpass.getpass("Type it again: ") != password:
        print("The two passwords differ. Nothing was created.")
        return 1

    with Session(engine) as db:
        try:
            user_id = bootstrap.create_root_admin(
                db, username=args.username, full_name=args.full_name, password=password
            )
        except bootstrap.BootstrapRefused as exc:
            print(f"Refused: {exc}. Nothing was created.")
            return 1
        db.commit()
    print(f"Root admin {args.username!r} created (user {user_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
