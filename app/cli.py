import os
import sys

from sqlalchemy import func, select

from app.auth import hash_password, normalize_email
from app.db import SessionLocal
from app.models import Firm, FirmMember, User


def bootstrap_admin() -> None:
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("BOOTSTRAP_ADMIN_PASSWORD must contain at least 12 characters")

    email = normalize_email(
        os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    )
    with SessionLocal.begin() as db:
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing:
            print(f"Admin already exists: {email}")
            return
        firm = Firm(name=os.environ.get("BOOTSTRAP_FIRM_NAME", "Local Accounting"))
        db.add(firm)
        db.flush()
        user = User(
            firm_id=firm.id,
            email=email,
            name=os.environ.get("BOOTSTRAP_ADMIN_NAME", "Local Admin"),
            password_hash=hash_password(password),
        )
        db.add(user)
        db.flush()
        db.add(FirmMember(firm_id=firm.id, user_id=user.id, role="FIRM_ADMIN"))
    print(f"Created firm administrator: {email}")


if __name__ == "__main__":
    if sys.argv[1:] != ["bootstrap-admin"]:
        raise SystemExit("Usage: python -m app.cli bootstrap-admin")
    bootstrap_admin()
