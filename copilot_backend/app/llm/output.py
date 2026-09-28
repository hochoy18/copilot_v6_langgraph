"""Shared parsing of structured LLM output (T16 / #14, T18 / #16).

Every first-party Prompt in this codebase asks the model for one JSON
object (the `tool-description-generator` contract, the `planner`
contract, and later the `result-summarizer` — T22). Models do not
always obey "只输出一个 JSON 对象": some wrap it in a ```json fence,
some prepend a sentence of prose. These two helpers absorb that
mess once instead of per-call-site.

* `content_to_text` normalises a LangChain message `content` payload
  (`str` or a list of typed blocks) to plain text.
* `extract_json_object` tries, in order: the whole payload as JSON, a
  fenced ```json block, the outermost brace pair. Returns `None` when
  nothing parses to a JSON object — the caller decides which of its
  own contract keys are required and raises its domain error from
  there.

Kept free of contract knowledge on purpose: validating the parsed
dict's shape belongs to the caller, whose contract (description vs
plan nodes) this module must not assume.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Fenced code block extractor: ```json … ``` or plain ``` … ```.
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def content_to_text(content: Any) -> str:
    """Normalise a LangChain message content payload to plain text.

    Chat models return either `str` or a list of content blocks
    (`{"type": "text", "text": …}` for OpenAI-compatible providers).
    Anything else is stringified defensively — the JSON extractor
    still gets a chance to find braces inside it.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "".join(chunks)
    return str(content)


def extract_json_object(content: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of model output, or `None`.

    The three candidates are tried cheapest-first: whole payload,
    fenced block, outermost braces. Non-dict JSON (arrays, scalars)
    counts as a miss — every contract here is an object.
    """
    candidate = content.strip()

    try:
        maybe = json.loads(candidate)
        if isinstance(maybe, dict):
            return maybe
    except ValueError:
        pass

    fenced = _FENCED_JSON_RE.search(candidate)
    if fenced is not None:
        try:
            maybe = json.loads(fenced.group(1))
            if isinstance(maybe, dict):
                return maybe
        except ValueError:
            pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if 0 <= start < end:
        try:
            maybe = json.loads(candidate[start : end + 1])
            if isinstance(maybe, dict):
                return maybe
        except ValueError:
            pass

    return None


__all__ = ["content_to_text", "extract_json_object"]
