"""Safe real-DB validation of PgReprocessStore SQL (ADR-042, M3 task 11).

The reset is destructive (TRUNCATE), so this runs it inside a transaction that is **rolled back** —
the SQL is exercised against real pgvector (TRUNCATE ... CASCADE resolves the FK cascade, the
UPDATE parses) without wiping dev data. The read methods (counts / count_merges / chronological
order) are verified read-only. Refuses a non-local DSN.

    python scripts/smoke_reprocess.py
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.db import Database
from app.services.reprocess import PgReprocessStore
from app.services.review_queue import KIND_STANCE_CANDIDATE

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} {detail}")


async def main() -> int:
    settings = get_settings()
    if "localhost" not in settings.database_url and "127.0.0.1" not in settings.database_url:
        print(f"REFUSING: database_url is not local: {settings.database_url}")
        return 2
    db = Database(settings)
    await db.connect()
    try:
        store = PgReprocessStore(db)

        print("\n== PgReprocessStore read methods (read-only) ==")
        captures, nodes = await store.counts()
        check("counts() returns ints", isinstance(captures, int) and isinstance(nodes, int),
              f"{captures},{nodes}")
        merges = await store.count_merges()
        check("count_merges() returns int", isinstance(merges, int), str(merges))
        ids = await store.capture_ids_chronological()
        check("capture_ids_chronological() returns a list", isinstance(ids, list))

        # Chronological ordering: verify against an explicit ORDER BY on the same data.
        async with db.acquire() as conn:
            want = [str(r["id"]) for r in await conn.fetch(
                "SELECT id FROM captures ORDER BY created_at ASC, id ASC"
            )]
        check("chronological order matches created_at ASC", ids == want)

        print("\n== reset_derived_and_review SQL (in a ROLLED-BACK txn — dev data untouched) ==")
        # Exercise the exact reset statements against real pg on THIS connection, then abort so
        # nothing is lost. (The store method opens its own pooled connection+txn — running it here
        # would commit outside this rollback and wipe dev data — so mirror its statements instead.)
        # Seed one stance-candidate + one other kind first so the kind-aware DELETE (ADR-048 §7) is
        # proven: stance-candidate survives, the rest are cleared.
        async with db.pool.acquire() as conn:  # manual txn control (start → rollback)
            tx = conn.transaction()
            await tx.start()
            try:
                before = await conn.fetchval("SELECT count(*) FROM nodes")
                stance_id = await conn.fetchval(
                    "INSERT INTO review_queue (kind, payload) "
                    "VALUES ($1, '{}'::jsonb) RETURNING id",
                    KIND_STANCE_CANDIDATE,
                )
                await conn.execute(
                    "INSERT INTO review_queue (kind, payload) "
                    "VALUES ('entity-ambiguity', '{}'::jsonb)"
                )
                await conn.execute("TRUNCATE nodes CASCADE")
                # Mirror of PgReprocessStore.reset_derived_and_review's kind-aware DELETE — the
                # constant guards against a silent drift if the preserved kind is ever renamed.
                await conn.execute(
                    "DELETE FROM review_queue WHERE kind <> $1", KIND_STANCE_CANDIDATE
                )
                await conn.execute("UPDATE captures SET node_paths = '{}'")
                after_nodes = await conn.fetchval("SELECT count(*) FROM nodes")
                after_chunks = await conn.fetchval("SELECT count(*) FROM chunks")
                after_edges = await conn.fetchval("SELECT count(*) FROM edges")
                after_profiles = await conn.fetchval("SELECT count(*) FROM node_profiles")
                check("TRUNCATE nodes CASCADE empties nodes", after_nodes == 0, str(after_nodes))
                check("cascade empties chunks", after_chunks == 0, str(after_chunks))
                check("cascade empties edges", after_edges == 0, str(after_edges))
                check("cascade empties node_profiles", after_profiles == 0, str(after_profiles))
                # Kind-aware review reset (ADR-048 §7): stance-candidate preserved, others gone.
                stance_kept = await conn.fetchval(
                    "SELECT count(*) FROM review_queue WHERE id = $1", stance_id
                )
                others_left = await conn.fetchval(
                    "SELECT count(*) FROM review_queue WHERE kind <> 'stance-candidate'"
                )
                check("stance-candidate item PRESERVED", stance_kept == 1, str(stance_kept))
                check("non-stance review kinds cleared", others_left == 0, str(others_left))
                # captures row count is preserved (only node_paths cleared).
                cap_after = await conn.fetchval("SELECT count(*) FROM captures")
                check("captures rows preserved (not truncated)", cap_after == captures,
                      f"{cap_after} vs {captures}")
                print(f"       (rolled back — {before} node(s) restored)")
            finally:
                await tx.rollback()

        # Confirm the rollback: data is back.
        _, nodes_after = await store.counts()
        check("rollback restored nodes (dev data intact)", nodes_after == nodes,
              f"{nodes_after} vs {nodes}")
    finally:
        await db.disconnect()

    print(f"\n==== {_PASS} passed, {_FAIL} failed ====")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
