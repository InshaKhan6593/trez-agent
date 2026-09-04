"""Isolating a single conversational turn.

With a checkpointer, `state["messages"]` is the WHOLE history for that
thread_id. Scanning it for tool messages therefore returns every search the
customer has ever run, not the one they just asked for.

Production bug 2026-09-04: a customer searched Askari 5, then asked about
Gadap Town, and kept receiving the Askari list in every subsequent reply —
because the old tool output was still in the history being rendered.
"""

from __future__ import annotations


def current_turn(messages: list) -> list:
    """Messages produced since the customer's most recent message."""
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "type", None) == "human":
            return messages[i + 1:]
    return list(messages)


def turn_tool_output(messages: list) -> str:
    """Tool results from THIS turn only."""
    return "\n".join(
        str(m.content) for m in current_turn(messages)
        if getattr(m, "type", None) == "tool"
    )


def turn_tool_calls(messages: list) -> list[dict]:
    """Tool calls made during THIS turn only."""
    return [
        {"name": tc["name"], "args": tc["args"]}
        for m in current_turn(messages)
        for tc in (getattr(m, "tool_calls", None) or [])
    ]
