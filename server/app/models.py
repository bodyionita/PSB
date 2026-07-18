"""Pydantic request/response models for the HTTP API (03-api.md).

These are the wire contract only; they are not DB models (there is no ORM).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .services.agent_runs import RunChild
from .services.capture_store import CaptureMediaRef, CaptureNodeRef, CaptureRecord
from .services.media_store import MediaRecord


# --- Auth ---
class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    authenticated: bool = True


class MeResponse(BaseModel):
    authenticated: bool
    session_created_at: datetime | None = None


# --- Capture (03-api.md §Capture, M1 / ADR-019) ---
class CaptureTextRequest(BaseModel):
    text: str = Field(min_length=1)
    # Optional client-supplied capture time (e.g. an offline note synced later). When absent
    # the server stamps `now()`. Drives the vault-facing `created` frontmatter + filename date.
    created_at: datetime | None = None


class FollowUpRequest(BaseModel):
    answer: str = Field(min_length=1)


class CaptureAnchorEditRequest(BaseModel):
    """Body for ``PUT /captures/{id}/anchor`` — the ADR-056 §5 anchor edit. ``anchor`` is the
    corrected recorded-at (an ISO-8601 datetime); overwriting the stored anchor triggers a
    background one-capture reorganize that re-resolves every relative date against it."""

    anchor: datetime


class CaptureAcceptedResponse(BaseModel):
    """202 body shared by the capture-accepting endpoints (text/voice/retry/follow-up/anchor)."""

    capture_id: str
    status: str = "received"


class DraftTextRequest(BaseModel):
    """Body for ``PUT /capture/{id}/text`` — edit a composite draft's typed text body (M9.6 T1,
    ADR-061 §3). Empty string is allowed (clears the body); the ``>=1 part`` Send gate lives on
    submit, not here."""

    text: str


class DraftPartView(BaseModel):
    """One media part on a composite draft (M9.6 T1, ADR-061 §3). ``kind`` is ``photo``/``voice``;
    ``status`` is the derivation lifecycle (``pending`` until Submit derives it); ``part_ordinal``
    is the stable 0-based position; the web renders the thumbnail/player via ``GET /media/{id}``."""

    id: str
    kind: str
    status: str
    part_ordinal: int | None = None
    mime_type: str | None = None

    @classmethod
    def from_record(cls, media: MediaRecord) -> DraftPartView:
        return cls(
            id=media.id,
            kind=media.kind,
            status=media.status,
            part_ordinal=media.part_ordinal,
            mime_type=media.mime_type,
        )


class DraftView(BaseModel):
    """A composite draft for the compose surface (M9.6 T1, ADR-061 §3): the resume payload —
    ``text_body`` + the ordinal-ordered media parts — so the client rebuilds the compose screen
    after app-close. Distinct from ``CaptureView`` (the committed-capture read; T4 generalizes its
    ``media`` to a list)."""

    capture_id: str
    status: str
    text_body: str | None = None
    parts: list[DraftPartView] = Field(default_factory=list)
    created_at: datetime | None = None

    @classmethod
    def from_record(cls, record: CaptureRecord, parts: list[MediaRecord]) -> DraftView:
        return cls(
            capture_id=record.id,
            status=record.status,
            text_body=record.text_body,
            parts=[DraftPartView.from_record(p) for p in parts],
            created_at=record.created_at,
        )


class CaptureNodeRefModel(BaseModel):
    """One of a capture's resulting nodes, **id-resolved** (M8.1 T4, ADR-054 §5 replan): the
    read-time ``node_paths -> nodes.id`` join, so the web can open a ``NodeChip`` (uuid-keyed
    ``GET /nodes/{id}``) straight from a capture — ``node_paths`` are store *paths*, not identity
    (02-data-model §Identity). ``type``/``title`` ride along as the chip's instant-paint hint."""

    id: str
    store_path: str
    type: str | None = None
    title: str | None = None

    @classmethod
    def from_ref(cls, ref: CaptureNodeRef) -> CaptureNodeRefModel:
        return cls(id=ref.id, store_path=ref.store_path, type=ref.type, title=ref.title)


