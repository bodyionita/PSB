"""NlTimeClassifier tests (ADR-056 §7, M8.2 Task 3-E).

The classifier turns a natural-language date phrase into a resolved absolute time via a `conspect`
call (fake here) + the deterministic resolver. Fail-closed: a "none" classification, unparseable
output, or a down provider all yield ``None`` (never a guessed date — rule 12).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.providers.registry import ProviderRegistry
from app.services.nl_time import NlTimeClassifier

from .fakes import FakeChatProvider, fake_routing

ANCHOR = datetime(2026, 7, 17, 9, 0, 0)


def _classifier(reply: str) -> NlTimeClassifier:
    from app.config import Settings

    registry = ProviderRegistry(
        {"fake-chat": FakeChatProvider("fake-chat", reply=reply)},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    return NlTimeClassifier(settings=Settings(scheduler_tz="UTC"), routing=fake_routing(registry))


@pytest.mark.asyncio
async def test_classifies_a_season_into_a_range():
    reply = '{"phrase": "summer 2019", "kind": "season", "season": "summer", "year": 2019}'
    rt = await _classifier(reply).classify("summer 2019", anchor=ANCHOR)
    assert rt is not None
    assert rt.occurred_start() == date(2019, 6, 1) and rt.occurred_end() == date(2019, 8, 31)
    assert rt.label == "summer 2019"


@pytest.mark.asyncio
async def test_relative_resolves_against_the_anchor():
    reply = '{"phrase": "10 days ago", "kind": "relative", "unit": "day", "offset": -10}'
    rt = await _classifier(reply).classify("10 days ago", anchor=ANCHOR)
    assert rt is not None and rt.occurred_start() == date(2026, 7, 7)


@pytest.mark.asyncio
async def test_kind_none_yields_none():
    rt = await _classifier('{"kind": "none"}').classify("whenever, doesn't matter", anchor=ANCHOR)
    assert rt is None


@pytest.mark.asyncio
async def test_unparseable_output_yields_none():
    rt = await _classifier("I think it was a while ago?").classify("a while ago", anchor=ANCHOR)
    assert rt is None


@pytest.mark.asyncio
async def test_invalid_classification_fails_closed():
    # A structurally-JSON but schema-invalid emission (bad month) resolves to None (no guess).
    reply = '{"phrase": "the 13th month", "kind": "explicit", "year": 2024, "month": 13}'
    rt = await _classifier(reply).classify("the 13th month", anchor=ANCHOR)
    assert rt is None


@pytest.mark.asyncio
async def test_provider_down_yields_none():
    from app.config import Settings

    registry = ProviderRegistry(
        {"fake-chat": FakeChatProvider("fake-chat", available=False)},
        chat_chain=["fake-chat"],
        distill_chain=["fake-chat"],
        embedding_provider_id="none",
        stt_chain=[],
    )
    classifier = NlTimeClassifier(
        settings=Settings(scheduler_tz="UTC"), routing=fake_routing(registry)
    )
    assert await classifier.classify("summer 2019", anchor=ANCHOR) is None


@pytest.mark.asyncio
async def test_blank_phrase_short_circuits():
    rt = await _classifier('{"kind": "none"}').classify("   ", anchor=ANCHOR)
    assert rt is None
