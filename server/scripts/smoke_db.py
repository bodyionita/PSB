"""Real-DB SQL smoke for M3 task-6/7a/7b stores + alias resolution + profile-in-search.

Drives the ACTUAL Pg* store classes against the running local pgvector Postgres (what the
unit tests fake). Catches column typos, pgvector codec issues, ANY/array/unnest SQL, the
merge reverse-index join, the vocab jsonb upsert, and the edge-consolidation inventory.

Isolated: every row uses a fixed test UUID / 'smoke::' store_path prefix and is deleted in a
finally, leaving pre-existing dev data untouched. Read-only against schema (no DDL).

Run:  uv run python <path>/smoke_db.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.chat.auto_recorded import PgAutoRecordedStore
from app.chat.distill_store import PgChatDistillStore
from app.chat.store import PgChatStore
from app.config import Settings
from app.db import Database
from app.dedup.store import PgDedupStore
from app.entities.entity_store import PgEntityStore
from app.entities.profile_store import PgProfileStore
from app.entities.store import PgAliasStore
from app.graph.store import PgNeighborStore
from app.identity.store import (
    CAPSULE_KEY,
    CapsuleBlob,
    PgCapsuleSourceStore,
    PgIdentityCapsuleStore,
)
from app.oauth.store import PgOAuthStore
from app.search.store import PgSearchStore, RetrievalParams
from app.services.agent_runs import (
    FAILED,
    SUCCEEDED,
    PgAgentRunStore,
    child_run_scope,
)
from app.services.capture_store import INDEXED, KIND_TEXT, PgCaptureStore
from app.services.reprocess import PgReprocessStore
from app.services.review_queue import (
    KIND_DEDUP_PROPOSAL,
    KIND_STANCE_CANDIDATE,
    PgReviewQueue,
    ReviewItem,
)
from app.vocab.edge_store import PgEdgeConsolidationStore
from app.vocab.store import PgVocabularyStore

# --- fixed test ids (uuid) -------------------------------------------------
ALEX = "aaaaaaaa-0000-0000-0000-000000000001"  # person, aliases Alex/Alexandru
GHOST = "aaaaaaaa-0000-0000-0000-0000000000ff"  # person, tombstoned (merged into ALEX)
PLACE = "aaaaaaaa-0000-0000-0000-000000000002"  # place
MEM1 = "aaaaaaaa-0000-0000-0000-000000000010"  # memory (edge -> ALEX)
MEM2 = "aaaaaaaa-0000-0000-0000-000000000011"  # memory (mentions Alex, no edge -> backfill)
ALL_IDS = [ALEX, GHOST, PLACE, MEM1, MEM2]
VOCAB_KEY = "vocabulary"

DIM = 768
_passes = 0
_fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passes, _fails
    mark = "PASS" if cond else "FAIL"
    if cond:
        _passes += 1
    else:
        _fails += 1
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))


def vec(seed: float) -> list[float]:
    """A deterministic unit-ish 768-vector; distinct seeds → distinct directions."""
    v = [0.0] * DIM
    v[0] = 1.0
    v[1] = seed
    return v


def rp(settings: Settings, **over) -> RetrievalParams:
    """A default hybrid RetrievalParams (Settings-driven knobs), overridable per check."""
    base = dict(
        top_k=10,
        candidates=60,
        rrf_k=settings.search_rrf_k,
        recency_half_life_days=settings.search_recency_half_life_days,
        recency_floor=settings.search_recency_floor,
        min_score=0.0,
        planes=None,
        types=None,
        since=None,
        until=None,
        as_of=None,
    )
    base.update(over)
    return RetrievalParams(**base)


async def seed(db: Database) -> None:
    now = datetime.now(UTC)
    async with db.transaction() as c:
        # nodes
        await c.execute(
            """
            INSERT INTO nodes (id, store_path, type, title, plane, planes, tags, aliases,
                disambig, occurred_start, merged_into, content_hash, embedding,
                node_created_at, indexed_at)
            VALUES
             ($1,'smoke::person/alex.md','person','Alex','personal','{personal}','{}',
              '{Alex,Alexandru}','the tall one', '2026-01-01', NULL, 'h_alex', $6, $7, $7),
             ($2,'smoke::person/ghost.md','person','Ghost','personal','{personal}','{}',
              '{Ghost}', NULL, NULL, $1, 'h_ghost', NULL, $7, $7),
             ($3,'smoke::place/office.md','place','Office','personal','{personal}','{}',
              '{Office}', NULL, NULL, NULL, 'h_place', NULL, $7, $7),
             ($4,'smoke::memory/m1.md','memory','Met Alex at the office','personal',
              '{personal}','{}','{}', NULL, '2026-02-01', NULL, 'h_m1', $8, $7, $7),
             ($5,'smoke::memory/m2.md','memory','Alexandru called again','personal',
              '{personal}','{}','{}', NULL, '2026-03-01', NULL, 'h_m2', $9, $7, $7)
            """,
            ALEX,
            GHOST,
            PLACE,
            MEM1,
            MEM2,
            vec(0.10),
            now,
            vec(0.20),
            vec(0.21),
        )
        # edges: MEM1 -involves-> ALEX (canonical), ALEX -at-> PLACE (canonical),
        #        GHOST -similar-> MEM2 (derived, from tombstone src → hidden from canonical reads),
        #        MEM1 -knows-> GHOST (canonical, tombstoned TARGET → hidden from the out-leg)
        await c.execute(
            """
            INSERT INTO edges (src_id, dst_id, rel, origin, score, since, until) VALUES
             ($1,$2,'involves','canonical',NULL,'2026-02-01',NULL),
             ($2,$3,'at','canonical',NULL,'2026-01-05',NULL),
             ($4,$5,'similar','derived',0.87,NULL,NULL),
             ($1,$4,'knows','canonical',NULL,NULL,NULL)
            """,
            MEM1,
            ALEX,
            PLACE,
            GHOST,
            MEM2,
        )
        # chunk for MEM2 mentioning the alias (backfill candidate + search hit)
        await c.execute(
            """
            INSERT INTO chunks (node_id, chunk_index, content, embedding)
            VALUES ($1, 0, 'Alexandru called again about the office project', $2)
            """,
            MEM2,
            vec(0.21),
        )
        # chunk for MEM1 (edge inventory src excerpt)
        await c.execute(
            """
            INSERT INTO chunks (node_id, chunk_index, content, embedding)
            VALUES ($1, 0, 'Met Alex at the office today', $2)
            """,
            MEM1,
            vec(0.20),
        )


def _load_migration_sql() -> tuple[str, str]:
    """The EXACT upgrade/downgrade SQL from migration 009 (numeric module name → load by path)."""
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "009_model_routing_id_migration.py"
    )
    spec = importlib.util.spec_from_file_location("_mig009", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._UPGRADE_SQL, mod._DOWNGRADE_SQL


async def check_model_routing_migration(db: Database) -> None:
    """Drive migration 009's real SQL against Postgres (ADR-045 §4, M4 follow-up 3 task 2).

    Backs up any real ``model_routing`` row, runs the actual upgrade statement over seeded
    old-shape data, and restores — asserting the remap, the effort-key rename, idempotency, a
    downgrade round-trip, and the no-op guards (empty + absent)."""
    up_sql, down_sql = _load_migration_sql()
    print("\n== migration 009 (saved model_routing → model ids, ADR-045 §4) ==")
    async with db.acquire() as conn:
        backup = await conn.fetchval("SELECT value FROM app_settings WHERE key = 'model_routing'")
    try:
        old = {
            "chat": {
                "active": "claude-max",
                "fallback": "nebius",
                "effort_by_provider": {"claude-max": "high"},
            },
            # conspect exercises a group where BOTH active and fallback remap (Claude→Claude).
            "conspect": {
                "active": "claude-max",
                "fallback": "claude-max-sonnet",
                "effort_by_provider": {"claude-max": "medium", "claude-max-sonnet": "low"},
            },
            "quick": {
                "active": "claude-max-sonnet",
                "fallback": "nebius",
                "effort_by_provider": {"claude-max-sonnet": "low"},
            },
        }
        async with db.transaction() as c:
            await c.execute(
                "INSERT INTO app_settings (key, value) VALUES ('model_routing', $1::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = $1::jsonb",
                json.dumps(old),
            )
            await c.execute(up_sql)
        async with db.acquire() as conn:
            got = json.loads(
                await conn.fetchval("SELECT value FROM app_settings WHERE key = 'model_routing'")
            )
        check(
            "active/fallback remapped to model ids",
            got["chat"]["active"] == "claude-opus-4-8"
            and got["chat"]["fallback"] == "meta-llama/Llama-3.3-70B-Instruct"
            and got["quick"]["active"] == "claude-sonnet-4-6",
            str(got),
        )
        check(
            "effort_by_provider → effort_by_model (key renamed, keys remapped)",
            got["chat"].get("effort_by_model") == {"claude-opus-4-8": "high"}
            and "effort_by_provider" not in got["chat"]
            and got["quick"]["effort_by_model"] == {"claude-sonnet-4-6": "low"},
            str(got),
        )
        check(
            "both active + fallback remap in one group (conspect)",
            got["conspect"]["active"] == "claude-opus-4-8"
            and got["conspect"]["fallback"] == "claude-sonnet-4-6"
            and got["conspect"]["effort_by_model"]
            == {"claude-opus-4-8": "medium", "claude-sonnet-4-6": "low"},
            str(got),
        )

        # Idempotent: a second pass over the already-migrated row changes nothing.
        async with db.transaction() as c:
            await c.execute(up_sql)
        async with db.acquire() as conn:
            again = json.loads(
                await conn.fetchval("SELECT value FROM app_settings WHERE key = 'model_routing'")
            )
        check("re-running upgrade is idempotent (no double-remap)", again == got, str(again))

        # Downgrade round-trip: new ids/keys map back to the old provider vocabulary (best-effort).
        async with db.transaction() as c:
            await c.execute(down_sql)
        async with db.acquire() as conn:
            reverted = json.loads(
                await conn.fetchval("SELECT value FROM app_settings WHERE key = 'model_routing'")
            )
        check(
            "downgrade round-trip restores old provider ids + effort_by_provider",
            reverted == old,
            str(reverted),
        )

        # No-op on an empty object: the guard skips a row with no old tokens.
        async with db.transaction() as c:
            await c.execute(
                "UPDATE app_settings SET value = '{}'::jsonb WHERE key = 'model_routing'"
            )
            await c.execute(up_sql)
        async with db.acquire() as conn:
            empty = await conn.fetchval(
                "SELECT value FROM app_settings WHERE key = 'model_routing'"
            )
        check(
            "empty {} row is a no-op (guard skips, never NULLed)",
            json.loads(empty) == {},
            str(empty),
        )

        # No-op on an absent row: nothing to migrate, no error, no row created.
        async with db.transaction() as c:
            await c.execute("DELETE FROM app_settings WHERE key = 'model_routing'")
            await c.execute(up_sql)
        async with db.acquire() as conn:
            absent = await conn.fetchval("SELECT 1 FROM app_settings WHERE key = 'model_routing'")
        check("absent row stays absent (no-op, no error)", absent is None, str(absent))
    finally:
        async with db.transaction() as c:
            await c.execute("DELETE FROM app_settings WHERE key = 'model_routing'")
            if backup is not None:
                # backup is the text asyncpg returns for a jsonb column (no jsonb codec is
                # registered) — cast back explicitly so the NOT NULL jsonb column accepts it.
                await c.execute(
                    "INSERT INTO app_settings (key, value) VALUES ('model_routing', $1::jsonb)",
                    backup,
                )


async def check_identity_capsule(db: Database) -> None:
    """Drive the M5 task-2 capsule stores against real pg (ADR-046 §5): the degree-ranked hub read,
    recent-node ordering, tombstone exclusion, and the app_settings blob round-trip.

    The source reads are GLOBAL (not smoke-scoped), so assert the smoke rows as a subset (present +
    correctly ordered/ranked, tombstone excluded) against a dev DB that may hold real nodes. The
    blob write backs up + restores any real ``identity_capsule`` row so dev state is untouched."""
    print("\n== PgIdentityCapsuleStore + PgCapsuleSourceStore (M5 task 2, ADR-046 §5) ==")
    caps = PgIdentityCapsuleStore(db)
    src = PgCapsuleSourceStore(db)

    # top_profile_hubs: ALEX has a profile + canonical degree 2 (involves-in + at-out); GHOST has a
    # profile too but is tombstoned (merged into ALEX) → excluded; PLACE has no profile → not a hub.
    hubs = {h.node_id: h for h in await src.top_profile_hubs(500)}
    check(
        "top_profile_hubs includes ALEX at canonical degree 2, excludes tombstone GHOST",
        ALEX in hubs and hubs[ALEX].degree == 2 and GHOST not in hubs,
        str({k: v.degree for k, v in hubs.items()}),
    )

    # recent_memories: MEM2 (occurred 2026-03) before MEM1 (2026-02), first-chunk excerpt rides.
    mems = await src.recent_memories(500)
    smoke_order = [m.node_id for m in mems if m.node_id in (MEM1, MEM2)]
    mem1 = next((m for m in mems if m.node_id == MEM1), None)
    check(
        "recent_memories orders MEM2 before MEM1 (occurred desc) + carries a chunk excerpt",
        smoke_order == [MEM2, MEM1] and mem1 is not None and "Alex" in (mem1.excerpt or ""),
        str(smoke_order),
    )
    # recent_insights: none of the smoke nodes are insights (memory/person/place) → excluded.
    ins = await src.recent_insights(500)
    smoke_ids = (MEM1, MEM2, ALEX, PLACE, GHOST)
    check(
        "recent_insights excludes the smoke non-insight nodes",
        all(i.node_id not in smoke_ids for i in ins),
        str([i.node_id for i in ins]),
    )

    # Blob round-trip, isolated: back up any real capsule row, exercise save/current, restore.
    async with db.acquire() as conn:
        backup = await conn.fetchval("SELECT value FROM app_settings WHERE key = $1", CAPSULE_KEY)
    try:
        await caps.save(
            CapsuleBlob(
                text="smoke capsule text",
                generated_at=datetime.now(UTC),
                source_refs=[{"node_id": ALEX, "title": "Alex", "kind": "hub"}],
            )
        )
        got = await caps.current()
        check(
            "capsule save/current round-trips text + generated_at + refs",
            got is not None
            and got.text == "smoke capsule text"
            and got.generated_at is not None
            and got.source_refs[0]["node_id"] == ALEX,
            str(got),
        )
        await caps.save(CapsuleBlob(text="smoke capsule updated"))  # ON CONFLICT update path
        got2 = await caps.current()
        check(
            "capsule save upserts (ON CONFLICT)",
            got2 is not None and got2.text == "smoke capsule updated",
            str(got2),
        )
    finally:
        async with db.transaction() as c:
            await c.execute("DELETE FROM app_settings WHERE key = $1", CAPSULE_KEY)
            if backup is not None:
                await c.execute(
                    "INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)",
                    CAPSULE_KEY,
                    backup,
                )


async def cleanup(db: Database) -> None:
    async with db.transaction() as c:
        await c.execute("DELETE FROM node_profiles WHERE node_id = ANY($1::uuid[])", ALL_IDS)
        await c.execute("DELETE FROM chunks WHERE node_id = ANY($1::uuid[])", ALL_IDS)
        await c.execute(
            "DELETE FROM edges WHERE src_id = ANY($1::uuid[]) OR dst_id = ANY($1::uuid[])", ALL_IDS
        )
        # break the tombstone self-reference before deleting nodes
        await c.execute("UPDATE nodes SET merged_into = NULL WHERE id = ANY($1::uuid[])", ALL_IDS)
        await c.execute("DELETE FROM nodes WHERE id = ANY($1::uuid[])", ALL_IDS)
        await c.execute(
            "DELETE FROM app_settings WHERE key = $1 AND value::text LIKE '%smoke_rel%'", VOCAB_KEY
        )
        # Chat smoke sessions (random uuids) are marked by a 'smoke::' title; messages cascade.
        await c.execute("DELETE FROM chat_sessions WHERE title LIKE 'smoke::%'")


async def check_oauth(db: Database) -> None:
    """PgOAuthStore (M5 task 3) — the un-fakeable atomic single-use code consume, the revoke-all
    sweep's affected-row count, and the client→codes/tokens FK cascade. Self-contained + isolated:
    one 'smoke_' client, cleaned up in a finally (cascade removes its codes + tokens)."""
    print("\n== PgOAuthStore (M5 task 3 — OAuth clients / single-use codes / tokens) ==")
    store = PgOAuthStore(db)
    cid = "smoke_oauth_client"
    try:
        await store.create_client(
            client_id=cid,
            client_secret_hash=None,
            metadata={"redirect_uris": ["https://claude.ai/cb"], "client_name": "Smoke"},
        )
        got = await store.get_client(cid)
        check(
            "get_client round-trips metadata jsonb",
            got is not None and got.metadata["redirect_uris"] == ["https://claude.ai/cb"],
            str(got),
        )

        soon = datetime.now(UTC) + timedelta(minutes=5)
        await store.create_code(
            code_hash="smoke_code_hash",
            client_id=cid,
            redirect_uri="https://claude.ai/cb",
            code_challenge="chal",
            code_challenge_method="S256",
            scope="brain",
            resource="https://x/mcp",
            expires_at=soon,
        )
        first = await store.consume_code("smoke_code_hash")
        check(
            "consume_code returns the bound params once",
            first is not None and first.scope == "brain" and first.client_id == cid,
            str(first),
        )
        second = await store.consume_code("smoke_code_hash")
        check("consume_code is single-use (2nd time -> None)", second is None, str(second))
        replay_owner = await store.consumed_code_client("smoke_code_hash")
        check(
            "consumed_code_client flags the replay (returns owner)",
            replay_owner == cid,
            str(replay_owner),
        )

        # An expired code never consumes (real now()-vs-expires_at comparison).
        await store.create_code(
            code_hash="smoke_code_expired",
            client_id=cid,
            redirect_uri="https://claude.ai/cb",
            code_challenge="c",
            code_challenge_method="S256",
            scope="brain",
            resource=None,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        check("expired code -> None", (await store.consume_code("smoke_code_expired")) is None)

        access_exp = datetime.now(UTC) + timedelta(hours=1)
        refresh_exp = datetime.now(UTC) + timedelta(days=60)
        tid = await store.create_token(
            client_id=cid,
            token_hash="smoke_access",
            kind="access",
            scope="brain",
            resource=None,
            expires_at=access_exp,
        )
        check("create_token returns an id", bool(tid))
        await store.create_token(
            client_id=cid,
            token_hash="smoke_refresh",
            kind="refresh",
            scope="brain",
            resource=None,
            expires_at=refresh_exp,
        )
        rec = await store.get_token("smoke_access")
        check(
            "get_token round-trips kind/scope/liveness",
            rec is not None and rec.kind == "access" and rec.revoked_at is None,
            str(rec),
        )

        # revoke_token returns the affected-row count — the refresh-rotation race-decider (finding
        # 1). Use a throwaway token so the revoke_all count below stays 2 (access + refresh).
        await store.create_token(
            client_id=cid,
            token_hash="smoke_throwaway",
            kind="access",
            scope="brain",
            resource=None,
            expires_at=access_exp,
        )
        check(
            "revoke_token returns 1 then 0 (idempotent rowcount)",
            (await store.revoke_token("smoke_throwaway")) == 1
            and (await store.revoke_token("smoke_throwaway")) == 0,
        )
        gone = await store.get_token("smoke_throwaway")
        check(
            "revoked token carries revoked_at",
            gone is not None and gone.revoked_at is not None,
            str(gone),
        )
        # NOTE: revoke_all() + invalidate_all_codes() are intentionally GLOBAL writes (the
        # "revoke all MCP access" switch), so they are NOT smoked here against the shared dev DB —
        # a global sweep would revoke real dev tokens. They share the `_rowcount` helper (exercised
        # by revoke_token above) and trivial SQL, and are covered by the OAuth unit tests + E2E.

        # FK cascade: dropping the client removes its codes + tokens.
        await db.pool.execute("DELETE FROM mcp_oauth_clients WHERE client_id = $1", cid)
        orphan_tokens = await db.pool.fetchval(
            "SELECT count(*) FROM mcp_tokens WHERE client_id = $1", cid
        )
        orphan_codes = await db.pool.fetchval(
            "SELECT count(*) FROM mcp_auth_codes WHERE client_id = $1", cid
        )
        check(
            "client delete cascades to tokens + codes",
            orphan_tokens == 0 and orphan_codes == 0,
            f"tok={orphan_tokens} code={orphan_codes}",
        )
    finally:
        await db.pool.execute("DELETE FROM mcp_oauth_clients WHERE client_id = $1", cid)


async def check_agent_runs_linkage(db: Database) -> None:
    """M5.5 task 1 (ADR-047 §5): the real ``agent_runs`` parent/child linkage — the
    ``parent_run_id`` column + FK (migration 012) driven through :class:`PgAgentRunStore` and the
    ``child_run_scope`` contextvar, which the unit tests can only fake."""
    print("\n== PgAgentRunStore parent/child linkage (M5.5 task 1 — migration 012) ==")
    runs = PgAgentRunStore(db)
    opened: list[str] = []
    try:
        # A bare start (no active scope) is parentless — the standalone-job path, unchanged.
        bare = await runs.start("smoke-bare")
        opened.append(bare)
        bare_row = await runs.get(bare)
        check(
            "bare run is parentless",
            bare_row is not None and bare_row.parent_run_id is None,
            str(bare_row),
        )

        # A pipeline parent + two child steps opened inside child_run_scope.
        parent = await runs.start("smoke-nightly")
        opened.append(parent)
        with child_run_scope(parent) as collected:
            c1 = await runs.start("smoke-step-a")
            await runs.finish(c1, status=SUCCEEDED, summary="a ok")
            c2 = await runs.start("smoke-step-b")
            await runs.finish(c2, status=FAILED, summary="b failed", error="boom")
        opened += [c1, c2]
        check("scope collected both child run ids", collected == [c1, c2], str(collected))

        c1_row = await runs.get(c1)
        c2_row = await runs.get(c2)
        parent_row = await runs.get(parent)
        check(
            "child A links to the parent (FK column round-trips)",
            c1_row is not None and c1_row.parent_run_id == parent,
            str(c1_row),
        )
        check(
            "child B links to the parent + status/error persisted",
            c2_row is not None
            and c2_row.parent_run_id == parent
            and c2_row.status == FAILED
            and c2_row.error == "boom",
            str(c2_row),
        )
        check(
            "parent stays parentless (opened outside any scope)",
            parent_row is not None and parent_row.parent_run_id is None,
            str(parent_row),
        )

        # After the scope exits, a start is parentless again (contextvar reset).
        after = await runs.start("smoke-after")
        opened.append(after)
        after_row = await runs.get(after)
        check(
            "start after the scope is parentless again (contextvar reset)",
            after_row is not None and after_row.parent_run_id is None,
            str(after_row),
        )
    finally:
        if opened:
            await db.pool.execute("DELETE FROM agent_runs WHERE id = ANY($1::uuid[])", opened)


async def check_chat_distill(db: Database) -> None:
    """M6 task 1 (ADR-048): the real ``PgChatDistillStore`` (idle-eligibility query, delta-after-
    watermark, upsert) + the ``captures.source_ref`` column — the un-fakeable GROUP BY/LEFT JOIN SQL
    and jsonb-less watermark upsert (migration 013)."""
    print("\n== PgChatDistillStore + captures.source_ref (M6 task 1 — migration 013) ==")
    chat = PgChatStore(db)
    store = PgChatDistillStore(db)
    captures = PgCaptureStore(db)
    now = datetime.now(UTC)
    sid = await chat.create_session()
    cap_ids: list[str] = []
    try:
        # Three idle messages (2 days old) so the session is distillable at a 12h cutoff. Explicit
        # created_at (add_message defaults to now()) so the watermark math is deterministic.
        times = [now - timedelta(days=2) + timedelta(minutes=i) for i in range(3)]
        for i, (role, ts) in enumerate(zip(["user", "assistant", "user"], times, strict=True)):
            await db.pool.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at)"
                " VALUES ($1, $2, $3, $4)",
                sid,
                role,
                f"smoke turn {i}",
                ts,
            )
        cutoff = now - timedelta(hours=12)

        due = await store.distillable_sessions(idle_cutoff=cutoff, limit=200)
        mine = next((d for d in due if d.session_id == sid), None)
        check(
            "idle session with no watermark is distillable",
            mine is not None and mine.watermark is None and mine.newest_at == times[2],
            str(mine),
        )

        delta_all = await store.delta_messages(sid, after=None, limit=300)
        check(
            "delta (after=None) returns all msgs oldest-first",
            [m.content for m in delta_all] == ["smoke turn 0", "smoke turn 1", "smoke turn 2"],
            str([m.content for m in delta_all]),
        )
        delta_after = await store.delta_messages(sid, after=times[1], limit=300)
        check(
            "delta after a watermark returns only newer msgs",
            [m.content for m in delta_after] == ["smoke turn 2"],
            str(delta_after),
        )

        # Advance to the newest — the session is no longer distillable (max == watermark, not >).
        await store.advance_watermark(sid, last_message_at=times[2], run_id=None)
        due2 = await store.distillable_sessions(idle_cutoff=cutoff, limit=200)
        check(
            "session drops out after the watermark advances (idempotent)",
            all(d.session_id != sid for d in due2),
            str([d.session_id for d in due2]),
        )

        # A new message after the watermark makes it eligible again; the upsert overwrites in place.
        newer = now - timedelta(days=1)
        await db.pool.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at)"
            " VALUES ($1,$2,$3,$4)",
            sid,
            "user",
            "smoke turn 3",
            newer,
        )
        due3 = await store.distillable_sessions(idle_cutoff=cutoff, limit=200)
        mine3 = next((d for d in due3 if d.session_id == sid), None)
        check(
            "new activity after the watermark re-eligibles the session",
            mine3 is not None and mine3.watermark == times[2] and mine3.newest_at == newer,
            str(mine3),
        )
        await store.advance_watermark(sid, last_message_at=newer, run_id=None)
        row = await db.pool.fetchrow(
            "SELECT last_message_at FROM chat_distill_state WHERE session_id = $1", sid
        )
        check(
            "advance_watermark upserts in place (one row)",
            row["last_message_at"] == newer,
            str(row),
        )

        # session_state (M6 task 3 — the on-demand `remember` path): by-id watermark + newest, no
        # idle filter. Here the watermark is at the newest message, so remember would skip.
        state = await store.session_state(sid)
        check(
            "session_state returns watermark + newest for a known session",
            state is not None and state.watermark == newer and state.newest_at == newer,
            str(state),
        )
        unknown = await store.session_state("dddddddd-0000-0000-0000-0000000000d1")
        check(
            "session_state returns None for an unknown session (→404)",
            unknown is None,
            str(unknown),
        )
        # A session that exists but has no messages: known (not None) but newest_at NULL → the
        # remember endpoint skips (nothing to distill), never 404.
        empty_sid = await chat.create_session()
        try:
            empty_state = await store.session_state(empty_sid)
            check(
                "session_state: empty session is known with newest_at=None (→skip, not 404)",
                empty_state is not None
                and empty_state.newest_at is None
                and empty_state.watermark is None,
                str(empty_state),
            )
        finally:
            await db.pool.execute("DELETE FROM chat_sessions WHERE id = $1", empty_sid)

        # captures.source_ref: an endorsed chat capture carries source=chat + source_ref=session-id.
        cap_id = "cccccccc-0000-0000-0000-0000000000c1"
        cap_ids.append(cap_id)
        await captures.create(
            capture_id=cap_id,
            kind=KIND_TEXT,
            status=INDEXED,
            raw_text="The user decided X.",
            source="chat",
            source_ref=sid,
        )
        got = await captures.get(cap_id)
        check(
            "captures.source_ref round-trips (source=chat, source_ref=session-id)",
            got is not None and got.source == "chat" and got.source_ref == sid,
            str(got),
        )
    finally:
        if cap_ids:
            await db.pool.execute("DELETE FROM captures WHERE id = ANY($1::uuid[])", cap_ids)
        # Deleting the session cascades chat_messages + chat_distill_state (FK ON DELETE CASCADE).
        await db.pool.execute("DELETE FROM chat_sessions WHERE id = $1", sid)


async def check_review_queue_reopen(db: Database) -> None:
    """M6 task 2 (ADR-048 §7): the real ``PgReviewQueue.resolve`` guard is decidable = ``pending`` ∪
    ``maybe`` (the ``status = ANY($::text[])`` SQL a fake can't validate). A parked ``maybe`` must
    re-open to a later verdict; ``resolved`` must be terminal."""
    print("\n== PgReviewQueue.resolve maybe-reopen (M6 task 2) ==")
    q = PgReviewQueue(db)
    rid = await q.enqueue(
        ReviewItem(
            kind=KIND_STANCE_CANDIDATE,
            payload={"candidate_text": "smoke stance", "anchor_at": "2026-07-10T00:00:00+00:00"},
            excerpt="smoke",
            source="chat",
            source_ref="smoke-session",
        )
    )
    try:
        parked = await q.resolve(rid, status="maybe", resolution={"verdict": "maybe"})
        check("pending → maybe transitions", parked is True)
        row = await q.get(rid)
        check("item is parked as maybe", row is not None and row.status == "maybe", str(row))

        reopened = await q.resolve(rid, status="resolved", resolution={"verdict": "agree"})
        check("maybe re-opens → resolved (the guard fix)", reopened is True)
        row2 = await q.get(rid)
        check("re-opened item is now resolved", row2 is not None and row2.status == "resolved")

        terminal = await q.resolve(rid, status="discarded", resolution={"verdict": "disagree"})
        check("resolved is terminal (no-op resolve)", terminal is False)
    finally:
        await db.pool.execute("DELETE FROM review_queue WHERE id = $1", rid)


async def check_maybe_digest_stats(db: Database) -> None:
    """M6 task 8 (ADR-048 §8): the real ``PgReviewQueue.maybe_kind_stats`` aggregate — the weekly
    maybe-digest's ``GROUP BY status='maybe'`` (per-kind count + oldest ``min(created_at)``,
    n-DESC-ordered) that a fake can't validate. Only ``maybe`` rows count; ``pending`` excluded."""
    print("\n== PgReviewQueue.maybe_kind_stats (M6 task 8) ==")
    q = PgReviewQueue(db)
    ids: list[str] = []
    try:
        # Two parked stance-candidates + one parked dedup-proposal + one still-pending (excluded).
        for i in range(2):
            rid = await q.enqueue(ReviewItem(kind=KIND_STANCE_CANDIDATE, payload={"n": i}))
            await q.resolve(rid, status="maybe", resolution={"verdict": "maybe"})
            ids.append(rid)
        rid_dedup = await q.enqueue(ReviewItem(kind=KIND_DEDUP_PROPOSAL, payload={"x": 1}))
        await q.resolve(rid_dedup, status="maybe", resolution={"action": "maybe"})
        ids.append(rid_dedup)
        rid_pending = await q.enqueue(ReviewItem(kind=KIND_STANCE_CANDIDATE, payload={"p": 1}))
        ids.append(rid_pending)  # left pending → must NOT appear in the maybe aggregate

        stats = await q.maybe_kind_stats()
        by_kind = {s.kind: s for s in stats}
        check(
            "stance-candidate maybe count = 2",
            by_kind.get(KIND_STANCE_CANDIDATE) is not None
            and by_kind[KIND_STANCE_CANDIDATE].count == 2,
            str(stats),
        )
        check(
            "dedup-proposal maybe count = 1",
            by_kind.get(KIND_DEDUP_PROPOSAL) is not None
            and by_kind[KIND_DEDUP_PROPOSAL].count == 1,
        )
        check(
            "oldest_created_at is populated",
            all(s.oldest_created_at is not None for s in stats),
        )
        # n DESC ordering: the 2-count kind sorts before the 1-count kind.
        check(
            "ordered by count desc",
            [s.kind for s in stats][:2] == [KIND_STANCE_CANDIDATE, KIND_DEDUP_PROPOSAL],
            str([s.kind for s in stats]),
        )
    finally:
        for rid in ids:
            await db.pool.execute("DELETE FROM review_queue WHERE id = $1", rid)