class CaptureMediaView(BaseModel):
    """The media item backing an image OR voice capture (M9 T3/T4, ADR-057 §6 / ADR-060 §5): the web
    renders the photo / voice player via ``GET /media/{id}`` and shows a derivation-status badge,
    straight off the capture. ``None`` for text/mcp/chat captures. ``kind`` is ``photo``/``voice``;
    ``status`` is the derivation lifecycle (``pending``/``derived``/``unavailable``)."""

    id: str
    kind: str
    status: str

    @classmethod
    def from_ref(cls, ref: CaptureMediaRef) -> CaptureMediaView:
        return cls(id=ref.id, kind=ref.kind, status=ref.status)


class CaptureView(BaseModel):
    """Pipeline state for the capture-screen strip / detail poll (03-api.md)."""

    capture_id: str
    kind: str
    status: str
    raw_text: str | None = None
    node_paths: list[str] = Field(default_factory=list)
    # Id-resolved projection of `node_paths` (M8.1 T4) — see `CaptureNodeRefModel`. A path with no
    # live `nodes` row (not yet indexed, or tombstoned) is simply absent, never null/error; the
    # client falls back to the plain `node_paths` list when a path has no matching ref.
    node_refs: list[CaptureNodeRefModel] = Field(default_factory=list)
    follow_up_question: str | None = None
    follow_up_answer: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # The capture's origin (M8.1, ADR-054 §4): `mcp`/`chat`, or NULL for a web capture (the client
    # falls back to `kind` for the source badge). Carried so the Captures expand
    # (GET /captures/{id}) renders the badge without re-reading the feed row.
    source: str | None = None
    # The backing media item for an image OR voice capture (M9 T3/T4) — the photo/audio + its
    # derivation status; None for text/mcp/chat captures. The web renders `GET /media/{media.id}` +
    # a status badge (a themed audio player for voice — ADR-060 §7).
    media: CaptureMediaView | None = None

    @classmethod
    def from_record(cls, record: CaptureRecord) -> CaptureView:
        return cls(
            capture_id=record.id,
            kind=record.kind,
            status=record.status,
            raw_text=record.raw_text,
            node_paths=list(record.node_paths),
            node_refs=[CaptureNodeRefModel.from_ref(r) for r in record.node_refs],
            follow_up_question=record.follow_up_question,
            follow_up_answer=record.follow_up_answer,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
            source=record.source,
            media=(
                CaptureMediaView.from_ref(record.media_ref)
                if record.media_ref is not None
                else None
            ),
        )


# --- Search & graph (03-api.md §Search & graph, M3 / ADR-022/026/030) ---
class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    # Optional result count; the service clamps it to SEARCH_MAX_TOP_K. None ⇒ SEARCH_TOP_K_DEFAULT.
    top_k: int | None = Field(default=None, ge=1)
    # Filter on `nodes.planes` (array overlap, not folder — ADR-005). None/[] = no filter.
    planes: list[str] | None = None
    # Filter on `nodes.type` (M3). None/[] = no filter.
    types: list[str] | None = None
    # M4 temporal filters (03-api §Search, ADR-032). `since`/`until` = occurred-range window;
    # `as_of` = simple node-date filter (`occurred_start ≤ as_of`). None = no filter.
    since: date | None = None
    until: date | None = None
    as_of: date | None = None


class SearchResultItem(BaseModel):
    """One node-grouped hit (best chunk = snippet), ranked by score (03-api §Search)."""

    node_id: str
    store_path: str
    type: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    snippet: str
    score: float
    # Distinct media kinds the node carries (M9 T4, ADR-060 §7): `photo`/`voice`/`video`, off the
    # `node_media` link. Drives a tiny glyph on the result card — no thumbnails in lists. Empty when
    # the node has no media.
    media_kinds: list[str] = Field(default_factory=list)


class NodeEdgeItem(BaseModel):
    """One edge of a node (03-api §Nodes): the *other* endpoint + edge metadata.

    ``dir`` = ``out`` (this node → other) | ``in``; ``origin`` = ``canonical`` | ``derived``;
    ``score`` = confidence (canonical) or cosine (derived)."""

    rel: str
    dir: str
    node_id: str
    type: str | None = None
    title: str | None = None
    origin: str
    score: float | None = None
    since: date | None = None
    until: date | None = None


