"""Generate an Argon2id hash for API_PASSWORD_HASH (ADR-012).

Usage:
    python scripts/hash_password.py 'your-password'
    python scripts/hash_password.py            # prompts without echoing
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

# Allow running as a plain script (no install) by putting the package on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import hash_password  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Password: ")
    if not password:
        print("Refusing to hash an empty password.", file=sys.stderr)
        raise SystemExit(1)
    print(hash_password(password))


if __name__ == "__main__":
    main()
