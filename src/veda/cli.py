"""Admin commands. Usage: uv run python -m veda.cli make-admin you@example.com"""

import argparse

from sqlalchemy import text

from veda.db import sync_session


def make_admin(email: str) -> None:
    with sync_session() as s, s.begin():
        user_id = s.execute(
            text("SELECT id FROM auth.users WHERE lower(email) = lower(:email)"), {"email": email}
        ).scalar()
        if user_id is None:
            raise SystemExit(
                f"No Supabase user with email {email}. Create one first in the Supabase "
                "dashboard (Authentication → Users → Add user), then rerun."
            )
        s.execute(
            text("INSERT INTO admins (id) VALUES (:id) ON CONFLICT DO NOTHING"), {"id": user_id}
        )
    print(f"{email} is an admin. They must enrol TOTP in the app before admin routes work.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="veda.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "make-admin", help="Make an existing Supabase user a super-admin"
    ).add_argument("email")
    args = parser.parse_args()
    if args.command == "make-admin":
        make_admin(args.email)


if __name__ == "__main__":
    main()