class NodeMediaItemModel(BaseModel):
    """One media item a node carries (GET /nodes/{id}.media[], M9 T4 / ADR-060 §1). The web renders
    the photo/voice via ``GET /media/{id}``; ``kind`` (photo/voice/video) picks the tile/player,
    ``status`` (pending/derived/unavailable) the shimmer/broken state, and ``capture_id`` opens the
    "see raw capture" detail sheet (ADR-060 §7)."""

    id: str
    kind: str
    status: str
    capture_id: str | None = None


class NodeDetailResponse(BaseModel):
    """Read-only node detail for the search UI expand + map (GET /nodes/{id}, 03-api §Nodes).

    ``profile`` is the derived entity profile ([ADR-030], null for content nodes and until the
    profile-refresh job lands); ``edges`` are canonical + derived, both directions; ``media`` is the
    node↔media link (M9 T4, ADR-060 §1 — the NodePreview media strip)."""

    node_id: str
    store_path: str
    type: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    disambig: str | None = None
    occurred: date | None = None
    occurred_end: date | None = None
    # Inner-voice dimension (M8.2 T3.5, ADR-055 §3c): `internal`|`external`|`mixed`, or null on an
    # unstamped entity hub. Drives the `NodePreview` marker (internal = full / mixed = subtle).
    interiority: str | None = None
    body: str
    profile: str | None = None
    edges: list[NodeEdgeItem] = Field(default_factory=list)
    media: list[NodeMediaItemModel] = Field(default_factory=list)


class NodeDateTokenEditRequest(BaseModel):
    """Body for ``PUT /nodes/{id}/date-token`` — the ADR-056 §5 mechanical token edit. ``old`` is
    the exact ``[[t:…]]`` token string currently in the body (the edit anchor — no text-span
    bookkeeping); ``start`` (and optional ``end`` for a range) are the new **partial-ISO** date(s)
    (``2025`` / ``2025-07`` / ``2025-07-07``); ``label`` is an optional absolute display label. The
    server rewrites the token, updates ``occurred`` iff it is the event date, then re-embeds."""

    old: str = Field(min_length=1)
    start: str = Field(min_length=1)
    end: str | None = None
    label: str | None = None


class NodeDateTokenEditResponse(BaseModel):
    """Result of a token edit (``PUT /nodes/{id}/date-token``). ``occurred_updated`` is true when
    the edited token was the node's event date; ``occurred``/``occurred_end`` (day-granular
    partial-ISO) are then the new event date, else null (the token changed but ``occurred`` did
    not)."""

    node_id: str
    occurred_updated: bool
    occurred: str | None = None
    occurred_end: str | None = None


# --- Map / neighbors (03-api.md §Search & graph, M7 / ADR-051) ---
class MapNeighborItem(BaseModel):
    """One 1-hop neighbor in a map zone or "show more" page (03-api §Nodes neighbors, ADR-051).

    Carries ``origin``/``dir``/``score``/``since``/``until`` + the endpoint's ``type``/``title``/
    ``plane`` so the canvas renders arrowheads, faint-derived + dashed-superseded (``until``) edges,
    and the node mark (emoji=type, colour=plane) without a second fetch."""

    origin: str
    rel: str
    dir: str
    node_id: str
    type: str | None = None
    title: str | None = None
    plane: str | None = None
    score: float | None = None
    since: date | None = None
    until: date | None = None
    # Inner-voice dimension (M8.2 T3.5, ADR-055 §3c) — the map marks internal/mixed neighbors
    # without a second fetch. Null on an unstamped entity hub.
    interiority: str | None = None


class MapZone(BaseModel):
    """One ``rel`` zone of a center's neighborhood, capped at ``map_zone_fanout`` (ADR-052).

    Keyed by ``rel`` alone — the sole dual-origin rel ``similar`` is one zone; each neighbor's own
    ``origin`` carries the solid/faint styling, so there is no zone-level ``origin``. ``total`` is
    the zone's full size (drives "show N more of M"); ``next_cursor`` is the token for the
    single-zone ``?rel=…&cursor=…`` "show more" page (``None`` when the zone fit)."""

    rel: str
    neighbors: list[MapNeighborItem] = Field(default_factory=list)
    total: int
    next_cursor: str | None = None


