# .githooks

Version-controlled git hooks. Currently: **`pre-commit`**, which does two things on every
commit:

1. **Secret guard** — refuses to commit private keys, credential files, or secret-shaped
   values (API keys, tokens, database URLs with inline passwords). Enforces the "Security &
   secrets discipline" rule in
   [`../second-brain-docs/09-session-protocol.md`](../../second-brain-docs/09-session-protocol.md).
2. **Lint + format gate** — runs the exact linters CI runs, scoped to the **staged** files,
   so a lint/format error can never reach `main` and break the deploy (CLAUDE.md rule 10,
   "CI must stay green per directory"):
   - **Python** (staged `server/**.py`): `ruff check` + `ruff format --check`.
   - **Web** (staged `web/**.{ts,tsx,js,jsx,cjs,mjs}`): `eslint --max-warnings 0` (the web
     has no separate formatter — eslint is the format authority).

   Scoping to staged files means pre-existing drift elsewhere never blocks an unrelated
   commit, and a server-only commit never invokes the web toolchain. The gate needs `uv`
   (Python) / `pnpm` (web) on PATH only when that subtree has staged changes; if the tool is
   missing it **fails closed** with a clear message rather than skipping the check.

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
  treated as compromised and rotated immediately. `--no-verify` also defeats the lint gate —
  don't reach for it to dodge a ruff/eslint error; fix the code.
- **Lint gate is staged-scoped, not whole-repo.** It keeps each commit's own files clean; it
  does not retroactively enforce format across untouched files (the repo has some pre-existing
  `ruff format` drift CI never gated — clean it up incrementally as you touch files).
- This local hook is fast feedback, not the only line of defence. A **gitleaks** job in CI
  (`.github/workflows/ci.yml`) re-scans on every push/PR and cannot be `--no-verify`-skipped.
- Real secrets live only in `deploy/.env` on the VPS (gitignored), the process environment,
  or GitHub Actions secrets. Only `.env.example` (placeholders) is tracked.
- False positive? Prefer fixing the content. If it is genuinely benign, add a scoped
  allowlist entry to `.gitleaks.toml` and, if needed, refine the pattern in `pre-commit` —
  never weaken the hook wholesale.