async def check_auto_recorded(db: Database) -> None:
    """M6 task 4 (ADR-048 §11/§12): the real ``PgAutoRecordedStore`` (audit JOIN + tombstone) +
    reprocess replay-exclusion — the un-fakeable ``chat_auto_recorded`` ⋈ ``captures`` ⋈ ``nodes``
    LATERAL title pick, the ``removed_at`` tombstone, and ``capture_ids_chronological`` excluding it
    (migration 014)."""
    print("\n== PgAutoRecordedStore audit list + remove tombstone (M6 task 4 — migration 014) ==")
    captures = PgCaptureStore(db)
    auto = PgAutoRecordedStore(db, snippet_max=200)
    reprocess = PgReprocessStore(db)
    # An auto-endorsed chat capture pointing at the seeded MEM1 (content) + ALEX (a person hub —
    # skipped by the title pick), plus a live control capture that must stay replayable.
    rec_id = "cccccccc-0000-0000-0000-0000000000a1"
    live_id = "cccccccc-0000-0000-0000-0000000000a2"
    try:
        await captures.create(
            capture_id=rec_id,
            kind=KIND_TEXT,
            status=INDEXED,
            raw_text="The user met Alex at the office.",
            source="chat",
            source_ref="smoke-sess",
        )
        await captures.set_node_paths(rec_id, ["smoke::memory/m1.md", "smoke::person/alex.md"])
        await captures.create(
            capture_id=live_id,
            kind=KIND_TEXT,
            status=INDEXED,
            raw_text="A second chat memory.",
            source="chat",
            source_ref="smoke-sess",
        )

        await auto.record(rec_id, salience="high")
        await auto.record(rec_id, salience="low")  # idempotent — ON CONFLICT DO NOTHING
        check("record is idempotent + is_recorded true", await auto.is_recorded(rec_id))
        check("a non-recorded capture is not is_recorded", not await auto.is_recorded(live_id))

        items = await auto.list_recent(200, entity_types=["person"])
        mine = next((i for i in items if i.capture_id == rec_id), None)
        check(
            "audit list joins the primary CONTENT node title (hub skipped)",
            mine is not None and mine.title == "Met Alex at the office",
            str(mine),
        )
        check(
            "audit item carries node_paths + salience + source_ref + snippet",
            mine is not None
            and mine.node_paths == ["smoke::memory/m1.md", "smoke::person/alex.md"]
            and mine.salience == "high"
            and mine.source_ref == "smoke-sess"
            and mine.snippet == "The user met Alex at the office.",
            str(mine),
        )

        # The live control capture has no chat_auto_recorded row → never in the audit list.
        check(
            "a non-auto-recorded chat capture is absent from the audit list",
            all(i.capture_id != live_id for i in items),
            str([i.capture_id for i in items]),
        )

        tombstoned = await auto.tombstone(rec_id)
        check("tombstone stamps removed_at (live row)", tombstoned is True)
        again = await auto.tombstone(rec_id)
        check("second tombstone is a no-op (already removed)", again is False)

        after = await auto.list_recent(200, entity_types=["person"])
        check(
            "tombstoned item drops out of the audit list",
            all(i.capture_id != rec_id for i in after),
            str([i.capture_id for i in after]),
        )

        # Reprocess replay-exclusion (the P10-preserving guarantee): the removed capture is skipped,
        # the live one still replays. capture_ids_chronological is a GLOBAL read → assert subset.
        ids = set(await reprocess.capture_ids_chronological())
        check(
            "reprocess excludes the tombstoned capture but keeps the live one",
            rec_id not in ids and live_id in ids,
            f"removed_in={rec_id in ids} live_in={live_id in ids}",
        )
    finally:
        # chat_auto_recorded cascades on the capture delete (FK ON DELETE CASCADE).
        await db.pool.execute("DELETE FROM captures WHERE id = ANY($1::uuid[])", [rec_id, live_id])


