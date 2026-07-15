"""Human-friendly display names for provider chat models (03-api §Chat/§Settings).

The picker + Settings + the chat "answered by" caption show these instead of the raw model id.
Derived from the configured model string (NOT a hardcoded id->name map, CLAUDE.md rule 9), so the
label tracks a config change and keeps the version visible per the user's call
(``claude-opus-4-8`` -> ``Claude Opus 4.8``; ``meta-llama/Llama-3.3-70B-Instruct`` -> ``Llama 3.3
70B``). An unrecognised shape falls back to the raw string, so a new provider/model is shown
verbatim rather than mislabelled.
"""

from __future__ import annotations

import re

_CLAUDE = re.compile(r"claude-([a-z]+)-(\d+)-(\d+)(?:-.*)?$", re.IGNORECASE)
_LLAMA = re.compile(r"(?:meta-)?llama-([\d.]+)-(\d+b)(?:-.*)?$", re.IGNORECASE)

# Legacy-tolerant audit labels (ADR-045 §4). Historical ``chat_messages.model`` rows hold the
# retired provider ids from before the provider/model split — ``claude-max`` (Opus), the fake
# ``claude-max-sonnet`` (Sonnet), and ``nebius`` (its Llama model). Those rows are left untouched
# in the DB (rewriting past audit would falsify the record), so label resolution must still map
# them to a name. Fold each retired id to the *vendor model string* it stood for, then derive the
# label the normal way — no second hardcoded name map, so the display still tracks the derivation
# (rule 9). Kept in lock-step with migration 009's saved-routing remap (same three pairs).
_LEGACY_MODEL_IDS = {
    "claude-max": "claude-opus-4-8",
    "claude-max-sonnet": "claude-sonnet-4-6",
    "nebius": "meta-llama/Llama-3.3-70B-Instruct",
}


def friendly_model_label(model: str) -> str:
    """A display name for ``model`` (see module docstring). Empty in -> empty out."""
    if not model:
        return model
    model = _LEGACY_MODEL_IDS.get(model, model)  # fold retired provider ids → their vendor string
    tail = model.split("/")[-1]
    m = _CLAUDE.fullmatch(tail)
    if m:
        family, major, minor = m.groups()
        return f"Claude {family.capitalize()} {major}.{minor}"
    m = _LLAMA.fullmatch(tail)
    if m:
        version, size = m.groups()
        return f"Llama {version} {size.upper()}"
    return model
