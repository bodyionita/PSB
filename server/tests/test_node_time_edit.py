"""NodeTimeEditService tests (ADR-056 §5, M8.2 Task 3-F).

The mechanical token edit rewrites a body ``[[t:…]]`` token and, when it is the node's event date,
moves ``occurred`` too — over a real NodeWriter on tmp files, with a fake search service supplying
the node's current state and fakes for the indexer/backup.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.graph.node_writer import NodeDocument, NodeWriter
from app.indexing.frontmatter import parse_node_metadata
from app.services.node_time_edit import BadTimeEdit, NodeNotFound, NodeTimeEditService

from .fakes import FakeIndexer, FakeStoreBackup

CREATED = datetime(2026, 7, 17, 9, 0, 0)
NODE_ID = "99999999-9999-4999-8999-999999999999"


class FakeSearch:
    """Minimal SearchService stand-in: returns a preview (or None) for get_node."""

    def __init__(self, preview) -> None:
        self._preview = preview

    async def get_node(self, node_id: str):
        return self._preview


def _preview(store_path, *, occurred, occurred_end=None, merged_into=None):
    return SimpleNamespace(
        store_path=store_path,
        occurred=occurred,
        occurred_end=occurred_end,
        merged_into=merged_into,
    )


def _write(
    tmp_path: Path, *, body: str, occurred=None, occurred_end=None
) -> tuple[NodeWriter, str]:
    writer = NodeWriter(str(tmp_path))
    [written] = writer.write_nodes(
        [
            NodeDocument(
                id=NODE_ID,
                type="memory",
                title="A trip",
                body=body,
                created_local=CREATED,
                source="text",
                occurred=occurred,
                occurred_end=occurred_end,
            )
        ]
    )
    return writer, written.store_path


def _service(writer, preview):
    indexer, backup = FakeIndexer(), FakeStoreBackup()
    service = NodeTimeEditService(
        search_service=FakeSearch(preview),
        node_writer=writer,
        indexer=indexer,
        store_backup=backup,
    )
    return service, indexer, backup


def _meta(tmp_path: Path, store_path: str):
    raw = (tmp_path / Path(*store_path.split("/"))).read_text(encoding="utf-8")
    return raw, parse_node_metadata(raw, store_path=store_path, fallback_created=CREATED)


@pytest.mark.asyncio
async def test_event_date_token_edit_moves_occurred(tmp_path: Path):
    writer, sp = _write(tmp_path, body="Left [[t:2025-07-07|7 July 2025]].", occurred="2025-07-07")
    service, indexer, backup = _service(writer, _preview(sp, occurred=date(2025, 7, 7)))
    result = await service.edit_token(
        NODE_ID, old_token="[[t:2025-07-07|7 July 2025]]", start="2025-08"
    )

    assert result.occurred_updated is True and result.occurred == "2025-08"
    raw, meta = _meta(tmp_path, sp)
    assert "[[t:2025-08]]" in raw and "[[t:2025-07-07" not in raw
    assert meta.occurred_start == date(2025, 8, 1) and meta.occurred_end == date(2025, 8, 31)
    assert indexer.calls == [[sp]] and backup.reasons  # re-embedded + commit requested


@pytest.mark.asyncio
async def test_non_event_token_leaves_occurred(tmp_path: Path):
    # The node's occurred (2025-07-07) differs from the edited token (a mention of 2020), so the
    # token changes but occurred is untouched.
    writer, sp = _write(
        tmp_path,
        body="Back in [[t:2020|2020]] we met, but I left [[t:2025-07-07|7 July 2025]].",
        occurred="2025-07-07",
    )
    service, _indexer, _backup = _service(writer, _preview(sp, occurred=date(2025, 7, 7)))
    result = await service.edit_token(NODE_ID, old_token="[[t:2020|2020]]", start="2019")

    assert result.occurred_updated is False and result.occurred is None
    raw, meta = _meta(tmp_path, sp)
    assert "[[t:2019]]" in raw
    assert meta.occurred_start == date(2025, 7, 7)  # unchanged


@pytest.mark.asyncio
async def test_range_edit_with_label(tmp_path: Path):
    writer, sp = _write(
        tmp_path, body="It was [[t:2025-07-07|7 July 2025]].", occurred="2025-07-07"
    )
    service, _i, _b = _service(writer, _preview(sp, occurred=date(2025, 7, 7)))
    result = await service.edit_token(
        NODE_ID,
        old_token="[[t:2025-07-07|7 July 2025]]",
        start="2025-06",
        end="2025-08",
        label="summer 2025",
    )
    assert result.occurred_updated is True
    raw, meta = _meta(tmp_path, sp)
    assert "[[t:2025-06/2025-08|summer 2025]]" in raw
    assert meta.occurred_start == date(2025, 6, 1) and meta.occurred_end == date(2025, 8, 31)


@pytest.mark.asyncio
async def test_token_not_in_body_is_bad_edit(tmp_path: Path):
    writer, sp = _write(tmp_path, body="No token here.", occurred=None)
    service, _i, _b = _service(writer, _preview(sp, occurred=None))
    with pytest.raises(BadTimeEdit):
        await service.edit_token(NODE_ID, old_token="[[t:2025-07-07]]", start="2025-08")


@pytest.mark.asyncio
async def test_unknown_node_is_not_found(tmp_path: Path):
    writer, _sp = _write(tmp_path, body="x [[t:2025]]", occurred=None)
    service, _i, _b = _service(writer, None)
    with pytest.raises(NodeNotFound):
        await service.edit_token(NODE_ID, old_token="[[t:2025]]", start="2026")


@pytest.mark.asyncio
async def test_merged_node_is_not_found(tmp_path: Path):
    writer, sp = _write(tmp_path, body="x [[t:2025]]", occurred=None)
    service, _i, _b = _service(writer, _preview(sp, occurred=None, merged_into="survivor"))
    with pytest.raises(NodeNotFound):
        await service.edit_token(NODE_ID, old_token="[[t:2025]]", start="2026")


@pytest.mark.asyncio
async def test_bad_new_date_is_bad_edit(tmp_path: Path):
    writer, sp = _write(tmp_path, body="x [[t:2025]]", occurred=None)
    service, _i, _b = _service(writer, _preview(sp, occurred=None))
    with pytest.raises(BadTimeEdit):
        await service.edit_token(NODE_ID, old_token="[[t:2025]]", start="not-a-date")


@pytest.mark.asyncio
async def test_malformed_old_token_is_bad_edit(tmp_path: Path):
    writer, sp = _write(tmp_path, body="x [[t:2025]]", occurred=None)
    service, _i, _b = _service(writer, _preview(sp, occurred=None))
    with pytest.raises(BadTimeEdit):
        await service.edit_token(NODE_ID, old_token="just some text", start="2026")
