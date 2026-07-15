"""Identity-capsule package (M5 task 2, ADR-046 §5 / ADR-033 #1).

The derived ~300-token "who the user is / current state" capsule: distilled nightly on the
``conspect`` tier from a blend of the graph's high-degree entity-profile hubs, recent memories, and
recent insights, stored as a rebuildable blob in ``app_settings`` (rule 1), and served as
``build_context`` level-0 + wired into the M4 chat system prompt. The MCP ``identity://me`` resource
(task 4) reads the same blob.
"""
