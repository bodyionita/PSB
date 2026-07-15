"""Remote MCP server over the service layer (M5 task 4, ADR-046 §1/§3/§4).

Streamable HTTP under ``/mcp`` on the existing api app, gated by the task-3 OAuth 2.1 resource
server. The six tools are **thin over the service layer** (ADR-028 — no logic of their own) and
render **LLM-optimized Markdown** at the boundary (a pure presentation seam, DTOs untouched); IDs
are emitted verbatim so the model chains ``search`` → ``build_context`` → ``capture``.
"""
