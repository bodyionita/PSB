"""Pure tag-consolidation logic (ADR-024 §2) — prompts, plan sanitisation, node rewriting.

Everything here is pure (no I/O, no provider calls) so it is unit-tested with no mocks (08
testing policy). The :class:`~app.tags.service.TagConsolidationService` owns the actual distill
call, the store reads/writes, and the reindex; this module only shapes the propose prompt,
sanitises a merge plan into a safe canonical→variant mapping, and rewrites a node's ``tags:``
frontmatter line.

Tag slugging reuses ``_slugify_tag`` (the single authority on a valid tag slug, 02 §2) and
frontmatter reading/rendering reuses the same inline-list helpers the rest of the code emits, so
a rewritten ``tags:`` line is byte-identical in shape to a freshly organized node.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..capture.organizer import _slugify_tag
from ..graph.node_writer import _yaml_list
from ..indexing.chunking import _normalize_newlines
from ..indexing.frontmatter import _parse_inline_list, _unquote

# --- Versioned prompt (bump the suffix on any wording change, mirroring the organizer). ---
CONSOLIDATION_PROMPT_VERSION = "tags-consolidate-v2"  # v2: node/graph vocabulary (M3 pivot)

CONSOLIDATION_SYSTEM_PROMPT = """\
You are cleaning up a tag vocabulary for a personal knowledge graph. Below is the list of tags
currently in use, each with the number of nodes it appears on (most-used first).

Group together ONLY tags that are genuine duplicates or variants of the SAME concept — spelling
or spacing variants, singular/plural, or obvious synonyms (e.g. "second-brain" / "secondbrain" /
"second-brain-app"). Do NOT merge tags that are merely related but distinct (e.g. "health" and
"fitness" are different facets — leave them separate). When in doubt, leave a tag alone.

For each group, choose the single best canonical tag to keep — normally the clearest, most-used
variant. Every tag you list (canonical and variants) MUST be one of the tags below, verbatim.

Return ONLY a JSON object, no prose, in exactly this shape:
{"merges": [{"canonical": str, "variants": [str]}]}

If nothing should be merged, return {"merges": []}.

Current tags:
{vocabulary}
"""


@dataclass(frozen=True)
class TagMerge:
    """One sanitised merge: fold ``variants`` into ``canonical`` (all valid, distinct tags)."""

    canonical: str
    variants: tuple[str, ...]


def render_vocabulary(tag_counts: list[tuple[str, int]]) -> str:
    """Render the ``tag (n)`` lines injected into the propose prompt (most-used first)."""
    return "\n".join(f"- {tag} ({count})" for tag, count in tag_counts)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_merge_plan(text: str) -> list[tuple[str, list[str]]]:
    """Best-effort parse of the model's merge JSON into ``(canonical, variants)`` pairs.

    Tolerates code fences / surrounding prose (mirrors the organizer parse). Malformed or
    non-conforming output yields ``[]`` — the caller then proposes nothing rather than erroring.
    """
    obj = _loads(text)
    if not isinstance(obj, dict):
        return []
    raw_merges = obj.get("merges")
    if not isinstance(raw_merges, list):
        return []
    pairs: list[tuple[str, list[str]]] = []
    for merge in raw_merges:
        if not isinstance(merge, dict):
            continue
        canonical = merge.get("canonical")
        variants = merge.get("variants")
        if not isinstance(canonical, str) or not isinstance(variants, list):
            continue
        pairs.append((canonical, [v for v in variants if isinstance(v, str)]))
    return pairs


def _loads(text: str) -> object | None:
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def clean_merges(
    raw_pairs: list[tuple[str, list[str]]],
    *,
    allowed: dict[str, int] | None = None,
) -> list[TagMerge]:
    """Sanitise raw ``(canonical, variants)`` pairs into safe, non-overlapping :class:`TagMerge`s.

    - Every tag is slugified (``_slugify_tag``); empties drop out.
    - When ``allowed`` (tag→frequency) is given, tags outside the current vocabulary are dropped
      (the propose path passes it so a hallucinated tag can't enter a plan; apply trusts the
      reviewed plan and passes ``None``).
    - A merge needs ≥2 distinct members after cleaning, else it's discarded.
    - The canonical is the given one when it survives, otherwise the highest-frequency member
      (deterministic tie-break: shorter then alphabetical).
    - A tag already consumed by an earlier merge is not reused, so the resulting mapping is a
      function (no tag maps to two canonicals).
    """
    seen: set[str] = set()
    result: list[TagMerge] = []
    for canonical_raw, variants_raw in raw_pairs:
        members: list[str] = []
        for raw in (canonical_raw, *variants_raw):
            slug = _slugify_tag(raw)
            if not slug or (allowed is not None and slug not in allowed):
                continue
            if slug not in members and slug not in seen:
                members.append(slug)
        if len(members) < 2:
            continue
        canonical = _slugify_tag(canonical_raw)
        if canonical not in members:
            canonical = _pick_canonical(members, allowed)
        variants = tuple(t for t in members if t != canonical)
        seen.update(members)
        result.append(TagMerge(canonical=canonical, variants=variants))
    return result


def _pick_canonical(members: list[str], allowed: dict[str, int] | None) -> str:
    """Highest-frequency member, tie-broken by shortest then alphabetical (deterministic)."""
    return sorted(
        members,
        key=lambda t: (-(allowed.get(t, 0) if allowed else 0), len(t), t),
    )[0]


def build_tag_mapping(merges: list[TagMerge]) -> dict[str, str]:
    """Flatten merges into a ``variant → canonical`` mapping (canonicals map to themselves)."""
    return {variant: merge.canonical for merge in merges for variant in merge.variants}


def remap_tags(tags: list[str], mapping: dict[str, str]) -> list[str]:
    """Apply ``mapping`` to a tag list, preserving order and de-duplicating the result."""
    out: list[str] = []
    for tag in tags:
        mapped = mapping.get(tag, tag)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def rewrite_node_tags(raw_text: str, mapping: dict[str, str]) -> tuple[str, bool]:
    """Rewrite a node's ``tags:`` frontmatter line under ``mapping`` (canonical replaces variant).

    Returns ``(new_text, changed)``. Only the single top-level ``tags:`` line inside the leading
    ``---`` frontmatter is touched — every other byte (body, other keys, the ``edges:`` block) is
    preserved; when nothing changes the *original* text is returned verbatim (no spurious newline
    churn). A node without frontmatter or without a ``tags:`` line is left as-is.
    """
    text = _normalize_newlines(raw_text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return raw_text, False
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        return raw_text, False

    for i in range(1, close):
        line = lines[i]
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() != "tags":
            continue
        old_tags = _parse_tags_value(value.strip())
        new_tags = remap_tags(old_tags, mapping)
        if new_tags == old_tags:
            return raw_text, False
        lines[i] = f"tags: {_yaml_list(new_tags)}"
        return "\n".join(lines), True
    return raw_text, False


def _parse_tags_value(value: str) -> list[str]:
    """Parse a frontmatter ``tags:`` value — inline ``[a, b]`` list or a bare/quoted scalar.

    Only the shapes this project emits are understood (matching ``frontmatter.parse_frontmatter``);
    a hand-edited multi-line YAML block list (``tags:\\n  - x``) yields ``[]`` and is left alone —
    such a node also never enters ``nodes.tags`` / the vocabulary, so it is simply invisible to the
    tag subsystem, never mis-rewritten.
    """
    if value.startswith("[") and value.endswith("]"):
        return _parse_inline_list(value[1:-1])
    scalar = _unquote(value)
    return [scalar] if scalar else []