class NeighborCenter(BaseModel):
    """The focal node's render header echoed by the grouped neighbors response (03-api §Nodes)."""

    node_id: str
    type: str
    title: str | None = None
    plane: str | None = None
    planes: list[str] = Field(default_factory=list)
    # Inner-voice dimension (M8.2 T3.5, ADR-055 §3c) — the focal node is markable too. Null on an
    # unstamped entity hub.
    interiority: str | None = None


class NeighborZonesResponse(BaseModel):
    """Grouped first page of ``GET /nodes/{id}/neighbors`` (no ``rel`` — ADR-051 §2).

    ``center`` is ``None`` and ``zones`` empty when the node is unknown (empty neighborhood)."""

    center: NeighborCenter | None = None
    zones: list[MapZone] = Field(default_factory=list)


class NeighborPageResponse(BaseModel):
    """A single zone's flat "show more" page — ``GET /nodes/{id}/neighbors?rel=…`` (ADR-051 §2).

    Thin over the M5 rel-filtered keyset; ``next_cursor`` is ``None`` at the zone's end."""

    center_id: str
    rel: str
    direction: str
    neighbors: list[MapNeighborItem] = Field(default_factory=list)
    next_cursor: str | None = None


# --- Chat (03-api.md §Chat, M4 / ADR-025) ---
class ChatRequest(BaseModel):
    """One chat turn (POST /chat). ``session_id`` omitted ⇒ implicit session creation; ``model`` =
    the composer's per-conversation picker override of the Chat group active model (ADR-025 §5)."""

    message: str = Field(min_length=1)
    # A uuid (the DB column type); a malformed id is a 422 here, not a 500 downstream. Absent ⇒
    # implicit session creation. The router hands the service its string form.
    session_id: UUID | None = None
    model: str | None = None
    planes: list[str] | None = None
    top_k: int | None = Field(default=None, ge=1)


class ChatSourceItem(BaseModel):
    """A cited node backing a chat answer (03-api §Chat) — cited-only, renumbered ``[1..m]``."""

    node_id: str
    store_path: str
    type: str
    title: str | None = None
    snippet: str
    score: float
    planes: list[str] = Field(default_factory=list)
    # Distinct media kinds the cited node carries (M9 T4, ADR-060 §7) — the source card's glyph.
    media_kinds: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """A chat answer (POST /chat). ``sources`` is empty for general / "not in your memories"
    answers; ``fallback_used`` flags that a non-primary model answered (ADR-025 transparency)."""

    session_id: str
    answer: str
    model_used: str
    fallback_used: bool
    # Reasoning effort applied to the answering model (None for effort-less models like Nebius);
    # feeds the "answered by <model> · <effort>" caption on a fresh turn (ADR-025 §4). Not persisted
    # (M4 follow-up scope), so history renders the model label without effort.
    effort_used: str | None = None
    sources: list[ChatSourceItem] = Field(default_factory=list)


class ChatModelItem(BaseModel):
    """A pickable chat model (GET /chat/models): stable ``id`` + human-readable ``label`` +
    ``effort`` = the reasoning effort the Chat group applies to it (None for effort-less models like
    Nebius, or one with no configured Chat-group effort), so the picker can show it (ADR-025 §4)."""

    id: str
    label: str
    effort: str | None = None


class ChatModelsResponse(BaseModel):
    """The composer's model picker (GET /chat/models): the registry's chat models + ``default`` =
    the Chat group's active model."""

    models: list[ChatModelItem] = Field(default_factory=list)
    default: str


class ChatSessionItem(BaseModel):
    """A chat session in the thread list (GET /chat/sessions), newest-first."""

    id: str
    title: str | None = None
    created_at: datetime | None = None
    last_model: str | None = None


class ChatMessageItem(BaseModel):
    """One persisted turn in a session (GET /chat/sessions/{id}). ``sources`` carries the cited
    nodes for assistant turns (empty otherwise), each in the ``ChatSourceItem`` shape."""

    role: str
    content: str
    model: str | None = None
    sources: list[ChatSourceItem] = Field(default_factory=list)
    created_at: datetime | None = None


class ChatSessionDetail(BaseModel):
    """A session with its full message history (GET /chat/sessions/{id})."""

    id: str
    title: str | None = None
    messages: list[ChatMessageItem] = Field(default_factory=list)


