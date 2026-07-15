"""Unit tests for the friendly model-label derivation (app/providers/labels.py, M4 follow-up)."""

import pytest

from app.providers.labels import friendly_model_label


@pytest.mark.parametrize(
    "model,expected",
    [
        # Claude tiers — family capitalised, version kept (user's call: keep model versions).
        ("claude-opus-4-8", "Claude Opus 4.8"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ("claude-haiku-3-5", "Claude Haiku 3.5"),
        ("claude-opus-4-8-20260101", "Claude Opus 4.8"),  # trailing date segment tolerated
        ("Claude-OPUS-4-8", "Claude Opus 4.8"),  # case-insensitive; family normalised
        # Nebius / Llama — vendor prefix + -Instruct suffix stripped, size upper-cased.
        ("meta-llama/Llama-3.3-70B-Instruct", "Llama 3.3 70B"),
        ("meta-llama/Meta-Llama-3.1-70B-Instruct", "Llama 3.1 70B"),  # old id still folds cleanly
        ("meta-llama/Llama-3.3-70b-instruct", "Llama 3.3 70B"),
        # Legacy-tolerant audit labels (ADR-045 §4): retired provider ids on historical
        # `chat_messages.model` rows fold to the vendor model string they stood for, then derive.
        ("claude-max", "Claude Opus 4.8"),
        ("claude-max-sonnet", "Claude Sonnet 4.6"),
        ("nebius", "Llama 3.3 70B"),
        # Unknown shapes fall back to the raw string (never mislabelled).
        ("gpt-4o", "gpt-4o"),
        ("Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-72B-Instruct"),
        ("", ""),
    ],
)
def test_friendly_model_label(model: str, expected: str) -> None:
    assert friendly_model_label(model) == expected
