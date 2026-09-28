"""Planner subsystem — T18 / #16 (ADR-0004 / ADR-0013 / ADR-0027).

Per SPEC §模块边界 this is the `planner/` module: the seam that turns a
user's natural-language instruction into a Plan. Two layers live here:

* `app.planner.planner.ToolPlanner` — the Planner LLM call itself. It
  renders the Langfuse `planner` Prompt with the active-Tool catalog,
  invokes the ChatModel, parses the strict-JSON contract, and binds
  the returned Tool names back to live `Tool` rows. A plain async
  callable on purpose: the LangGraph `StateGraph` (ADR-0012, T28)
  wraps node functions exactly like this one — no adapter needed.
* `app.planner.service.PlannerService` — the turn-level orchestration:
  ownership guard, Turn persistence, snapshot freezing (ADR-0027),
  Plan persistence (status `pending`, awaiting the HITL preview —
  ADR-0004), and the graceful-degradation ladder.

T18's scope is the single-Tool Plan (ticket #16); multi-node output
arrives with T25 (#22) by editing the Langfuse copy — the parser here
already accepts N nodes.
"""