class RememberResponse(BaseModel):
    """The on-demand distill result (POST /chat/sessions/{id}/remember, ADR-048 §6). Either the pass
    ran — ``endorsed``/``to_review`` counts (each ``0+``; endorsed captures organize in the
    background) with ``skipped=None`` — or it was a no-op (``skipped`` = the reason, counts null).
    Same salience + stance gate as the nightly run; advances the same watermark."""

    endorsed: int | None = None
    to_review: int | None = None
    skipped: str | None = None


class AutoRecordedItem(BaseModel):
    """One auto-endorsed chat memory in the "recently auto-recorded" audit list (GET
    /chat/auto-recorded, ADR-048 §12 / M6 task 4). Feeds the one-tap-remove surface. ``node_paths``
    is empty + ``title`` null until the background organize lands; ``snippet`` previews the endorsed
    statement; ``source_ref`` is the originating chat-session id; ``salience`` is the distiller's
    coarse triage tag."""

    capture_id: str
    node_paths: list[str] = Field(default_factory=list)
    title: str | None = None
    snippet: str
    salience: str | None = None
    source_ref: str | None = None
    created_at: datetime | None = None


# --- Settings: model routing (03-api.md §Settings, ADR-025 / ADR-043) ---
class RoutingModelItem(BaseModel):
    """A pickable chat model for a routing group's dropdowns (GET /settings). ``id`` is the MODEL id
    (the raw vendor string) and ``provider`` is the id of the provider that serves it (derived —
    ADR-045 §1; the routable unit is the model, the provider is an attribute). ``effort_levels`` is
    empty unless ``supports_effort`` — the web renders the effort selector only where it applies,
    from these registry-sourced levels (no hardcoded enums)."""

    id: str
    provider: str
    label: str
    supports_effort: bool = False
    effort_levels: list[str] = Field(default_factory=list)


class GroupRoutingModel(BaseModel):
    """One routing group's editable state (GET /settings): the effective active/fallback + per-
    model effort (saved-over-seed, ADR-045) and the models the dropdowns choose from. ``active``/
    ``fallback`` and every ``effort_by_model`` key are MODEL ids (the raw vendor strings)."""

    group: str
    active: str
    fallback: str
    effort_by_model: dict[str, str] = Field(default_factory=dict)
    models: list[RoutingModelItem] = Field(default_factory=list)


class SettingsResponse(BaseModel):
    """Model routing for all groups (GET /settings, ADR-025 + ADR-043 + ADR-057 §4 — the 4th
    `vision` group). Groups are returned in the service's ``GROUPS`` order."""

    groups: list[GroupRoutingModel] = Field(default_factory=list)


class ModelRoutingUpdate(BaseModel):
    """Save one group's routing (PUT /settings/models). ``group`` is constrained to the 4 known
    groups (422 otherwise); ``active``/``fallback``/``effort_by_model`` keys are model ids (ADR-045)
    — unknown model ids / bad effort levels are a 422 from the service."""

    group: Literal["chat", "conspect", "quick", "vision"]
    active: str = Field(min_length=1)
    fallback: str = ""
    effort_by_model: dict[str, str] = Field(default_factory=dict)


# --- Meta (03-api.md §Meta) ---
class PlanesResponse(BaseModel):
    """The configured plane vocabulary for the Search-tab filter chips (GET /planes, ADR-005).

    ``planes`` = the ``PLANES=`` config list (primary homes); ``inbox`` is the system folder that
    holds organizer-fallback nodes (02 §1), not a plane. The web filters ``POST /search`` on
    ``nodes.planes`` membership using these values, so it duplicates no server config (ADR-006)."""

    planes: list[str] = Field(default_factory=list)
    inbox: str


# --- Review queue (03-api.md §Review, M3 / ADR-030 §3) ---
class ReviewItemResponse(BaseModel):
    """One ``review_queue`` row for the admin Review surface (GET /review, POST /review/{id}).

    ``payload`` carries the kind-specific data decidable in place — an ``entity-ambiguity`` item's
    candidates (``{id,name,disambig,aliases}``) + ``pending_edges``, or a ``vocab-proposal``'s
    proposed ``{vocab,value}``. ``resolution`` is null until the item is resolved."""

    id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    excerpt: str | None = None
    source: str | None = None
    source_ref: str | None = None
    status: str
    resolution: dict[str, Any] | None = None
    created_at: datetime


