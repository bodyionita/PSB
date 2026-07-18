-- M9 T6 — media-join SQL smoke (the T3 follow-up, ADR-057/ADR-060).
--
-- Verifies the media / node_media joins the app relies on actually work against the REAL prod DB,
-- not just the fakes CI uses. Run AFTER migrations 017+018 apply and the voice-media-backfill op.
-- READ-ONLY — every statement is a SELECT; safe to run on production. Run in the Supabase SQL
-- editor, or:  docker compose exec -T api sh -c 'psql "$DATABASE_URL" -f -' < deploy/smoke/m9_media_join_smoke.sql
--
-- The join shapes mirror the app SQL:
--   * GET /nodes/{id}.media[]   → PgSearchStore.get_node  (node_media nm JOIN media m ON m.id = nm.media_id)
--   * search/chat media_kinds   → array_agg(DISTINCT m.kind) over the same join
--   * CaptureView.media         → PgMediaStore.get_by_capture_id (media WHERE capture_id = …)
-- Eyeball the results; the PASS conditions are in the comment above each block.

\echo '== 1. Tables + indexes exist (migrations 017 + 018 applied) =='
-- PASS: both rows present; node_media has PK (node_id,media_id) + node_media_media_id_idx.
SELECT tablename FROM pg_tables WHERE tablename IN ('media', 'node_media') ORDER BY tablename;
SELECT indexname FROM pg_indexes
 WHERE tablename IN ('media', 'node_media')
 ORDER BY tablename, indexname;

\echo '== 2. media inventory by kind/status =='
-- PASS: kinds ⊆ {image, voice} (no video yet in M9); statuses ⊆ {pending, derived, unavailable}
-- (media has no `deriving` — that's a capture status). `pending` is transient — a lingering
-- count after the pipeline settles means a stuck item.
SELECT kind, status, count(*) AS n
  FROM media
 GROUP BY kind, status
 ORDER BY kind, status;

\echo '== 3. node_media link health =='
-- PASS: total > 0 after any media capture; links_to_missing_media = 0 and links_to_missing_node = 0
-- (both FKs are ON DELETE CASCADE, so a dangling link should be impossible — this proves it).
SELECT
    (SELECT count(*) FROM node_media)                                              AS total_links,
    (SELECT count(*) FROM node_media nm
       LEFT JOIN media m ON m.id = nm.media_id WHERE m.id IS NULL)                  AS links_to_missing_media,
    (SELECT count(*) FROM node_media nm
       LEFT JOIN nodes n ON n.id = nm.node_id WHERE n.id IS NULL)                   AS links_to_missing_node;

\echo '== 4. Links never strand on a tombstone (ADR-060 §4 merge repoint) =='
-- PASS: 0 rows. A node_media row whose node was merged away means a merge failed to repoint the
-- media onto the survivor (the loser is kept, not deleted, so the FK cascade never reaps it).
SELECT nm.node_id, nm.media_id, n.merged_into
  FROM node_media nm JOIN nodes n ON n.id = nm.node_id
 WHERE n.merged_into IS NOT NULL;

\echo '== 5. Every non-connector media row is reachable from a node (content-node link policy) =='
-- PASS (soft): ad-hoc capture media (source='capture') in a terminal status should be node-linked.
-- Rows here = a `derived`/`unavailable` capture media with NO node_media link → the derived-tier
-- rebuild did not run for it (investigate; a still-`pending`/`deriving` item legitimately has none).
SELECT m.id, m.kind, m.status, m.capture_id
  FROM media m
  LEFT JOIN node_media nm ON nm.media_id = m.id
 WHERE m.source = 'capture'
   AND m.status IN ('derived', 'unavailable')
   AND nm.media_id IS NULL
 ORDER BY m.created_at;

\echo '== 6. GET /nodes/{id}.media[] join — sample the newest media-backed nodes =='
-- PASS: rows render (id, kind, status, capture_id) — this IS the get_node media subquery. Empty
-- only if no media-backed node exists yet (capture a photo/voice first).
SELECT n.id AS node_id, n.title, m.kind, m.status, m.capture_id
  FROM node_media nm
  JOIN media m ON m.id = nm.media_id
  JOIN nodes n ON n.id = nm.node_id
 WHERE n.merged_into IS NULL
 ORDER BY m.created_at DESC
 LIMIT 10;

\echo '== 7. search/chat media_kinds array_agg join — nodes and their distinct kinds =='
-- PASS: media-backed nodes show media_kinds like {image} / {voice} / {image,voice}. This is the
-- exact array_agg(DISTINCT m.kind) the search + chat-source glyph reads.
SELECT n.id AS node_id, n.title,
       array_agg(DISTINCT m.kind ORDER BY m.kind) AS media_kinds
  FROM node_media nm
  JOIN media m ON m.id = nm.media_id
  JOIN nodes n ON n.id = nm.node_id
 WHERE n.merged_into IS NULL
 GROUP BY n.id, n.title
 ORDER BY max(m.created_at) DESC
 LIMIT 10;

\echo '== 8. CaptureView.media join — media resolved by capture_id =='
-- PASS: image/voice captures resolve their media row (kind, status). This is get_by_capture_id.
SELECT c.id AS capture_id, c.kind AS capture_kind, c.status AS capture_status,
       m.id AS media_id, m.kind AS media_kind, m.status AS media_status
  FROM captures c
  JOIN media m ON m.capture_id = c.id
 WHERE c.kind IN ('image', 'voice')
 ORDER BY c.created_at DESC
 LIMIT 10;

\echo '== 9. voice-media-backfill verification (ADR-060 §5) =='
-- PASS: after the backfill, every voice capture has a media row (legacy voice was pre-substrate).
-- unbackfilled_voice_captures = 0 (a degraded item — missing audio — still mints an `unavailable`
-- media row, so it counts as backfilled here). derived vs unavailable splits the recoverable ones.
SELECT
    (SELECT count(*) FROM captures WHERE kind = 'voice')                            AS voice_captures,
    (SELECT count(*) FROM captures c WHERE c.kind = 'voice'
       AND NOT EXISTS (SELECT 1 FROM media m WHERE m.capture_id = c.id))            AS unbackfilled_voice_captures,
    (SELECT count(*) FROM media WHERE kind = 'voice' AND status = 'derived')        AS voice_media_derived,
    (SELECT count(*) FROM media WHERE kind = 'voice' AND status = 'unavailable')    AS voice_media_unavailable;
