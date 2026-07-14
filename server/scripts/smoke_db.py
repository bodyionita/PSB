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
import sys
from datetime import UTC, date, datetime, timedelta

from app.config import Settings
from app.db import Database
from app.entities.entity_store import PgEntityStore
from app.entities.profile_store import PgProfileStore
from app.entities.store import PgAliasStore
from app.search.store import PgSearchStore, RetrievalParams
from app.vocab.edge_store import PgEdgeConsolidationStore
from app.vocab.store import PgVocabularyStore

# --- fixed test ids (uuid) -------------------------------------------------
ALEX = "aaaaaaaa-0000-0000-0000-000000000001"      # person, aliases Alex/Alexandru
GHOST = "aaaaaaaa-0000-0000-0000-0000000000ff"     # person, tombstoned (merged into ALEX)
PLACE = "aaaaaaaa-0000-0000-0000-000000000002"     # place
MEM1 = "aaaaaaaa-0000-0000-0000-000000000010"      # memory (edge -> ALEX)
MEM2 = "aaaaaaaa-0000-0000-0000-000000000011"      # memory (mentions Alex, no edge -> backfill)
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
            ALEX, GHOST, PLACE, MEM1, MEM2, vec(0.10), now, vec(0.20), vec(0.21),
        )
        # edges: MEM1 -involves-> ALEX (canonical), ALEX -at-> PLACE (canonical),
        #        GHOST -involves-> ALEX inbound (canonical, from tombstone src excluded),
        #        MEM1 -similar-> MEM2 (derived, must be ignored by canonical reads)
        await c.execute(
            """
            INSERT INTO edges (src_id, dst_id, rel, origin, score, since, until) VALUES
             ($1,$2,'involves','canonical',NULL,'2026-02-01',NULL),
             ($2,$3,'at','canonical',NULL,'2026-01-05',NULL),
             ($4,$5,'similar','derived',0.87,NULL,NULL)
            """,
            MEM1, ALEX, PLACE, GHOST, MEM2,
        )
        # chunk for MEM2 mentioning the alias (backfill candidate + search hit)
        await c.execute(
            """
            INSERT INTO chunks (node_id, chunk_index, content, embedding)
            VALUES ($1, 0, 'Alexandru called again about the office project', $2)
            """,
            MEM2, vec(0.21),
        )
        # chunk for MEM1 (edge inventory src excerpt)
        await c.execute(
            """
            INSERT INTO chunks (node_id, chunk_index, content, embedding)
            VALUES ($1, 0, 'Met Alex at the office today', $2)
            """,
            MEM1, vec(0.20),
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


async def main() -> int:
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
        check("normalized alias 'alexandru' -> ALEX (accretion recall)",
              [c.id for c in by_alias] == [ALEX], str(by_alias))
        ghost_hit = await alias.find_candidates("Ghost", types=["person"])
        check("tombstoned 'Ghost' excluded", ghost_hit == [], str(ghost_hit))
        wrong_type = await alias.find_candidates("Alex", types=["idea"])
        check("type filter excludes person", wrong_type == [], str(wrong_type))
        # ADR-040 (M3 task 11): the token-overlap leg — a variant surface form must surface the hub
        # by a shared significant token, exercising the regexp_split_to_array + && SQL on real pg.
        overlap = await alias.find_candidates(
            "Alex Marsh", types=["person"], tokens=["alex", "marsh"], limit=8
        )
        check("token-overlap 'Alex Marsh' -> ALEX (shared token)",
              ALEX in [c.id for c in overlap], str(overlap))
        no_tokens = await alias.find_candidates("Nomatch", types=["person"], tokens=[], limit=8)
        check("empty tokens -> exact-only (no fan-out)", no_tokens == [], str(no_tokens))
        check("candidate carries store_path (for accretion)",
              all(c.store_path for c in by_title), str(by_title))

        print("\n== PgEntityStore (task-6 merge/backfill/profile reads) ==")
        n = await ent.get_node(ALEX)
        check("get_node ALEX aliases", n is not None and set(n.aliases) == {"Alex", "Alexandru"})
        inbound = await ent.inbound_canonical_edges(ALEX)
        check("inbound_canonical_edges excludes tombstone src + derived",
              [e.src_id for e in inbound] == [MEM1], str(inbound))
        listed = await ent.list_entities(types=["person", "place"])
        check("list_entities excludes tombstone",
              sorted(e.id for e in listed) == sorted([ALEX, PLACE]), str([e.id for e in listed]))
        touched = await ent.entities_touched_since(
            types=["person"], since=datetime.now(UTC) - timedelta(days=1)
        )
        check("entities_touched_since recent", [e.id for e in touched] == [ALEX], str(touched))
        hood = await ent.neighborhood(ALEX)
        rels = sorted((h.dir, h.rel, h.node_id) for h in hood)
        check("neighborhood = in:involves(MEM1) + out:at(PLACE), tombstone/derived excluded",
              rels == sorted([("in", "involves", MEM1), ("out", "at", PLACE)]), str(rels))
        check("neighborhood carries since/occurred", any(h.since is not None for h in hood))
        cands = await ent.memory_nodes_matching_alias(
            "Alexandru", entity_id=ALEX,
            window_start=datetime.now(UTC) - timedelta(days=365), limit=10,
        )
        check("backfill alias match = MEM2 (no edge yet)",
              [m.node_id for m in cands] == [MEM2], str(cands))

        print("\n== PgProfileStore (task-6 node_profiles upsert + embed codec) ==")
        check("current_hash None before write", await prof.current_hash(ALEX) is None)
        await prof.upsert_profile(
            ALEX, tier="snapshot", profile="Alex is a person met at the office.",
            observations=[{"cat": "role", "text": "colleague", "as_of": "2026-02"}],
            neighborhood_hash="hash_v1", embedding=vec(0.10),
        )
        check("current_hash after upsert", await prof.current_hash(ALEX) == "hash_v1")
        await prof.upsert_profile(  # ON CONFLICT update path (also clears embedding)
            ALEX, tier="full", profile="Alex, updated.", observations=[],
            neighborhood_hash="hash_v2", embedding=None,
        )
        check("upsert ON CONFLICT updates hash", await prof.current_hash(ALEX) == "hash_v2")
        # final state: ALEX profile with a NON-null embedding on a known direction so the profile
        # retrieval leg (ADR-037) can be exercised below. ALEX has NO chunk (thin entity hub).
        await prof.upsert_profile(
            ALEX, tier="full", profile="Alex, updated.", observations=[],
            neighborhood_hash="hash_v3", embedding=vec(0.10),
        )
        # a tombstoned entity (GHOST, merged into ALEX) with a profile on the SAME direction — the
        # profile leg's `merged_into IS NULL` guard must keep it out of search (ADR-030 §5 / 037).
        await prof.upsert_profile(
            GHOST, tier="stub", profile="Ghost profile.", observations=[],
            neighborhood_hash="hash_g", embedding=vec(0.10),
        )

        print("\n== PgSearchStore (get_node profile join + hybrid RRF search + temporal, M4 t2) ==")
        node_row = await search.get_node(ALEX)
        check("get_node LEFT JOIN returns profile text",
              node_row is not None and node_row.profile == "Alex, updated.", str(node_row))
        # chunk (vector) leg intact: query at MEM2's chunk direction, no FTS text → vector-only.
        chunk_hits = await search.search_chunks(vec(0.21), "", rp(settings))
        check("vector leg intact: query -> MEM2 via chunk embedding",
              MEM2 in [h.node_id for h in chunk_hits], str([h.node_id for h in chunk_hits]))
        # ADR-037 profile leg: ALEX has a profile embedding but NO chunk — a query at the profile's
        # direction must surface the ALEX entity node, snippet = the profile text.
        prof_hits = await search.search_chunks(vec(0.10), "", rp(settings))
        alex_hit = next((h for h in prof_hits if h.node_id == ALEX), None)
        check("ADR-037: profile-only entity ALEX reachable via search (profile leg)",
              alex_hit is not None, str([h.node_id for h in prof_hits]))
        check("ADR-037: profile hit snippet = profile text",
              alex_hit is not None and alex_hit.snippet == "Alex, updated.",
              str(alex_hit))
        check("ADR-037: tombstoned entity's profile excluded from search leg",
              GHOST not in [h.node_id for h in prof_hits], str([h.node_id for h in prof_hits]))
        # type filter still applies to the profile leg (ALEX is a person).
        filtered = await search.search_chunks(vec(0.10), "", rp(settings, types=["idea"]))
        check("type filter excludes ALEX profile hit",
              ALEX not in [h.node_id for h in filtered], str([h.node_id for h in filtered]))

        # --- M4 task 2: hybrid FTS leg (migration 008 tsv) + RRF fusion ------------------------
        # FTS leg matches by lexeme (not vector): a far-off embedding + a text query hitting MEM2's
        # chunk ("office project") must still surface MEM2 via the tsvector leg, proving the tsv
        # column + websearch_to_tsquery + RRF fuse. vec(9.0) is far from every seeded direction.
        fts_hits = await search.search_chunks(vec(9.0), "office project", rp(settings))
        check("FTS leg: text 'office project' surfaces MEM2 via tsvector (migration 008)",
              MEM2 in [h.node_id for h in fts_hits], str([h.node_id for h in fts_hits]))
        check("FTS leg: 'office' matches both office-chunk memories (MEM1+MEM2)",
              {MEM1, MEM2} <= set(h.node_id for h in
                                  await search.search_chunks(vec(9.0), "office", rp(settings))))
        # Self-suppression: a non-English query yields no corpus-matching lexemes → FTS contributes
        # nothing, ranking falls back to the vector leg (no crash, MEM2 still reachable by vector).
        suppressed = await search.search_chunks(vec(0.21), "bonjour salut ça va", rp(settings))
        check("FTS self-suppresses on non-English query (vector-only fallback, no error)",
              MEM2 in [h.node_id for h in suppressed], str([h.node_id for h in suppressed]))

        # --- M4 task 2: temporal filters on occurred (ALEX 2026-01-01, MEM1 02-01, MEM2 03-01) ---
        until_hits = await search.search_chunks(
            vec(0.20), "office", rp(settings, until=date(2026, 1, 15))
        )
        check("temporal until: occurred_start <= 2026-01-15 keeps ALEX, drops MEM1/MEM2",
              MEM1 not in [h.node_id for h in until_hits]
              and MEM2 not in [h.node_id for h in until_hits], str([h.node_id for h in until_hits]))
        since_hits = await search.search_chunks(
            vec(0.20), "office", rp(settings, since=date(2026, 2, 15))
        )
        check("temporal since: occurred >= 2026-02-15 keeps MEM2, drops MEM1/ALEX",
              MEM2 in [h.node_id for h in since_hits]
              and MEM1 not in [h.node_id for h in since_hits]
              and ALEX not in [h.node_id for h in since_hits], str([h.node_id for h in since_hits]))
        asof_hits = await search.search_chunks(
            vec(0.10), "", rp(settings, as_of=date(2026, 1, 15))
        )
        check("temporal as_of: occurred_start <= 2026-01-15 keeps ALEX only (dated nodes)",
              ALEX in [h.node_id for h in asof_hits]
              and MEM1 not in [h.node_id for h in asof_hits]
              and MEM2 not in [h.node_id for h in asof_hits], str([h.node_id for h in asof_hits]))

        print("\n== PgVocabularyStore (task-7a app_settings jsonb) ==")
        check("get_additions empty initially",
              (await vocab.get_additions()).edge_rels == ())
        added = await vocab.add(edge_rels=["smoke_rel"], node_types=["smoke_type"])
        check("add returns merged additions",
              "smoke_rel" in added.edge_rels and "smoke_type" in added.node_types, str(added))
        again = await vocab.add(edge_rels=["smoke_rel"])  # idempotent
        check("add idempotent (no dup)", again.edge_rels.count("smoke_rel") == 1, str(again))

        print("\n== PgEdgeConsolidationStore (task-7b inventory + path resolution) ==")
        inv = await edges.edge_inventory(exclude_rel="involves", limit=50)
        inv_rels = {(e.src_id, e.rel, e.dst_id) for e in inv}
        check("edge_inventory excludes exclude_rel + derived + tombstone target",
              (MEM1, "involves", ALEX) not in inv_rels and (ALEX, "at", PLACE) in inv_rels,
              str(inv_rels))
        # excerpt join: query with exclude_rel='at' so the involves edge (src MEM1, which HAS a
        # chunk_index 0) is the candidate — its src_excerpt must be the LEFT JOIN'd chunk content.
        inv2 = await edges.edge_inventory(exclude_rel="at", limit=50)
        involves_edge = next((e for e in inv2 if e.rel == "involves" and e.src_id == MEM1), None)
        check("edge_inventory src_excerpt from chunk_index 0",
              involves_edge is not None and involves_edge.src_excerpt is not None
              and "Alex" in involves_edge.src_excerpt, str(involves_edge))
        paths = await edges.store_paths_for([ALEX, GHOST, PLACE])
        check("store_paths_for resolves live, drops tombstone",
              paths.get(ALEX) == "smoke::person/alex.md" and GHOST not in paths, str(paths))

        print(f"\n==== {_passes} passed, {_fails} failed ====")
        return 0 if _fails == 0 else 1
    finally:
        await cleanup(db)
        await db.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