class ReviewResolveRequest(BaseModel):
    """Resolution body for POST /review/{id}; the meaningful field depends on the item's kind.

    entity-ambiguity → ``choice`` (a candidate node id | ``"new"`` | ``"maybe"``); vocab-proposal →
    ``verdict`` (``"approve"`` | ``"reject"``); stance-candidate → ``verdict``
    (``"agree"``/``"disagree"``/``"maybe"``); dedup-proposal (ADR-049) → ``action``
    (``"merge"``/``"keep"``/``"link"``) with an optional ``survivor`` (a node id, defaulting to the
    payload's ``default_survivor`` for a merge); occurred-enrichment (ADR-056 §7) → ``answer`` (a
    natural-language date like ``"summer 2019"``, or ``"maybe"`` to park / ``"skip"`` to dismiss).
    The server validates per kind (400 otherwise)."""

    choice: str | None = None
    verdict: str | None = None
    action: str | None = None
    survivor: str | None = None
    answer: str | None = None


class ReviewBatchRequest(BaseModel):
    """Batch resolution body for POST /review/batch (ADR-048 §8): apply one ``action`` string to
    many items, best-effort per item. The action is the kind's resolution term — a ``verdict``
    (``agree``/``disagree``/``maybe`` for stance-candidate, ``approve``/``reject`` for vocab) or an
    entity-ambiguity ``choice`` (``maybe`` / a candidate id); each item's resolver reads the field
    that fits its kind, so an action invalid for an item just fails that item (recorded in
    ``results``), never the batch."""

    ids: list[UUID] = Field(min_length=1)
    action: str = Field(min_length=1)


class ReviewBatchResultItem(BaseModel):
    """One item's outcome in a batch resolve (POST /review/batch): ``ok`` with no ``error``, or
    ``ok=false`` + a short reason (unknown / already resolved / invalid for the kind)."""

    id: str
    ok: bool
    error: str | None = None


class ReviewBatchResponse(BaseModel):
    """The per-item results of a batch resolve (POST /review/batch), in request order."""

    results: list[ReviewBatchResultItem] = Field(default_factory=list)


# --- Activity (03-api.md §Activity feed) ---
class RunChildModel(BaseModel):
    """One node of a run's recursive step tree (GET /activity/runs/{id}, M8.1 ADR-054 §2). A lighter
    shape than the run itself — ``name``/``ts`` — plus its own ``children`` (a distiller step's
    spawned ``capture`` runs sit one level deeper). Siblings are ordered early→late."""

    id: str
    name: str
    status: str
    ts: datetime | None = None
    summary: str | None = None
    children: list[RunChildModel] = Field(default_factory=list)

    @classmethod
    def from_run_child(cls, child: RunChild) -> RunChildModel:
        return cls(
            id=child.id,
            name=child.name,
            status=child.status,
            ts=child.ts,
            summary=child.summary,
            children=[cls.from_run_child(c) for c in child.children],
        )


class AgentRunResponse(BaseModel):
    """One ``agent_runs`` row (GET /activity/runs/{id}). M2 pull-forward of the M4 feed so the
    Admin tab can poll a reindex / tags-apply run's live status + ``details`` counts. ``trigger``
    (M8, ADR-053 §5) is the run's origin (``scheduled``/``manual``). ``children`` (M8.1, ADR-054 §2)
    is the recursive step subtree — empty for a leaf run, populated for a pipeline parent."""

    id: str
    agent: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_used: str | None = None
    fallback_used: bool = False
    summary: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    trigger: str = "scheduled"
    children: list[RunChildModel] = Field(default_factory=list)


class RunLogLineModel(BaseModel):
    """One captured live-log line (M8, ADR-053 §1/§2). ``seq`` is the per-run cursor key."""

    seq: int
    ts: datetime | None = None
    level: str
    message: str


