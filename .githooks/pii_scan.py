"""PII name-guard scanner (invoked by .githooks/pre-commit).

Reads a staged diff (added lines) on stdin and prints any real-contact FULL name (first + last)
it finds, one per line — the pre-commit hook blocks the commit when this prints anything. Only
full-name combos are caught; lone first or last names are fine. The denylist is stored as SHA-256
of the diacritic-folded, lower-cased name, so the real names NEVER appear in this public repo (a
plaintext denylist would re-leak exactly what ADR-064 redacted). Fabricated stand-ins (Diana Vance,
Horia Fenwick, Madalina Fairfax, …) are the intended replacements and hash to nothing here.
"""

import hashlib
import re
import sys
import unicodedata

DENY = {
    "7530c823b73c09a91ffe99bd36c231c8fc62f5d4459f357b6339b1df209109e1",
    "9aa00c51d9cfa2c950c81a54270b593f797e885cd570332bdf94d8fcbc22903d",
    "4c35dc73f3dd8ba985ec90bc8451fca02cc4f1473db54d468906d43dfe306d9a",
    "c912ef32e411742e0d6ae908485000c8bb46edea7f993d3bdd25bf7fa9100274",
    "3862621803f1fe8a6816ae4f61c1626191c9e4b0cef31e81395a3b58293df410",
    "107e0e06b18e7c29bf3fa5e7269455875829f58a41978878d59fddd500295399",
    "003d8e4cf560ecdd4a72c7769026b14ce34bf2cff84a74a526236116a04af6a5",
    "1f86b3f18dbdf26a12552a8eac9c5db843d06d039a79ff02bc5db5da0a1867df",
    "3e85a2de10515ff5b9de978e388db4b3f715b4ee35cea9d5c6dbd12f55785447",
}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def main() -> None:
    hits: set[str] = set()
    # git diff is UTF-8, but a Windows console defaults stdin/stdout to cp1252, which mangles a
    # diacritic in (missing a match) and crashes printing one out. Force UTF-8 both ways.
    text = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    for line in text.splitlines():
        toks = re.findall(r"[^\W\d_]{2,}", line, re.UNICODE)
        for a, b in zip(toks, toks[1:], strict=False):
            if hashlib.sha256(fold(f"{a} {b}").encode()).hexdigest() in DENY:
                hits.add(f"{a} {b}")
    for h in sorted(hits):
        print(h)


if __name__ == "__main__":
    main()
