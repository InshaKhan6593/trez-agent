"""Agent middleware.

Middleware is the LangChain v1 hook system — it lets you intervene at each step
of the agent loop without touching the loop itself.
"""

from __future__ import annotations

import re

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

# Reasoning models (qwen3.x, deepseek-r1, and friends) emit their chain of
# thought wrapped in <think>...</think>. Via OpenRouter the opening tag is
# often already stripped, leaving the raw reasoning followed by a lone
# </think> and then the real answer — all inside `content`.
#
# Observed 2026-09-04 on qwen3.5-flash, "ghar chahiye":
#   "The customer is asking for a ghar... I should ask ONE qualifying
#    question first as per the guidelines.
#    </think>
#    Kya aap ghar khareedna chahte hain ya kiraye par lena chahte hain?"
#
# The answer was correct; only the presentation was broken. No prompt fixes
# this — the model is doing what it was trained to do, so strip it in code.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_DANGLING_CLOSE = re.compile(r"^.*?</think>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    if not text or "</think>" not in text.lower():
        return text
    cleaned = _THINK_BLOCK.sub("", text)
    if "</think>" in cleaned.lower():          # unmatched opening tag
        cleaned = _DANGLING_CLOSE.sub("", cleaned, count=1)
    return cleaned.strip()


class StripReasoningMiddleware(AgentMiddleware):
    """Remove chain-of-thought from the message the customer receives."""

    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or not isinstance(last.content, str):
            return None
        cleaned = strip_reasoning(last.content)
        if cleaned == last.content:
            return None
        # Same id => LangGraph replaces the message rather than appending.
        return {"messages": [last.model_copy(update={"content": cleaned})]}
