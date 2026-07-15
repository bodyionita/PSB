"""Claude provider — primary mind, driven through the headless Claude CLI (ADR-004 / ADR-045).

A Max subscription is not an API key: programmatic use goes through the Claude Agent SDK /
CLI (OAuth), whose credentials live on a Docker volume and are established once with
``claude login`` (07-infrastructure.md).

**One provider, N models (ADR-045).** The CLI takes a per-call ``--model`` flag, so a single
provider instance serves every Claude model (Opus, Sonnet, …) — the registry passes the resolved
model id per call. This replaces the former two fake provider ids over the one CLI: provider ≠
model, and a fallback/error is a single ``claude`` provider event.

**Health-guarded (ADR-012):** if the CLI is absent or not logged in, ``health()`` reports
False and ``complete()`` raises :class:`ProviderUnavailable`, so the chain falls back to
Nebius and records ``fallback_used``. On the VPS, ``claude login`` lights up the real path
with zero code change — nothing here special-cases dev vs prod.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

from .base import ChatMessage, ChatProvider, ProviderUnavailable


def _render_prompt(messages: list[ChatMessage]) -> str:
    """Flatten a message list into a single prompt for the CLI's print mode."""
    parts: list[str] = []
    for m in messages:
        if m.role == "system":
            parts.append(f"[system]\n{m.content}")
        elif m.role == "assistant":
            parts.append(f"[assistant]\n{m.content}")
        else:
            parts.append(m.content)
    return "\n\n".join(parts)


class ClaudeProvider(ChatProvider):
    # The CLI takes ``--effort`` natively, so a per-call effort (ADR-025 §4) is honored.
    supports_effort = True
    # The CLI's ``--effort`` scale (ADR-025 §6, config comment on ``claude_effort``).
    effort_levels = ("low", "medium", "high", "xhigh", "max")

    def __init__(
        self,
        *,
        id: str = "claude",
        models: list[str],
        default_model: str | None = None,
        effort: str = "medium",
        provider_label: str = "Claude",
        cli_path: str = "claude",
    ) -> None:
        if not models:
            raise ValueError("ClaudeProvider requires at least one model")
        self.id = id
        # Friendly PROVIDER name for the ADR-044 Providers card (one row per provider — ADR-045 §6).
        # Model display names are derived per model id by the registry (labels.py), not here.
        self.provider_label = provider_label
        # The vendor model strings this one provider serves via per-call ``--model`` (ADR-045).
        self._models = list(models)
        # The model used when a call passes no explicit ``model=`` (defaults to the first).
        self._default_model = default_model or self._models[0]
        self._effort = effort
        self._cli_name = cli_path

    def chat_model_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    def _resolve_cli(self) -> str | None:
        return shutil.which(self._cli_name)

    async def health(self) -> bool:
        """True only if the CLI exists and responds — cheap, no LLM call, never raises."""
        cli = self._resolve_cli()
        if cli is None:
            return False
        try:
            return await asyncio.to_thread(self._probe_version, cli)
        except Exception:
            return False

    @staticmethod
    def _probe_version(cli: str) -> bool:
        try:
            result = subprocess.run(
                [cli, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    async def complete(
        self, messages: list[ChatMessage], *, model: str | None = None, effort: str | None = None
    ) -> str:
        cli = self._resolve_cli()
        if cli is None:
            raise ProviderUnavailable("claude: CLI not found on PATH")
        prompt = _render_prompt(messages)
        try:
            result = await asyncio.to_thread(
                self._run_cli, cli, prompt, model or self._default_model, effort or self._effort
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProviderUnavailable(f"claude invocation failed: {exc}") from exc
        if result.returncode != 0:
            # Most commonly: not logged in, or usage window exhausted. Fall back.
            raise ProviderUnavailable(
                f"claude returned {result.returncode}: {result.stderr.strip()[:200]}"
            )
        text = result.stdout.strip()
        if not text:
            raise ProviderUnavailable("claude returned empty output")
        return text

    @staticmethod
    def _run_cli(
        cli: str, prompt: str, model: str, effort: str
    ) -> subprocess.CompletedProcess[str]:
        # Pin UTF-8 for the CLI's stdio: the CLI emits UTF-8, but text-mode subprocess otherwise
        # decodes with the host locale (cp1252 on a Windows dev box), which mojibakes any non-ASCII
        # organizer output (e.g. Romanian diacritics) before it reaches the writer. On the Linux
        # container the locale is already UTF-8, so this is a no-op there — pure host-robustness.
        # ``errors="replace"`` keeps a stray invalid byte from raising a ``UnicodeDecodeError`` (a
        # ``ValueError`` that would escape ``complete``'s ``except (OSError, SubprocessError)`` and
        # crash instead of degrading to the fallback chain — rule 7): it degrades to U+FFFD, which
        # folds/slugs away harmlessly downstream.
        return subprocess.run(
            [cli, "--print", "--model", model, "--effort", effort, prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