async def check_dedup_sweep(db: Database) -> None:
    """M6 task 5 (ADR-049 §3–§6): the real ``PgDedupStore`` candidate SQL + re-file guard + survivor
    degree — the un-fakeable strict-AND gate (HNSW top-K cosine ⋀ shared entity hub ⋀ occurred-
    overlap), the ``array_agg`` shared-entity LATERAL, the ``payload->>`` guard match, and the
    canonical-degree subquery. Seeds its own ``dddddddd::`` nodes/edges, cleaned up in a finally."""
    print("\n== PgDedupStore candidate SQL + re-file guard + survivor degree (M6 task 5) ==")
    store = PgDedupStore(db)
    now = datetime.now(UTC)
    # Two entity hubs + five content memories. A~B qualify (high cosine, share hub P, dates
    # overlap); C shares P + high cosine but a DISJOINT occurred window; D has high cosine + overlap
    # but links a DIFFERENT hub (no shared entity); F shares P + overlap but a LOW-cosine vector.
    p = "dddddddd-0000-0000-0000-0000000000a0"
    p2 = "dddddddd-0000-0000-0000-0000000000a9"
    a = "dddddddd-0000-0000-0000-0000000000a1"
    b = "dddddddd-0000-0000-0000-0000000000a2"
    c = "dddddddd-0000-0000-0000-0000000000a3"
    d = "dddddddd-0000-0000-0000-0000000000a4"
    f = "dddddddd-0000-0000-0000-0000000000a5"
    g = "dddddddd-0000-0000-0000-0000000000a6"  # UNDATED (occurred_start NULL) — never excludes
    ids = [p, p2, a, b, c, d, f, g]
    low_cos = [0.0] * DIM  # orthogonal-ish to vec(0.10) → cosine ≈ 0.20, below the 0.90 floor
    low_cos[0], low_cos[1] = 0.10, 1.0
    a_created = datetime(2026, 1, 1, tzinfo=UTC)
    b_created = datetime(2026, 6, 1, tzinfo=UTC)
    try:
        async with db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO nodes (id, store_path, type, title, planes, tags, aliases, merged_into,
                    occurred_start, occurred_end, content_hash, embedding, node_created_at,
                    indexed_at)
                VALUES
                 ($1,'dddddddd::person/p.md','person','Ana','{}','{}','{}',NULL,
                  NULL,NULL,'h_p',$8,$14,$14),
                 ($2,'dddddddd::person/p2.md','person','Bob','{}','{}','{}',NULL,
                  NULL,NULL,'h_p2',$8,$14,$14),
                 ($3,'dddddddd::memory/a.md','memory','coffee with Ana','{}','{}','{}',NULL,
                  '2026-02-01','2026-02-28','h_a',$9,$12,$14),
                 ($4,'dddddddd::memory/b.md','memory','coffee w/ Ana','{}','{}','{}',NULL,
                  '2026-02-15',NULL,'h_b',$10,$13,$14),
                 ($5,'dddddddd::memory/c.md','memory','met Ana last year','{}','{}','{}',NULL,
                  '2025-01-01','2025-01-05','h_c',$9,$14,$14),
                 ($6,'dddddddd::memory/d.md','memory','lunch with Bob','{}','{}','{}',NULL,
                  '2026-02-10',NULL,'h_d',$9,$14,$14),
                 ($7,'dddddddd::memory/f.md','memory','unrelated Ana note','{}','{}','{}',NULL,
                  '2026-02-12',NULL,'h_f',$11,$14,$14),
                 ($15,'dddddddd::memory/g.md','memory','coffee, Ana (undated)','{}','{}','{}',NULL,
                  NULL,NULL,'h_g',$9,$14,$14)
                """,
                p,
                p2,
                a,
                b,
                c,
                d,
                f,
                vec(0.10),
                vec(0.10),
                vec(0.11),
                low_cos,
                a_created,
                b_created,
                now,
                g,
            )
            await conn.execute(
                """
                INSERT INTO edges (src_id, dst_id, rel, origin, score) VALUES
                 ($1,$2,'involves','canonical',NULL),
                 ($3,$2,'involves','canonical',NULL),
                 ($4,$2,'involves','canonical',NULL),
                 ($5,$6,'involves','canonical',NULL),
                 ($7,$2,'involves','canonical',NULL),
                 ($8,$2,'involves','canonical',NULL)
                """,
                a,
                p,
                b,
                c,
                d,
                p2,
                f,
                g,
            )

        watermark = now - timedelta(days=365)
        pairs = await store.candidate_pairs(
            content_types=["memory"],
            entity_like_types=["person"],
            watermark=watermark,
            min_cosine=0.90,
            candidate_k=10,
        )
        canon = {tuple(sorted((x.node_a, x.node_b))) for x in pairs}
        check(
            "A~B near-dup pair surfaces (cosine ⋀ shared hub ⋀ overlap)",
            tuple(sorted((a, b))) in canon,
            str(canon),
        )
        # C is dated 2025 (disjoint from A/B's 2026-02 windows) → excluded from its DATED partners.
        check(
            "disjoint dated occurred windows exclude the pair (A/B vs C)",
            tuple(sorted((a, c))) not in canon and tuple(sorted((b, c))) not in canon,
            str(canon),
        )
        check(
            "no shared entity hub excludes the pair (D → different hub)",
            not any(d in pair for pair in canon),
            str(canon),
        )
        check(
            "below-floor cosine excludes the pair (F orthogonal)",
            not any(f in pair for pair in canon),
            str(canon),
        )
        # G is UNDATED: a null occurred_start on either side never excludes (ADR-049 §3) — so A~G
        # still reaches review, but occurred_overlap is False (no positive date signal).
        check(
            "undated node never excluded: A~G surfaces despite G's null occurred_start",
            tuple(sorted((a, g))) in canon,
            str(canon),
        )
        ag = next(x for x in pairs if {x.node_a, x.node_b} == {a, g})
        check(
            "A~G signals: occurred_overlap False (G undated, not a date signal)",
            ag.occurred_overlap is False and p in ag.shared_entity_ids,
            str(ag),
        )
        ab = next(x for x in pairs if {x.node_a, x.node_b} == {a, b})
        check(
            "A~B signals: cosine ≥ 0.90, shared hub P, occurred_overlap True",
            ab.cosine >= 0.90
            and p in ab.shared_entity_ids
            and "Ana" in ab.shared_entity_titles
            and ab.occurred_overlap is True,
            str(ab),
        )

        # survivor_stats: canonical degree (in+out) + created times for the default-survivor pick.
        stats = await store.survivor_stats([a, b])
        check(
            "survivor_stats degree = 1 each (one canonical edge to the hub)",
            stats[a].degree == 1 and stats[b].degree == 1,
            str(stats),
        )
        check(
            "survivor_stats carries node_created_at (A older than B)",
            stats[a].node_created_at == a_created and stats[b].node_created_at == b_created,
            str(stats),
        )

        # Re-file guard: a filed dedup-proposal for the canonical pair blocks a re-propose.
        na, nb = sorted((a, b))
        await db.pool.execute(
            """
            INSERT INTO review_queue (kind, payload, source)
            VALUES ('dedup-proposal', $1::jsonb, 'dedup-sweep')
            """,
            json.dumps({"node_a": na, "node_b": nb}),
        )
        check(
            "proposal_exists true for the filed canonical pair (re-file guard)",
            await store.proposal_exists(na, nb),
        )
        check(
            "proposal_exists false for an unrelated pair",
            not await store.proposal_exists(na, "dddddddd-0000-0000-0000-0000000000ee"),
        )
    finally:
        await db.pool.execute(
            "DELETE FROM review_queue WHERE kind = 'dedup-proposal'"
            " AND payload->>'node_a' = ANY($1::text[])",
            sorted((a, b))[:1],
        )
        await db.pool.execute("DELETE FROM edges WHERE src_id = ANY($1::uuid[])", ids)
        await db.pool.execute("DELETE FROM nodes WHERE id = ANY($1::uuid[])", ids)


async def check_inbox_drain(db: Database) -> None:
    """M6 task 6 (ADR-048 §10): the real ``PgCaptureStore.list_inbox_materialized`` SQL — the
    un-fakeable ``EXISTS (unnest(node_paths) … LIKE folder/%)`` inbox predicate + the ``removed_at``
    tombstone filter + the oldest-first ``LIMIT`` bound. The read is GLOBAL, so we assert on our
    own seeded ids (subset) — pre-existing dev inbox captures never break it. Cleaned up finally."""
    print("\n== PgCaptureStore.list_inbox_materialized (M6 task 6 — inbox drainer) ==")
    captures = PgCaptureStore(db)
    now = datetime.now(UTC)
    old = "eeeeeeee-0000-0000-0000-0000000000a1"  # inbox, oldest of ours
    new = "eeeeeeee-0000-0000-0000-0000000000a2"  # inbox, newer
    organized = "eeeeeeee-0000-0000-0000-0000000000a3"  # real nodes only — excluded
    removed = "eeeeeeee-0000-0000-0000-0000000000a4"  # inbox but tombstoned — excluded
    ids = [old, new, organized, removed]
    try:
        await captures.create(
            capture_id=old, kind=KIND_TEXT, status=INDEXED, created_at=now - timedelta(hours=2)
        )
        await captures.set_node_paths(old, ["inbox/old--aa.md"])
        await captures.create(
            capture_id=new, kind=KIND_TEXT, status=INDEXED, created_at=now - timedelta(hours=1)
        )
        await captures.set_node_paths(new, ["inbox/new--bb.md"])
        await captures.create(capture_id=organized, kind=KIND_TEXT, status=INDEXED, created_at=now)
        await captures.set_node_paths(organized, ["memory/real--cc.md", "person/x--dd.md"])
        await captures.create(
            capture_id=removed, kind=KIND_TEXT, status=INDEXED, created_at=now - timedelta(hours=3)
        )
        await captures.set_node_paths(removed, ["inbox/removed--ee.md"])
        await db.pool.execute("UPDATE captures SET removed_at = now() WHERE id = $1", removed)

        got = await captures.list_inbox_materialized(folder="inbox", limit=200)
        mine = [r.id for r in got if r.id in ids]
        check(
            "inbox-materialized captures selected oldest-first; organized + removed excluded",
            mine == [old, new],
            str(mine),
        )
        # The LIMIT bounds a run (global read → assert the count, not which id).
        capped = await captures.list_inbox_materialized(folder="inbox", limit=1)
        check("the LIMIT bounds one run's selection", len(capped) == 1, str(len(capped)))
        # A different configured folder matches its own prefix, not `inbox/` (config-driven).
        other = await captures.list_inbox_materialized(folder="dropbox", limit=200)
        check("a non-matching folder selects none of ours", all(r.id not in ids for r in other))
    finally:
        await db.pool.execute("DELETE FROM captures WHERE id = ANY($1::uuid[])", ids)


async def check_neighbor_zones(db: Database) -> None:
    """M7 task 1 (ADR-051 §2): the real ``PgNeighborStore.neighbor_zones`` window SQL + the light
    ``center_header`` read — the un-fakeable per-``(origin, rel)`` ROW_NUMBER cap, the ``COUNT(*)
    OVER`` zone total, direction scoping (filtered *before* the window so the count follows), and
    tombstone exclusion. Seeds its own ``ffffffff::`` nodes/edges, cleaned up in a finally."""
    print("\n== PgNeighborStore.neighbor_zones + center_header (M7 task 1 — grouped map SQL) ==")
    store = PgNeighborStore(db)
    now = datetime.now(UTC)
    c = "ffffffff-0000-0000-0000-0000000000c0"  # center hub (person)
    place = "ffffffff-0000-0000-0000-0000000000c1"  # place — out/at zone (current)
    sim = "ffffffff-0000-0000-0000-0000000000c2"  # derived-similar target
    past = "ffffffff-0000-0000-0000-0000000000c3"  # place — a superseded (until-closed) at edge
    tomb = "ffffffff-0000-0000-0000-0000000000cf"  # tombstoned — endpoint excluded both ends
    mems = [f"ffffffff-0000-0000-0000-0000000000d{i}" for i in range(5)]  # involves -> C (inbound)
    ids = [c, place, sim, past, tomb, *mems]
    try:
        async with db.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO nodes (id, store_path, type, title, plane, planes, tags, aliases,
                    merged_into, content_hash, node_created_at, indexed_at)
                VALUES
                 ($1,'ffffffff::person/c.md','person','Hub','personal','{personal}','{}','{}',
                  NULL,'h_c',$5,$5),
                 ($2,'ffffffff::place/pl.md','place','Place','personal','{personal}','{}','{}',
                  NULL,'h_pl',$5,$5),
                 ($3,'ffffffff::memory/sim.md','memory','Similar','personal','{personal}','{}','{}',
                  NULL,'h_sim',$5,$5),
                 ($4,'ffffffff::person/tomb.md','person','Tomb','personal','{personal}','{}','{}',
                  $1,'h_tomb',$5,$5),
                 ($6,'ffffffff::place/past.md','place','Past','personal','{personal}','{}','{}',
                  NULL,'h_past',$5,$5)
                """,
                c,
                place,
                sim,
                tomb,
                now,
                past,
            )
            await conn.executemany(
                """
                INSERT INTO nodes (id, store_path, type, title, plane, planes, tags, aliases,
                    merged_into, content_hash, node_created_at, indexed_at)
                VALUES ($1,$2,'memory',$3,'personal','{personal}','{}','{}',NULL,$4,$5,$5)
                """,
                [
                    (m, f"ffffffff::memory/m{i}.md", f"Mem{i}", f"h_m{i}", now)
                    for i, m in enumerate(mems)
                ],
            )
            # m_i -involves-> C (inbound zone), C -at-> place (out zone, current), C -at-> past (a
            # superseded/until-closed edge — must still be RETURNED, ADR-051 §6), C -similar-> sim
            # (derived zone), C -knows-> tomb (canonical, dst tombstoned → excluded from out-leg)
            await conn.executemany(
                "INSERT INTO edges (src_id, dst_id, rel, origin, score) "
                "VALUES ($1,$2,'involves','canonical',NULL)",
                [(m, c) for m in mems],
            )
            await conn.execute(
                """
                INSERT INTO edges (src_id, dst_id, rel, origin, score, since, until) VALUES
                 ($1,$2,'at','canonical',NULL,NULL,NULL),
                 ($1,$5,'at','canonical',NULL,'2020-01-01','2023-01-01'),
                 ($1,$3,'similar','derived',0.9,NULL,NULL),
                 ($1,$4,'knows','canonical',NULL,NULL,NULL)
                """,
                c,
                place,
                sim,
                tomb,
                past,
            )

        rows = await store.neighbor_zones(c, direction=None, fanout=3)
        involves = [z for z in rows if z.edge.rel == "involves"]
        at_zone = [z for z in rows if z.edge.rel == "at"]
        similar = [z for z in rows if z.edge.rel == "similar"]
        check(
            "zones = involves(in) + at(out) + derived similar; tombstoned 'knows' dst excluded",
            {(z.edge.origin, z.edge.rel) for z in rows}
            == {("canonical", "involves"), ("canonical", "at"), ("derived", "similar")},
            str([(z.edge.origin, z.edge.rel, z.edge.node_id) for z in rows]),
        )
        check(
            "involves zone capped to fanout (3 of 5), inbound, node_id-ordered, zone_total=5",
            [z.edge.node_id for z in involves] == mems[:3]
            and all(z.edge.dir == "in" for z in involves)
            and all(z.zone_total == 5 for z in involves),
            str([(z.edge.node_id, z.zone_total) for z in involves]),
        )
        check(
            "at zone = [place, past] out, zone_total 2; carries endpoint type/plane (no 2nd fetch)",
            [z.edge.node_id for z in at_zone] == [place, past]
            and all(z.zone_total == 2 for z in at_zone)
            and at_zone[0].edge.type == "place"
            and at_zone[0].edge.plane == "personal",
            str(at_zone),
        )
        # Superseded (until-closed) edges are RETURNED, dashed+dimmed on the map (ADR-051 §6) — the
        # SQL applies no until filter; the past edge carries its since/until through for the render.
        past_edge = next((z for z in at_zone if z.edge.node_id == past), None)
        check(
            "superseded (until-closed) at edge present, carries since/until (belief timeline)",
            past_edge is not None
            and str(past_edge.edge.until) == "2023-01-01"
            and str(past_edge.edge.since) == "2020-01-01",
            str(past_edge),
        )
        check(
            "derived similar is its own zone (origin=derived), zone_total 1",
            [z.edge.node_id for z in similar] == [sim]
            and similar[0].edge.origin == "derived"
            and similar[0].zone_total == 1,
            str(similar),
        )
        # Direction scoping: 'in' keeps only the involves zone; its total stays 5 (the out edges are
        # filtered before the window, so they neither rank nor count).
        inbound = await store.neighbor_zones(c, direction="in", fanout=3)
        check(
            "direction='in' -> involves zone only, zone_total still 5",
            {(z.edge.origin, z.edge.rel) for z in inbound} == {("canonical", "involves")}
            and all(z.zone_total == 5 for z in inbound),
            str([(z.edge.rel, z.zone_total) for z in inbound]),
        )
        head = await store.center_header(c)
        check(
            "center_header(C) = person/Hub/personal",
            head is not None and head.type == "person" and head.plane == "personal",
            str(head),
        )
        missing = await store.center_header("ffffffff-0000-0000-0000-0000000000ee")
        check("center_header(unknown) -> None", missing is None, str(missing))
    finally:
        await db.pool.execute("DELETE FROM edges WHERE src_id = ANY($1::uuid[])", ids)
        await db.pool.execute("DELETE FROM nodes WHERE id = ANY($1::uuid[])", ids)