class RunLogsResponse(BaseModel):
    """GET /activity/runs/{id}/logs — the live log tail (poll, not stream). Lines with
    ``seq > after_seq`` in order + a ``running`` flag (poll while true, stop when false).
    ``next_after_seq`` is the max ``seq`` returned — pass it back as ``after_seq`` next poll
    (unchanged from the request when there are no new lines)."""

    run_id: str
    running: bool
    logs: list[RunLogLineModel] = Field(default_factory=list)
    next_after_seq: int


# --- Admin (03-api.md §Agents & admin) ---
class BackupResponse(BaseModel):
    """POST /admin/backup result — did this force a new commit, and did the push reach remote."""

    committed: bool
    pushed: bool


class ReindexAcceptedResponse(BaseModel):
    """202 body for POST /admin/reindex — the ``agent_runs`` id of the background reindex run.

    Poll ``agent_runs`` / the activity feed with this id for counts + status (03-api §Admin)."""

    run_id: str


# --- Reprocess-all-from-raw (03-api §Admin, M3 task 11 / ADR-042) ---
class ReprocessRequest(BaseModel):
    """POST /admin/reprocess body. ``confirm=false`` (default) previews what a reprocess would
    touch (no writes); ``confirm=true`` runs the destructive replay in the background."""

    confirm: bool = False


class ReprocessPreviewResponse(BaseModel):
    """Preview (no writes): how many captures would replay + the current derived-node count, plus
    any standing merges the rebuild cannot re-apply by id (reported, never silently dropped)."""

    captures: int
    nodes: int
    merges: int


class ReprocessAcceptedResponse(BaseModel):
    """202 body for the confirm step — the ``agent_runs`` id of the background reprocess run."""

    run_id: str


# --- Identity-capsule refresh (03-api §Admin, M5 task 2 / ADR-046 §5) ---
class IdentityCapsuleAcceptedResponse(BaseModel):
    """202 body for POST /admin/identity-capsule/refresh — the ``agent_runs`` id of the background
    distill run. Poll the activity feed with it for the refreshed/kept outcome."""

    run_id: str


# --- MCP access revoke-all (03-api §MCP, M5 task 3 / ADR-046 §2) ---
class McpRevokeAllResponse(BaseModel):
    """POST /admin/mcp/revoke-all result — how many live MCP tokens this switch just revoked. The
    "revoke all MCP access" control (single-user, instant + total); a connector must reauthorize."""

    revoked: int


# --- Entity merge (03-api §Admin, M3 / ADR-030 §5) ---
class EntityMergeRequest(BaseModel):
    """POST /admin/entities/merge body. ``apply=false`` (default) proposes the inbound-edge
    inventory; ``apply=true`` performs the merge (retarget → alias union → tombstone → reindex)."""

    loser: str = Field(min_length=1)
    survivor: str = Field(min_length=1)
    apply: bool = False


class MergeSideModel(BaseModel):
    """A merge endpoint's loser/survivor summary (identity + alias set)."""

    id: str
    type: str
    title: str | None = None
    aliases: list[str] = Field(default_factory=list)


class InboundEdgeModel(BaseModel):
    """One inbound edge in the propose inventory — a source node that points at the loser."""

    src_id: str
    src_store_path: str
    rel: str


class EntityMergeProposeResponse(BaseModel):
    """Propose result — a correlation id, both sides, and the inbound-edge inventory (no writes)."""

    plan_id: str
    loser: MergeSideModel
    survivor: MergeSideModel
    inbound_count: int
    inbound: list[InboundEdgeModel] = Field(default_factory=list)


class EntityMergeAcceptedResponse(BaseModel):
    """202 body for the apply step — the ``agent_runs`` id of the background merge run."""

    run_id: str


# --- Tag consolidation (03-api §Agents & admin, M2 / ADR-024 §2) ---
class TagMergeItem(BaseModel):
    """One merge group: fold ``variants`` into ``canonical`` (ADR-024). Wire shape for both the
    propose response and the apply request body."""

    canonical: str
    variants: list[str] = Field(default_factory=list)


class TagConsolidateRequest(BaseModel):
    """POST /admin/tags/consolidate body. ``apply=false`` (default) proposes; ``apply=true``
    applies the reviewed ``plan``."""

    apply: bool = False
    plan: list[TagMergeItem] | None = None


