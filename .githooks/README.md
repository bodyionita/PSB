# .githooks

Version-controlled git hooks. Currently: **`pre-commit`** — a zero-dependency secret guard
that refuses to commit private keys, credential files, or secret-shaped values (API keys,
tokens, database URLs with inline passwords). It enforces the "Security & secrets discipline"
rule in [`../second-brain-docs/09-session-protocol.md`](../../second-brain-docs/09-session-protocol.md).

## Enable it (once per clone)

Git does not use this directory automatically. Point git at it:

```sh
git config core.hooksPath .githooks
```

That's it — the hook runs on every `git commit`. On Windows it runs under Git Bash (shipped
with Git for Windows), so no extra tooling is needed.

## Notes

- **Do not bypass it for secrets.** `git commit --no-verify` skips all hooks; using it to
  sneak in credentials is forbidden by the session protocol. A committed secret must be
  treated as compromised and rotated immediately.
- This local hook is fast feedback, not the only line of defence. A **gitleaks** job in CI
  (`.github/workflows/ci.yml`) re-scans on every push/PR and cannot be `--no-verify`-skipped.
- Real secrets live only in `deploy/.env` on the VPS (gitignored), the process environment,
  or GitHub Actions secrets. Only `.env.example` (placeholders) is tracked.
- False positive? Prefer fixing the content. If it is genuinely benign, add a scoped
  allowlist entry to `.gitleaks.toml` and, if needed, refine the pattern in `pre-commit` —
  never weaken the hook wholesale.
