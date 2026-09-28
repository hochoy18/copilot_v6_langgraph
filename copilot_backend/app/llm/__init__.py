"""LLM integration layer — T16 / #14 (ADR-0013 / ADR-0014 / ADR-0016).

Two seams live here:

* `app.llm.provider` — builds a LangChain `BaseChatModel` from runtime
  settings (ADR-0014: the business side only ever sees the ChatModel
  abstraction; swapping OpenAI-compatible providers is config-only).
* `app.llm.prompts` — fetches Prompt templates from Langfuse at runtime
  (ADR-0013), with the documented degradation ladder: fresh fetch →
  cached last-good copy → code-embedded bootstrap template.

Callers (the Tool description generator, later the Planner) receive
these through `app.db.dependencies` so tests can swap them wholesale.
"""
