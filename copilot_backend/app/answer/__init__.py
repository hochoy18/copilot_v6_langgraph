"""T22 / #19 — LLM final answer streaming.

Submodules:

* `generator` — `AnswerGenerator`, the pure LLM-call layer (Langfuse
  `result-summarizer` Prompt, streaming `BaseChatModel.astream`).
* `service` — `AnswerService`, the orchestrator that wires the
  generator to the SSE bus (per-token `llm.token` events) and
  persists the assembled reply as an `assistant` Turn.
"""