async def main() -> int:
    # Section headers carry non-cp1252 glyphs (→, §); force UTF-8 so the run completes on a
    # Windows console instead of dying with UnicodeEncodeError mid-way through the checks.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    settings = Settings()
    db = Database(settings)
    await db.connect()
    # guard: never run against a non-local DB
    if "localhost" not in settings.database_url and "127.0.0.1" not in settings.database_url:
        print(f"REFUSING: database_url is not local: {settings.database_url}")
        return 2
    try:
        await cleanup(db)  # clean slate in case a prior run aborted
        await seed(db)

        alias = PgAliasStore(db)
        ent = PgEntityStore(db)
        prof = PgProfileStore(db)
        search = PgSearchStore(db)
        vocab = PgVocabularyStore(db)
        edges = PgEdgeConsolidationStore(db)

        print("\n== PgAliasStore.find_candidates (entity resolution / exact alias) ==")
        by_title = await alias.find_candidates("Alex", types=["person"])
        check("exact title 'Alex' -> ALEX", [c.id for c in by_title] == [ALEX], str(by_title))
        by_alias = await alias.find_candidates("alexandru", types=["person"])
        check(
            "normalized alias 'alexandru' -> ALEX (accretion recall)",
            [c.id for c in by_alias] == [ALEX],
            str(by_alias),
        )
        ghost_hit = await alias.find_candidates("Ghost", types=["person"])
        check("tombstoned 'Ghost' excluded", ghost_hit == [], str(ghost_hit))
        wrong_type = await alias.find_candidates("Alex", types=["idea"])
        check("type filter excludes person", wrong_type == [], str(wrong_type))
        # ADR-040 (M3 task 11): the token-overlap leg — a variant surface form must surface the hub
        # by a shared significant token, exercising the regexp_split_to_array + && SQL on real pg.
        overlap = await alias.find_candidates(
            "Alex Marsh", types=["person"], tokens=["alex", "marsh"], limit=8
        )
        check(
            "token-overlap 'Alex Marsh' -> ALEX (shared token)",
            ALEX in [c.id for c in overlap],
            str(overlap),
        )
        no_tokens = await alias.find_candidates("Nomatch", types=["person"], tokens=[], limit=8)
        check("empty tokens -> exact-only (no fan-out)", no_tokens == [], str(no_tokens))
        check(
            "candidate carries store_path (for accretion)",
            all(c.store_path for c in by_title),
            str(by_title),
        )

        print("\n== PgEntityStore (task-6 merge/backfill/profile reads) ==")
        n = await ent.get_node(ALEX)
        check("get_node ALEX aliases", n is not None and set(n.aliases) == {"Alex", "Alexandru"})
        inbound = await ent.inbound_canonical_edges(ALEX)
        check(
            "inbound_canonical_edges excludes tombstone src + derived",
            [e.src_id for e in inbound] == [MEM1],
            str(inbound),
        )
        # list_entities / entities_touched_since are GLOBAL reads (not smoke-scoped), so assert the
        # invariants as subsets — smoke live entities present, tombstone excluded — rather than
        # exact equality, keeping the check honest against a dev DB that holds real entities
        # (isolation promise in the module docstring).
        listed = {e.id for e in await ent.list_entities(types=["person", "place"])}
        check(
            "list_entities includes live smoke entities, excludes tombstone",
            {ALEX, PLACE} <= listed and GHOST not in listed,
            str(listed),
        )
        touched = {
            e.id
            for e in await ent.entities_touched_since(
                types=["person"], since=datetime.now(UTC) - timedelta(days=1)
            )
        }
        check(
            "entities_touched_since includes recent ALEX, excludes tombstone",
            ALEX in touched and GHOST not in touched,
            str(touched),
        )
        hood = await ent.neighborhood(ALEX)
        rels = sorted((h.dir, h.rel, h.node_id) for h in hood)
        check(
            "neighborhood = in:involves(MEM1) + out:at(PLACE), tombstone/derived excluded",
            rels == sorted([("in", "involves", MEM1), ("out", "at", PLACE)]),
            str(rels),
        )
        check("neighborhood carries since/occurred", any(h.since is not None for h in hood))
        cands = await ent.memory_nodes_matching_alias(
            "Alexandru",
            entity_id=ALEX,
            window_start=datetime.now(UTC) - timedelta(days=365),
            limit=10,
        )
        check(
            "backfill alias match = MEM2 (no edge yet)",
            [m.node_id for m in cands] == [MEM2],
            str(cands),
        )

        print("\n== PgProfileStore (task-6 node_profiles upsert + embed codec) ==")
        check("current_hash None before write", await prof.current_hash(ALEX) is None)
        await prof.upsert_profile(
            ALEX,
            tier="snapshot",
            profile="Alex is a person met at the office.",
            observations=[{"cat": "role", "text": "colleague", "as_of": "2026-02"}],
            neighborhood_hash="hash_v1",
            embedding=vec(0.10),
        )
        check("current_hash after upsert", await prof.current_hash(ALEX) == "hash_v1")
        await prof.upsert_profile(  # ON CONFLICT update path (also clears embedding)
            ALEX,
            tier="full",
            profile="Alex, updated.",
            observations=[],
            neighborhood_hash="hash_v2",
            embedding=None,
        )
        check("upsert ON CONFLICT updates hash", await prof.current_hash(ALEX) == "hash_v2")
        # final state: ALEX profile with a NON-null embedding on a known direction so the profile
        # retrieval leg (ADR-037) can be exercised below. ALEX has NO chunk (thin entity hub).
        await prof.upsert_profile(
            ALEX,
            tier="full",
            profile="Alex, updated.",
            observations=[],
            neighborhood_hash="hash_v3",
            embedding=vec(0.10),
        )
        # a tombstoned entity (GHOST, merged into ALEX) with a profile on the SAME direction — the
        # profile leg's `merged_into IS NULL` guard must keep it out of search (ADR-030 §5 / 037).
        await prof.upsert_profile(
            GHOST,
            tier="stub",
            profile="Ghost profile.",
            observations=[],
            neighborhood_hash="hash_g",
            embedding=vec(0.10),
        )

        print("\n== PgSearchStore (get_node profile join + hybrid RRF search + temporal, M4 t2) ==")
        node_row = await search.get_node(ALEX)
        check(
            "get_node LEFT JOIN returns profile text",
            node_row is not None and node_row.profile == "Alex, updated.",
            str(node_row),
        )
        # chunk (vector) leg intact: query at MEM2's chunk direction, no FTS text → vector-only.
        chunk_hits = await search.search_chunks(vec(0.21), "", rp(settings))
        check(
            "vector leg intact: query -> MEM2 via chunk embedding",
            MEM2 in [h.node_id for h in chunk_hits],
            str([h.node_id for h in chunk_hits]),
        )
        # ADR-037 profile leg: ALEX has a profile embedding but NO chunk — a query at the profile's
        # direction must surface the ALEX entity node, snippet = the profile text.
        prof_hits = await search.search_chunks(vec(0.10), "", rp(settings))
        alex_hit = next((h for h in prof_hits if h.node_id == ALEX), None)
        check(
            "ADR-037: profile-only entity ALEX reachable via search (profile leg)",
            alex_hit is not None,
            str([h.node_id for h in prof_hits]),
        )
        check(
            "ADR-037: profile hit snippet = profile text",
            alex_hit is not None and alex_hit.snippet == "Alex, updated.",
            str(alex_hit),
        )
        check(
            "ADR-037: tombstoned entity's profile excluded from search leg",
            GHOST not in [h.node_id for h in prof_hits],
            str([h.node_id for h in prof_hits]),
        )
        # type filter still applies to the profile leg (ALEX is a person).
        filtered = await search.search_chunks(vec(0.10), "", rp(settings, types=["idea"]))
        check(
            "type filter excludes ALEX profile hit",
            ALEX not in [h.node_id for h in filtered],
            str([h.node_id for h in filtered]),
        )

        # --- M4 task 2: hybrid FTS leg (migration 008 tsv) + RRF fusion ------------------------
        # FTS leg matches by lexeme (not vector): a far-off embedding + a text query hitting MEM2's
        # chunk ("office project") must still surface MEM2 via the tsvector leg, proving the tsv
        # column + websearch_to_tsquery + RRF fuse. vec(9.0) is far from every seeded direction.
        fts_hits = await search.search_chunks(vec(9.0), "office project", rp(settings))
        check(
            "FTS leg: text 'office project' surfaces MEM2 via tsvector (migration 008)",
            MEM2 in [h.node_id for h in fts_hits],
            str([h.node_id for h in fts_hits]),
        )
        check(
            "FTS leg: 'office' matches both office-chunk memories (MEM1+MEM2)",
            {MEM1, MEM2}
            <= set(h.node_id for h in await search.search_chunks(vec(9.0), "office", rp(settings))),
        )
        # Self-suppression: a non-English query yields no corpus-matching lexemes → FTS contributes
        # nothing, ranking falls back to the vector leg (no crash, MEM2 still reachable by vector).
        suppressed = await search.search_chunks(vec(0.21), "bonjour salut ça va", rp(settings))
        check(
            "FTS self-suppresses on non-English query (vector-only fallback, no error)",
            MEM2 in [h.node_id for h in suppressed],
            str([h.node_id for h in suppressed]),
        )

        # --- M4 task 2: temporal filters on occurred (ALEX 2026-01-01, MEM1 02-01, MEM2 03-01) ---
        until_hits = await search.search_chunks(
            vec(0.20), "office", rp(settings, until=date(2026, 1, 15))
        )
        check(
            "temporal until: occurred_start <= 2026-01-15 keeps ALEX, drops MEM1/MEM2",
            MEM1 not in [h.node_id for h in until_hits]
            and MEM2 not in [h.node_id for h in until_hits],
            str([h.node_id for h in until_hits]),
        )
        since_hits = await search.search_chunks(
            vec(0.20), "office", rp(settings, since=date(2026, 2, 15))
        )
        check(
            "temporal since: occurred >= 2026-02-15 keeps MEM2, drops MEM1/ALEX",
            MEM2 in [h.node_id for h in since_hits]
            and MEM1 not in [h.node_id for h in since_hits]
            and ALEX not in [h.node_id for h in since_hits],
            str([h.node_id for h in since_hits]),
        )
        asof_hits = await search.search_chunks(vec(0.10), "", rp(settings, as_of=date(2026, 1, 15)))
        check(
            "temporal as_of: occurred_start <= 2026-01-15 keeps ALEX only (dated nodes)",
            ALEX in [h.node_id for h in asof_hits]
            and MEM1 not in [h.node_id for h in asof_hits]
            and MEM2 not in [h.node_id for h in asof_hits],
            str([h.node_id for h in asof_hits]),
        )

        print("\n== PgNeighborStore (M5 task 1 — one-hop traverse: union/keyset/tombstone SQL) ==")
        # Seeded graph: MEM1 -involves-> ALEX (canonical), ALEX -at-> PLACE (canonical),
        # GHOST -similar-> MEM2 (derived; GHOST is tombstoned → its endpoints are hidden).
        nbr = PgNeighborStore(db)
        both = await nbr.neighbors(ALEX, rel=None, direction=None, after=None, limit=50)
        # Ordered by (origin, rel, dir, node_id): PLACE (canonical/at/out) then MEM1 (involves/in).
        check(
            "neighbors(ALEX) both dirs, ordered, tombstone/derived-endpoint excluded",
            [(e.node_id, e.dir, e.rel) for e in both]
            == [(PLACE, "out", "at"), (MEM1, "in", "involves")],
            str(both),
        )
        check(
            "neighbor carries endpoint type/plane (M7 render, no second fetch)",
            both[0].type == "place" and both[0].plane == "personal",
            str(both[0]),
        )
        only_at = await nbr.neighbors(ALEX, rel="at", direction=None, after=None, limit=50)
        check(
            "rel filter 'at' -> PLACE only", [e.node_id for e in only_at] == [PLACE], str(only_at)
        )
        inbound = await nbr.neighbors(ALEX, rel=None, direction="in", after=None, limit=50)
        check("direction 'in' -> MEM1 only", [e.node_id for e in inbound] == [MEM1], str(inbound))
        outbound = await nbr.neighbors(ALEX, rel=None, direction="out", after=None, limit=50)
        check(
            "direction 'out' -> PLACE only", [e.node_id for e in outbound] == [PLACE], str(outbound)
        )
        # Tombstone exclusion on BOTH ends: the in-leg drops a tombstoned src (MEM2's only edge is
        # GHOST -similar-> MEM2), the out-leg drops a tombstoned dst (MEM1 -knows-> GHOST).
        mem2 = await nbr.neighbors(MEM2, rel=None, direction=None, after=None, limit=50)
        check(
            "neighbors(MEM2) empty — derived edge's tombstoned src excluded (in-leg)",
            mem2 == [],
            str(mem2),
        )
        mem1 = await nbr.neighbors(MEM1, rel=None, direction=None, after=None, limit=50)
        check(
            "neighbors(MEM1) = ALEX only — tombstoned dst GHOST excluded (out-leg)",
            [e.node_id for e in mem1] == [ALEX],
            str(mem1),
        )
        # Keyset pagination drives the real (origin,rel,dir,node_id) > (…::uuid) tuple comparison.
        pg1 = await nbr.neighbors(ALEX, rel=None, direction=None, after=None, limit=1)
        check("page 1 (limit 1) -> PLACE", [e.node_id for e in pg1] == [PLACE], str(pg1))
        after = (pg1[0].origin, pg1[0].rel, pg1[0].dir, pg1[0].node_id)
        pg2 = await nbr.neighbors(ALEX, rel=None, direction=None, after=after, limit=1)
        check(
            "page 2 (keyset after PLACE) -> MEM1, no overlap",
            [e.node_id for e in pg2] == [MEM1],
            str(pg2),
        )

        print("\n== PgVocabularyStore (task-7a app_settings jsonb) ==")
        check("get_additions empty initially", (await vocab.get_additions()).edge_rels == ())
        added = await vocab.add(edge_rels=["smoke_rel"], node_types=["smoke_type"])
        check(
            "add returns merged additions",
            "smoke_rel" in added.edge_rels and "smoke_type" in added.node_types,
            str(added),
        )
        again = await vocab.add(edge_rels=["smoke_rel"])  # idempotent
        check("add idempotent (no dup)", again.edge_rels.count("smoke_rel") == 1, str(again))

        print("\n== PgEdgeConsolidationStore (task-7b inventory + path resolution) ==")
        inv = await edges.edge_inventory(exclude_rel="involves", limit=50)
        inv_rels = {(e.src_id, e.rel, e.dst_id) for e in inv}
        check(
            "edge_inventory excludes exclude_rel + derived + tombstone target",
            (MEM1, "involves", ALEX) not in inv_rels and (ALEX, "at", PLACE) in inv_rels,
            str(inv_rels),
        )
        # excerpt join: query with exclude_rel='at' so the involves edge (src MEM1, which HAS a
        # chunk_index 0) is the candidate — its src_excerpt must be the LEFT JOIN'd chunk content.
        inv2 = await edges.edge_inventory(exclude_rel="at", limit=50)
        involves_edge = next((e for e in inv2 if e.rel == "involves" and e.src_id == MEM1), None)
        check(
            "edge_inventory src_excerpt from chunk_index 0",
            involves_edge is not None
            and involves_edge.src_excerpt is not None
            and "Alex" in involves_edge.src_excerpt,
            str(involves_edge),
        )
        paths = await edges.store_paths_for([ALEX, GHOST, PLACE])
        check(
            "store_paths_for resolves live, drops tombstone",
            paths.get(ALEX) == "smoke::person/alex.md" and GHOST not in paths,
            str(paths),
        )

        print("\n== PgChatStore (M4 task 3 — chat_sessions/chat_messages + sources jsonb) ==")
        chat = PgChatStore(db)
        sid = await chat.create_session()
        check("create_session returns id", bool(sid))
        await chat.add_message(sid, role="user", content="what did I decide about pricing?")
        srcs = [
            {
                "node_id": ALEX,
                "store_path": "smoke::person/alex.md",
                "type": "person",
                "title": "Alex",
                "snippet": "met at the office",
                "score": 0.031,
                "planes": ["Work"],
            },
        ]
        await chat.add_message(
            sid,
            role="assistant",
            content="You raised prices [1].",
            model="claude-opus-4-8",
            sources=srcs,
        )
        msgs = await chat.session_messages(sid)
        check(
            "session_messages returns both turns oldest-first",
            [m.role for m in msgs] == ["user", "assistant"],
            str([m.role for m in msgs]),
        )
        check(
            "assistant sources jsonb round-trips (list[dict], score float)",
            msgs[1].sources == srcs,
            str(msgs[1].sources),
        )
        check(
            "user turn has no model / empty sources",
            msgs[0].model is None and msgs[0].sources == [],
            str(msgs[0]),
        )
        # limit window: newest-N, still oldest-first (the DESC-then-reorder subquery).
        last_one = await chat.session_messages(sid, limit=1)
        check(
            "session_messages limit=1 keeps the newest turn",
            [m.content for m in last_one] == ["You raised prices [1]."],
            str(last_one),
        )
        await chat.set_last_model(sid, "nebius")
        await chat.set_title(sid, "smoke::Pricing decision")
        sess = await chat.get_session(sid)
        check(
            "set_title / set_last_model persisted",
            sess is not None
            and sess.title == "smoke::Pricing decision"
            and sess.last_model == "nebius",
            str(sess),
        )
        listed = await chat.list_sessions(50)
        check("list_sessions includes the session", sid in [s.id for s in listed])
        await db.pool.execute("DELETE FROM chat_sessions WHERE id = $1", sid)

        await check_identity_capsule(db)
        await check_model_routing_migration(db)
        await check_oauth(db)
        await check_agent_runs_linkage(db)
        await check_chat_distill(db)
        await check_review_queue_reopen(db)
        await check_maybe_digest_stats(db)
        await check_auto_recorded(db)
        await check_dedup_sweep(db)
        await check_inbox_drain(db)
        await check_neighbor_zones(db)

        print(f"\n==== {_passes} passed, {_fails} failed ====")
        return 0 if _fails == 0 else 1
    finally:
        await cleanup(db)
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
