"""M4 follow-up 3 task 2 — migrate saved model routing to model ids (ADR-045 §4).

Revision ID: 009
Revises: 008
Create Date: 2026-07-15

Hand-authored plain SQL (ADR-011): no ORM, no autogenerate. The provider/model/effort split
(ADR-045) makes the routable unit a **model id** (the raw vendor string), not a provider id.
Saved user routing lives in ``app_settings.model_routing`` (one jsonb row, 3 groups each
``{active, fallback, effort_by_*}``) and was written under the *old* vocabulary — provider ids
``claude-max`` / ``claude-max-sonnet`` / ``nebius`` in ``active``/``fallback`` and as the keys of
``effort_by_provider``. Under the new code (task 1) those ids are no longer in the model index and
``effort_by_provider`` is no longer read, so a pre-existing saved row would silently degrade to the
config seed — losing a deliberate routing choice. This is the **P10-load-bearing** step (vision
P10 / "ingested data must survive bug fixes"): migrate the saved config in place so the choice
survives the rename.

Rewrites, in the one ``model_routing`` row:
  * ``claude-max``        → ``claude-opus-4-8``
  * ``claude-max-sonnet`` → ``claude-sonnet-4-6``
  * ``nebius``            → ``meta-llama/Llama-3.3-70B-Instruct``
  * key ``effort_by_provider`` → ``effort_by_model``
in every position they occur (``active``, ``fallback``, and the ``effort_by_*`` object's keys).

Mechanism — an **ordered text substitution** over the jsonb serialisation. The value domain here
is a tiny *closed* set: the only strings a ``model_routing`` value ever holds are model/provider
ids, the effort-object key name, effort levels (``low``/``medium``/``high``) and the group names
(``chat``/``conspect``/``quick``). None of the search tokens is a substring of any legitimate value
we are NOT rewriting, and — after ordering — no replacement's *output* contains a later search
token, so the pass is unambiguous and single-application. Ordering matters only because
``claude-max`` is a prefix of ``claude-max-sonnet``: the ``-sonnet`` form is replaced **first**, so
by the time the bare ``claude-max`` rule runs, only true Opus ids remain. (A structured
``jsonb_object_agg`` key-remap was considered; it would have to rebuild each group object and guard
the empty-row NULL-collapse — more surface for the same closed-set result.)

Idempotent: after one pass none of the search tokens remain, so a re-run over a partially-migrated
DB matches nothing. **No-op when the row is absent or holds no old tokens** — the ``WHERE`` guard
skips any row without ``claude-max`` / ``nebius`` / ``effort_by_provider`` (an unset or already-
migrated ``model_routing``, or an empty ``{}``, is never touched). Legacy-tolerant labels for
historical ``chat_messages.model`` rows (left untouched — rewriting audit would falsify it) live in
``app/providers/labels.py`` (``_LEGACY_MODEL_IDS``), kept in lock-step with the three pairs here.
"""

from __future__ import annotations

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

# Old provider-id vocabulary still present in a saved row → skip everything else (no-op guard).
_UPGRADE_MARKER = "claude-max|nebius|effort_by_provider"
# New model-id vocabulary → the downgrade's no-op guard (best-effort, ADR-011).
_DOWNGRADE_MARKER = r"claude-opus-4-8|claude-sonnet-4-6|meta-llama|effort_by_model"

# `-sonnet` before bare `claude-max` (prefix hazard); the rest are order-independent.
_UPGRADE_SQL = f"""
UPDATE app_settings
SET value = replace(replace(replace(replace(
        value::text,
        'claude-max-sonnet', 'claude-sonnet-4-6'),
        'claude-max',        'claude-opus-4-8'),
        'nebius',            'meta-llama/Llama-3.3-70B-Instruct'),
        'effort_by_provider','effort_by_model')::jsonb
WHERE key = 'model_routing' AND value::text ~ '{_UPGRADE_MARKER}'
"""

# Reverse map (new → old). `claude-opus-4-8`/`claude-sonnet-4-6` share no prefix, so order-free.
_DOWNGRADE_SQL = f"""
UPDATE app_settings
SET value = replace(replace(replace(replace(
        value::text,
        'claude-opus-4-8',                    'claude-max'),
        'claude-sonnet-4-6',                  'claude-max-sonnet'),
        'meta-llama/Llama-3.3-70B-Instruct',  'nebius'),
        'effort_by_model',                    'effort_by_provider')::jsonb
WHERE key = 'model_routing' AND value::text ~ '{_DOWNGRADE_MARKER}'
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
