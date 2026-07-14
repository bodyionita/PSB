"""Admin router (03-api.md §Agents & admin). Session-gated operational actions.

`POST /admin/backup` forces an immediate graph-store commit + push (ADR-014) — the manual
counterpart to the debounced write-batch commits and the nightly sweep.

`POST /admin/reindex` triggers the combined store-reconciliation pass (rescan + derived-edge
recompute) asynchronously — `202 {run_id}`, single-flight (`409` if one is already running).

`POST /admin/captures/{id}/reorganize` re-runs organize on a capture's stored raw text and
replaces its nodes — a maintenance re-run (e.g. re-deriving nodes after an organizer prompt change).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..dependencies import (
    get_capture_pipeline,
    get_edge_consolidation_service,
    get_merge_service,
    get_registry,
    get_reindex_service,
    get_reprocess_service,
    get_store_backup,
    get_tag_consolidation_service,
    require_session,
)
from ..entities.merge import BadMerge, MergeNodeNotFound, MergeService
from ..models import (
    BackupResponse,
    CaptureAcceptedResponse,
    EdgeRetypeItem,
    EntityMergeAcceptedResponse,
    EntityMergeProposeResponse,
    EntityMergeRequest,
    InboundEdgeModel,
    MergeSideModel,
    ProviderErrorModel,
    ProvidersResponse,
    ProviderStatusItem,
    ReindexAcceptedResponse,
    ReprocessAcceptedResponse,
    ReprocessPreviewResponse,
    ReprocessRequest,
    TagConsolidateAcceptedResponse,
    TagConsolidateProposeResponse,
    TagConsolidateRequest,
    TagMergeItem,
    VocabConsolidateAcceptedResponse,
    VocabConsolidateProposeResponse,
    VocabConsolidateRequest,
)
from ..providers.base import ProviderUnavailable
from ..providers.registry import ProviderRegistry
from ..services.capture_pipeline import CaptureNotFound, CapturePipeline
from ..services.reindex import ReindexService
from ..services.reprocess import ReprocessService
from ..services.store_backup import StoreBackupService
from ..tags.consolidation import TagMerge
from ..tags.service import TagConsolidationService
from ..vocab.edge_consolidation import BadConsolidation, EdgeConsolidationService, EdgeRetype

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_session)])


@router.post("/backup", response_model=BackupResponse)
async def backup(
    store_backup: StoreBackupService = Depends(get_store_backup),
) -> BackupResponse:
    result = await store_backup.backup_now()
    return BackupResponse(committed=result.committed, pushed=result.pushed)


@router.post(
    "/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReindexAcceptedResponse,
)
async def reindex(
    reindex_service: ReindexService = Depends(get_reindex_service),
) -> ReindexAcceptedResponse:
    """Trigger a full store reconciliation (rescan + derived-edge recompute) in the background.

    Returns `202 {run_id}` and opens an `agent="reindex"` run (`details.trigger="manual"`);
    single-flight — `409` if a reindex or the nightly rescan is already running (ADR-023 §4).
    """
    run_id = await reindex_service.start_manual()
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a reindex is already running"
        )
    return ReindexAcceptedResponse(run_id=run_id)


@router.post("/reprocess", response_model=None)
async def reprocess(
    request: ReprocessRequest,
    response: Response,
    service: ReprocessService = Depends(get_reprocess_service),
) -> ReprocessPreviewResponse | ReprocessAcceptedResponse:
    """Reusable ``reprocess-all-from-raw`` op (ADR-042) — the data-survival mechanism (vision P10).

    Confirm-gated (destructive of derived state). ``confirm=false`` (default) → ``200`` preview
    (how many captures replay, current node count, standing merges not re-appliable), no writes.
    ``confirm=true`` → ``202 {run_id}``: reset the derived index + store node files, replay every
    capture's raw chronologically through the current pipeline, recompute derived edges, and force
    a commit+push — all in the background. Raw + approved vocabulary are preserved. Single-flight —
    ``409`` if a reprocess is already running.
    """
    if request.confirm:
        run_id = await service.apply()
        if run_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="a reprocess is already running"
            )
        response.status_code = status.HTTP_202_ACCEPTED
        return ReprocessAcceptedResponse(run_id=run_id)
    preview = await service.preview()
    return ReprocessPreviewResponse(
        captures=preview.captures, nodes=preview.nodes, merges=preview.merges
    )


@router.post("/tags/consolidate", response_model=None)
async def consolidate_tags(
    request: TagConsolidateRequest,
    response: Response,
    service: TagConsolidationService = Depends(get_tag_consolidation_service),
) -> TagConsolidateProposeResponse | TagConsolidateAcceptedResponse:
    """Two-step tag-vocabulary cleanup (ADR-024 §2).

    Propose (``apply=false``/default) → ``200 {plan_id, merges}``, no writes; a down distill chain
    → 503. Apply (``apply=true`` + reviewed ``plan``) → ``202 {run_id}``, rewriting the affected
    nodes' frontmatter tags + reindexing them in the background (never-lose, git-revertible).
    """
    if request.apply:
        if not request.plan:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="apply requires a non-empty plan",
            )
        plan = [
            TagMerge(canonical=item.canonical, variants=tuple(item.variants))
            for item in request.plan
        ]
        run_id = await service.apply(plan)
        response.status_code = status.HTTP_202_ACCEPTED
        return TagConsolidateAcceptedResponse(run_id=run_id)

    try:
        proposal = await service.propose()
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"tag consolidation unavailable: {exc}",
        ) from exc
    return TagConsolidateProposeResponse(
        plan_id=proposal.plan_id,
        merges=[
            TagMergeItem(canonical=merge.canonical, variants=list(merge.variants))
            for merge in proposal.merges
        ],
    )


@router.post("/vocab/consolidate", response_model=None)
async def consolidate_vocab(
    request: VocabConsolidateRequest,
    response: Response,
    service: EdgeConsolidationService = Depends(get_edge_consolidation_service),
) -> VocabConsolidateProposeResponse | VocabConsolidateAcceptedResponse:
    """Two-step edge retro-consolidation for an approved edge rel (ADR-036 / task 7b).

    Propose (``apply=false``/default) → ``200 {plan_id, rel, retypings}``, no writes; a down distill
    chain → 503. Apply (``apply=true`` + reviewed ``plan``) → ``202 {run_id}``, rewriting the chosen
    edges' ``rel:`` frontmatter + reindexing them in the background (never-lose, git-revertible).
    ``400`` for an unknown/empty rel or an empty apply plan.
    """
    try:
        if request.apply:
            if not request.plan:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="apply requires a non-empty plan",
                )
            plan = [
                EdgeRetype(src_id=i.src_id, to=i.to, from_rel=i.from_rel, to_rel=i.to_rel)
                for i in request.plan
            ]
            run_id = await service.apply(request.rel, plan)
            response.status_code = status.HTTP_202_ACCEPTED
            return VocabConsolidateAcceptedResponse(run_id=run_id)

        proposal = await service.propose(request.rel)
    except BadConsolidation as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"vocab consolidation unavailable: {exc}",
        ) from exc
    return VocabConsolidateProposeResponse(
        plan_id=proposal.plan_id,
        rel=proposal.rel,
        retypings=[
            EdgeRetypeItem(src_id=r.src_id, to=r.to, from_rel=r.from_rel, to_rel=r.to_rel)
            for r in proposal.retypings
        ],
    )


@router.post("/entities/merge", response_model=None)
async def merge_entities(
    request: EntityMergeRequest,
    response: Response,
    service: MergeService = Depends(get_merge_service),
) -> EntityMergeProposeResponse | EntityMergeAcceptedResponse:
    """Two-step entity merge (ADR-030 §5).

    Propose (``apply=false``/default) → ``200`` inbound-edge inventory, no writes. Apply
    (``apply=true``) → ``202 {run_id}``, retargeting inbound edges → unioning aliases → writing the
    tombstone → reindexing + force-commit in the background. ``400`` self-merge / non-entity /
    tombstone endpoint; ``404`` unknown loser or survivor.
    """
    try:
        if request.apply:
            run_id = await service.apply(request.loser, request.survivor)
            response.status_code = status.HTTP_202_ACCEPTED
            return EntityMergeAcceptedResponse(run_id=run_id)
        proposal = await service.propose(request.loser, request.survivor)
    except MergeNodeNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="loser or survivor node not found"
        ) from None
    except BadMerge as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return EntityMergeProposeResponse(
        plan_id=proposal.plan_id,
        loser=MergeSideModel(**vars(proposal.loser)),
        survivor=MergeSideModel(**vars(proposal.survivor)),
        inbound_count=proposal.inbound_count,
        inbound=[InboundEdgeModel(**vars(e)) for e in proposal.inbound],
    )


@router.post(
    "/captures/{capture_id}/reorganize",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CaptureAcceptedResponse,
)
async def reorganize_capture(
    capture_id: str,
    pipeline: CapturePipeline = Depends(get_capture_pipeline),
) -> CaptureAcceptedResponse:
    """Re-organize a capture's stored raw text and replace its nodes (202; runs in background)."""
    try:
        await pipeline.reorganize_capture(capture_id)
    except CaptureNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="capture not found"
        ) from None
    return CaptureAcceptedResponse(capture_id=capture_id)


@router.get("/providers", response_model=ProvidersResponse)
async def providers(
    registry: ProviderRegistry = Depends(get_registry),
) -> ProvidersResponse:
    """Provider observability (ADR-044) — one row per registered provider: identity, configured
    capabilities, a **live** ``health()`` reachability probe (config-reachability, *not* a success
    guarantee), and the in-memory runtime status (sticky ``last_error`` + ``last_success_at`` +
    ``consecutive_failures``). Closes the P8/rule-7 silent-fallback gap the M4 Accept exposed.

    No LLM call, no persistence (in-memory, resets on redeploy). ``/health`` is left untouched — its
    per-provider error text (endpoints, model ids, key-state) must not be public, so this is
    session-gated (the router's ``require_session`` dependency).
    """
    report = await registry.provider_report()
    return ProvidersResponse(
        providers=[
            ProviderStatusItem(
                id=row.id,
                label=row.label,
                capabilities=row.capabilities,
                reachable=row.reachable,
                last_error=(
                    ProviderErrorModel(message=row.last_error.message, at=row.last_error.at)
                    if row.last_error is not None
                    else None
                ),
                last_success_at=row.last_success_at,
                consecutive_failures=row.consecutive_failures,
            )
            for row in report
        ]
    )
