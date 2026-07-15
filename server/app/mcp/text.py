"""Authored usage text for the MCP surface (M5 task 4, ADR-046 §3).

The `initialize` **instructions** capsule (what the brain is · the six tools + when to use each ·
the efficient loop · temporal filters · the research-via-MCP pattern), the rich per-tool
descriptions, and the invokable **research-via-MCP** prompt (ADR-033 #6). Static text — distinct
from the *derived* identity capsule (ADR-046 §5).
"""

from __future__ import annotations

# The MCP `initialize` instructions field — the connected LLM reads this once (~300 tokens).
SERVER_INSTRUCTIONS = """\
This is the user's Personal Second Brain: a typed knowledge graph of their memories, people, \
ideas, projects, and insights. Nodes are typed and connected by typed edges; every node and edge \
has a stable id.

Tools:
- `search(query, ...)` — semantic + keyword search over the graph. Your default entry point.
- `build_context(id)` — the fastest way to understand one node: its content, profile, and a \
bounded neighborhood in one call. Level 0 is a short "about the user" capsule. Prefer this over \
get_node + traverse calls.
- `get_node(id)` — one node's full content, metadata, and edges.
- `traverse(id, rel?, cursor?)` — page a node's direct neighbors; filter by relation, follow the \
cursor for more.
- `list_planes()` / `list_types()` — the vocabulary (life areas + node/edge types).
- `capture(text)` — write a new memory. It is organized into typed node(s) in the background; the \
graph's organizer owns typing and linking, so write natural prose, not structure.

Efficient loop: `search` to find relevant ids → `build_context` on the best hit to understand it → \
`capture` anything new worth remembering. Chain by id — ids are exact, use them verbatim.

Temporal filters on `search`: `since`/`until` bound the event date; `as_of` asks what was known by \
a date. Use them for "recently", "last year", "at the time" questions.

Only `capture` writes; everything else is read-only. After a `capture`, `search` to confirm the \
node landed (it is processed asynchronously)."""

# Per-tool descriptions (annotations set read-only vs write separately in server.py).
SEARCH_DESCRIPTION = (
    "Search the user's second brain (hybrid semantic + keyword, ranked). Returns matching nodes "
    "with their ids, types, planes, tags, and a snippet. `planes`/`types` filter the results; "
    "`since`/`until` bound the event date (YYYY-MM-DD); `as_of` asks what was known by a date. "
    "Your default entry point — then chain the returned ids into `build_context`/`get_node`."
)
GET_NODE_DESCRIPTION = (
    "Fetch one node by id: its full content, metadata (type, planes, tags, aliases, dates), the "
    "derived profile (for people/places/projects), and its edges. Use `build_context` instead when "
    "you also want the surrounding neighborhood."
)
TRAVERSE_DESCRIPTION = (
    "List a node's directly connected neighbors (one hop). `rel` filters to one relation; "
    "`direction` is `out`/`in`/`both`; pass the returned `cursor` to page further. Backs graph "
    "exploration when you need more than `build_context`'s bounded neighborhood."
)
BUILD_CONTEXT_DESCRIPTION = (
    "Assemble a node's context in one call: a short 'about the user' capsule (level 0), the node "
    "itself, and a bounded neighbor tree up to `depth` (max 2). The most token-efficient way to "
    "understand a node and what surrounds it — prefer it over separate get_node + traverse calls."
)
LIST_PLANES_DESCRIPTION = "List the user's life-area planes (e.g. Professional, Personal, Health)."
LIST_TYPES_DESCRIPTION = "List the graph vocabulary: node types, edge relations, and entity types."
CAPTURE_DESCRIPTION = (
    "Write a new memory into the user's brain. Pass natural-language prose — the graph's organizer "
    "decides the node type(s), plane, tags, and links (you never write structure directly). "
    "Returns a capture id immediately; processing is asynchronous, so `search` shortly after to "
    "confirm the node(s). This is the only tool that writes."
)

# The invokable research-via-MCP prompt (ADR-033 #6 — "documented at M5").
RESEARCH_PROMPT_NAME = "research"
RESEARCH_PROMPT_DESCRIPTION = (
    "Research a topic against the user's brain, then enrich it: find what's already known, spot "
    "gaps, research externally, and capture the distilled findings back with source references."
)


def research_prompt(topic: str) -> str:
    """Render the research-via-MCP workflow prompt for a topic (ADR-033 #6)."""
    return f"""\
Research the topic: "{topic}" — for the user's Personal Second Brain.

1. Query what the brain already knows: `search("{topic}")`, then `build_context` on the most \
relevant nodes. Note the ids you rely on.
2. Identify gaps — what's missing, outdated, or unconnected relative to the topic.
3. Research those gaps with your external knowledge and tools.
4. `capture` the distilled findings as clear prose, explicitly referencing the node ids you built \
on (so the organizer can link the new memory to the existing graph). Capture only durable, \
non-trivial conclusions — not the raw search transcript.
5. Briefly summarize for the user what was already known, what you added, and the capture id(s)."""
