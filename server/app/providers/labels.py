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

_CLAUDE = re.compile(r"claude-([a-z]+)-(\d+)-(\d+)(?:-.*)?$")
_LLAMA = re.compile(r"(?:meta-)?llama-([\d.]+)-(\d+b)(?:-.*)?$", re.IGNORECASE)


def friendly_model_label(model: str) -> str:
    """A display name for ``model`` (see module docstring). Empty in -> empty out."""
    if not model:
        return model
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
