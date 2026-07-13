"""Organizer: prompts + pure parsing/validation of the LLM organize step (ADR-005/019).

The organizer turns raw capture text into one or more atomic notes, split per plane. Every
function here is pure and unit-tested — no I/O, no provider calls. The pipeline
(``services/capture_pipeline.py``) owns the actual ``registry.distill()`` call and the
never-lose fallback decision; this module only shapes prompts and sanitises model output.

Never-lose contract (CLAUDE.md rule 2): a malformed or empty organize result must NOT fail a
capture. :func:`validate_organizer_output` returns ``[]`` for unusable output and the pipeline
falls back to a single Inbox note via :func:`inbox_fallback_note`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# --- Versioned prompt constants (ADR-019 §4). Bump the suffix on any wording change. ---

ORGANIZER_PROMPT_VERSION = "organizer-v3"  # v3: existing vault tag vocabulary injected (ADR-024)
NUDGE_PROMPT_VERSION = "nudge-v2"  # v2: sourced from the raw capture; explicit language match

# Organic tagging (ADR-019 / M1 build decisions): emotional tone + the what/why around the
# feelings, free-form — no rigid taxonomy. Splitting per plane produces atomic notes (ADR-005).
ORGANIZER_SYSTEM_PROMPT = """\
You organize a person's raw capture (a voice memo transcript or a typed note) into one or
more atomic notes for their personal knowledge vault.

Return ONLY a JSON object, no prose, in exactly this shape:
{"notes": [{"title": str, "plane": str, "planes": [str], "tags": [str], "body": str}]}

Rules:
- Split the capture into as few atomic notes as the content honestly needs. One coherent
  thought = one note. Only split when the capture genuinely spans separate topics or life
  areas (planes).
- "plane" is the note's primary life area and MUST be one of the configured planes below,
  matched case-insensitively. If you cannot confidently place it, use "{inbox}".
- "planes" is the full set of life areas the note touches (a superset of "plane"); use the
  configured planes only.
- "tags" are organic and free-form: capture the emotional tone and the what/why around any
  feelings, plus salient topics. No rigid taxonomy. Each tag MUST be a valid Obsidian tag:
  English, lower-case, a single word or hyphenated (e.g. "personal-growth"), NO spaces, no "#".
  Prefer reusing a tag from the existing vault vocabulary below when one genuinely fits; only
  coin a new tag when nothing there matches. This keeps the vocabulary from fragmenting into
  near-duplicate variants.