class TagConsolidateProposeResponse(BaseModel):
    """Propose result — a correlation id + the merges to review (no writes yet)."""

    plan_id: str
    merges: list[TagMergeItem] = Field(default_factory=list)


class TagConsolidateAcceptedResponse(BaseModel):
    """202 body for the apply step — the ``agent_runs`` id of the background rewrite+reindex run."""

    run_id: str


# --- Vocabulary governance (03-api §Search/Settings, M3 task 7 / ADR-027 / ADR-035) ---
class VocabProposalItem(BaseModel):
    """A pending ``vocab-proposal`` the organizer filed (GET /types). ``vocab`` is the axis
    (``node_type`` | ``entity_type`` | ``edge_rel``); resolve it with the review item ``id``."""

    id: str
    vocab: str | None = None
    value: str | None = None
    excerpt: str | None = None
    created_at: str


class TypesResponse(BaseModel):
    """GET /types — the effective node/edge vocabulary (config seeds ∪ approved additions) plus the
    still-pending type proposals (ADR-027). Entity-like types are the subset carrying the entity
    substrate (aliases/profiles — ADR-030)."""

    node_types: list[str] = Field(default_factory=list)
    edge_rels: list[str] = Field(default_factory=list)
    entity_like_types: list[str] = Field(default_factory=list)
    proposals: list[VocabProposalItem] = Field(default_factory=list)


class VocabularyResolveRequest(BaseModel):
    """PUT /settings/vocabulary — approve or reject a pending type proposal by its review item id.

    Approve writes the type to the live vocabulary + opens the ``vocab-consolidation`` job; reject
    discards. Same choke point as ``POST /review/{id}`` for a ``vocab-proposal`` (ADR-027 §4)."""

    review_id: str = Field(min_length=1)
    verdict: str  # "approve" | "reject"


# --- Edge retro-consolidation (03-api §Admin, M3 task 7b / ADR-036) ---
class EdgeRetypeItem(BaseModel):
    """One edge re-typing: the edge ``{rel: from_rel, to}`` on node ``src_id`` becomes ``to_rel``.
    Wire shape for both the propose response and the apply request body."""

    src_id: str
    to: str
    from_rel: str
    to_rel: str


class VocabConsolidateRequest(BaseModel):
    """POST /admin/vocab/consolidate body. ``apply=false`` (default) proposes edge re-typings for
    the approved ``rel``; ``apply=true`` applies the reviewed ``plan`` (ADR-036)."""

    # No min_length: an empty/whitespace rel is a 400 (``unknown edge rel``) from the service's
    # ``_validated_rel``, matching 03-api §Admin + ADR-036 (not a 422 from schema validation).
    rel: str
    apply: bool = False
    plan: list[EdgeRetypeItem] | None = None


class VocabConsolidateProposeResponse(BaseModel):
    """Propose result — a correlation id, target rel, and the re-typings to review (no writes)."""

    plan_id: str
    rel: str
    retypings: list[EdgeRetypeItem] = Field(default_factory=list)


class VocabConsolidateAcceptedResponse(BaseModel):
    """202 body for the apply step — the ``agent_runs`` id of the background re-type+reindex run."""

    run_id: str


# --- Health ---
class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    db: bool
    store: bool
    git_remote: bool
    backups: bool  # M1 (ADR-014 §6): latest integrity-drill fresh + not failed


# --- Provider observability (03-api.md §Admin, M4 follow-up / ADR-044) ---
class ProviderErrorModel(BaseModel):
    """The last runtime failure for a provider — sticky (a later success does not clear it)."""

    message: str
    at: datetime


class ProviderStatusItem(BaseModel):
    """One provider row for GET /admin/providers (ADR-044). ``reachable`` is a live
    ``Provider.health()`` probe — config-reachability, **not** a success guarantee; the runtime
    truth is ``last_error`` (sticky) + ``last_success_at`` + ``consecutive_failures`` beside it."""

    id: str
    label: str
    capabilities: list[Literal["chat", "stt", "embedding"]] = Field(default_factory=list)
    reachable: bool
    last_error: ProviderErrorModel | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int


class ProvidersResponse(BaseModel):
    """Provider observability (GET /admin/providers): one row per registered provider."""

    providers: list[ProviderStatusItem] = Field(default_factory=list)