- "body" is the cleaned, lightly-structured note content in Markdown (do not invent facts;
  preserve the person's meaning).
- Write EVERY title, body, and tag in English. If the capture is in another language,
  translate its meaning into natural English — do not leave phrases in the original language.
{tag_vocabulary}
Configured planes: {planes}
"""

NUDGE_SYSTEM_PROMPT = """\
Below is a person's raw capture (a voice memo transcript or a typed note) that was just saved
to their vault. Ask ONE short, warm, open question inviting them to expand on the most
emotionally or substantively significant thread — the kind of gentle nudge that draws a thought
out. At most 20 words. Never an interrogation, never multiple questions. Detect the language of
the capture and write the question in that SAME language (e.g. an English capture gets an
English question). Return only the question text.
"""


def render_tag_vocabulary(tags: list[str]) -> str:
    """Render the existing-tag list injected into the organizer prompt (ADR-024 §1).

    ``tags`` is the current vault vocabulary (distinct ``notes.tags``, most-used first, already
    capped by the caller). Returns a short prompt block, or ``""`` for a cold vault with no tags
    yet — so the ``{tag_vocabulary}`` token simply collapses to a blank line and the organizer
    tags organically, as before. Pure: the caller fetches the vocabulary.
    """
    if not tags:
        return ""
    return (
        "Existing vault tags (most-used first) — prefer reusing one of these when it fits:\n"
        + ", ".join(tags)
    )


@dataclass(frozen=True)
class OrganizerNote:
    """One validated atomic note ready to be written to the vault."""

    title: str
    plane: str
    planes: tuple[str, ...]
    tags: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class OrganizeResult:
    """Outcome of validating a model organize response.

    ``used_fallback`` is True when the output was unusable and the caller must synthesise an
    Inbox note — in which case NO follow-up nudge is generated (ADR-019 §1).

    ``model_used`` / ``provider_fallback_used`` carry the *provider-chain* resolution of the
    organize call (which chat provider answered, and whether the chain fell back) for the
    capture ``agent_runs`` interaction log (ADR-021). Distinct from ``used_fallback``, which is
    about the *Inbox* fallback (the chain was exhausted and no model answered → both empty/False
    here with ``used_fallback`` True).
    """

    notes: tuple[OrganizerNote, ...]
    used_fallback: bool = field(default=False)
    model_used: str = field(default="")
    provider_fallback_used: bool = field(default=False)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_organizer_json(text: str) -> dict | None:
    """Best-effort parse of the model's JSON, tolerating code fences and surrounding prose.

    Returns the decoded object, or ``None`` if nothing parseable is found (caller → Inbox).
    """
    if not text:
        return None
    candidate = _FENCE_RE.sub("", text.strip())
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to the first balanced-looking {...} span.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _canonical_plane(value: object, plane_lookup: dict[str, str], inbox_plane: str) -> str:
    """Map a raw plane string to its configured canonical spelling, else Inbox."""
    if not isinstance(value, str):
        return inbox_plane
    return plane_lookup.get(value.strip().lower(), inbox_plane)


# Chars allowed in an Obsidian tag body; anything else (spaces, punctuation) is a separator.
_TAG_INVALID = re.compile(r"[^a-z0-9_/-]+")


def _slugify_tag(raw: str) -> str:
    """Reduce a free-form tag to a valid Obsidian tag (02-data-model): lower-case, no spaces,
    hyphenated. Obsidian tags allow letters/digits/``_ - /`` and MUST contain a non-numeric
    character. Spaces/punctuation collapse to a single hyphen; a purely-numeric or empty result
    is dropped (returns "")."""
    tag = _TAG_INVALID.sub("-", raw.strip().lstrip("#").strip().lower())
    tag = re.sub(r"-{2,}", "-", tag).strip("-/_")
    return tag if any(c.isalpha() for c in tag) else ""


def _clean_tags(value: object, *, max_tags: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = _slugify_tag(item)
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= max_tags:
            break
    return tuple(seen)


def validate_organizer_output(
    parsed: dict | None,
    *,
    planes: list[str],
    inbox_plane: str,
    max_notes: int,
    max_tags: int,
) -> tuple[OrganizerNote, ...]:
    """Pure validation of a parsed organize object into safe :class:`OrganizerNote`s.

    - ``plane`` is normalised to a configured plane's canonical spelling (case-insensitive);
      unknown/missing → Inbox. Inbox itself is always a valid target.
    - ``planes`` is filtered to configured planes and made a superset of ``plane``.
    - A note with an empty title/body after stripping is dropped.
    - At most ``max_notes`` notes and ``max_tags`` tags per note are kept.

    Returns ``()`` when nothing usable is present — the caller then applies the Inbox fallback.
    """
    if not isinstance(parsed, dict):
        return ()
    raw_notes = parsed.get("notes")
    if not isinstance(raw_notes, list):
        return ()

    # Canonical spelling lookup keyed by lower-case; Inbox is always allowed.
    plane_lookup = {p.strip().lower(): p for p in planes}
    plane_lookup[inbox_plane.strip().lower()] = inbox_plane

    result: list[OrganizerNote] = []
    for raw in raw_notes:
        if not isinstance(raw, dict):
            continue
        title = raw.get("title")
        body = raw.get("body")
        if not isinstance(title, str) or not isinstance(body, str):
            continue
        title = title.strip()
        body = body.strip()
        if not title or not body:
            continue

        primary = _canonical_plane(raw.get("plane"), plane_lookup, inbox_plane)

        membership: list[str] = [primary]
        raw_planes = raw.get("planes")
        if isinstance(raw_planes, list):
            for item in raw_planes:
                canon = _canonical_plane(item, plane_lookup, inbox_plane)
                # Only add extra memberships that are configured planes (not the Inbox catch-all
                # unless it is the primary) and de-dup.
                if canon not in membership and (canon != inbox_plane or canon == primary):
                    membership.append(canon)

        result.append(
            OrganizerNote(
                title=title,
                plane=primary,
                planes=tuple(membership),
                tags=_clean_tags(raw.get("tags"), max_tags=max_tags),
                body=body,
            )
        )
        if len(result) >= max_notes:
            break

    return tuple(result)


def inbox_fallback_note(raw_text: str, *, inbox_plane: str) -> OrganizerNote:
    """The never-lose note: title = first 8 words, body = full raw text, plane = Inbox.

    Used when the organizer chain is exhausted, the output is unparseable, or zero valid notes
    survive validation. A capture is never lost to a model error (CLAUDE.md rule 2).
    """
    stripped = raw_text.strip()
    words = stripped.split()
    title = " ".join(words[:8]) if words else "Untitled capture"
    body = stripped if stripped else "(empty capture)"
    return OrganizerNote(
        title=title,
        plane=inbox_plane,
        planes=(inbox_plane,),
        tags=(),
        body=body,
    )
