"""L3 — agent-level evaluation, deterministic tier.

Deterministic only: tool choice, tool arguments, invented listing ids,
WhatsApp formatting, reasoning leaks. Free, fast, no model involved.

For LLM-as-judge scoring and LangSmith experiments use evals.langsmith_eval —
this module is the quick local loop when iterating on one golden.

Usage:
    uv run python -m evals.run_agent_evals              # all goldens
    uv run python -m evals.run_agent_evals --only A4    # one golden
"""

from __future__ import annotations

import argparse
import re
import sys

import psycopg
from langgraph.checkpoint.memory import InMemorySaver

from agents.db import DSN
from agents.graph import MODEL, build_agent
from agents.turn import turn_tool_calls, turn_tool_output

from .goldens import AGENT_GOLDENS

ID_RE = re.compile(r"\bid=(\d{6,})\b")
LISTING_IN_TEXT = re.compile(r"\b(\d{8})\b")

# The model narrating itself must never reach a customer on WhatsApp.
META_LEAK = re.compile(
    r"\b(the user is|the customer is|i should|i will now|"
    r"according to the guidelines|based on the inventory summary)\b",
    re.I,
)


def known_area_names() -> set[str]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        return {r[0].lower() for r in cur.execute("SELECT name FROM locations").fetchall()}


def run_agent(inputs: list[str]) -> dict:
    """Run one conversation and return everything needed to grade it."""
    agent = build_agent(InMemorySaver())
    cfg = {"configurable": {"thread_id": f"eval-{abs(hash(tuple(inputs)))}"}}
    state = None
    for text in inputs:
        state = agent.invoke({"messages": [{"role": "user", "content": text}]}, cfg)

    # THIS turn only. Scanning the whole history made a multi-turn golden
    # grade every earlier search too — the same bug that made the live agent
    # resend an Askari list when the customer asked about Gadap Town.
    tool_calls = turn_tool_calls(state["messages"])
    return {
        "reply": state["messages"][-1].content or "",
        "tool_calls": tool_calls,
        "tool_names": [c["name"] for c in tool_calls],
        "tool_outputs": turn_tool_output(state["messages"]),
    }


def grade_deterministic(g: dict, run: dict, areas: set[str]) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    reply, names = run["reply"], run["tool_names"]

    for want in g.get("must_call", []):
        checks.append((want in names, f"calls {want}"))

    for banned in g.get("must_not_call", []):
        checks.append((banned not in names, f"does NOT call {banned}"))

    for key, val in (g.get("args_contain") or {}).items():
        got = [c["args"].get(key) for c in run["tool_calls"] if key in c["args"]]
        checks.append((val in got, f"arg {key}={val}" + ("" if val in got else f"  got {got}")))

    # invented listing ids — every id in the reply must have come from a tool
    from_tools = set(ID_RE.findall(run["tool_outputs"]))
    in_reply = set(LISTING_IN_TEXT.findall(reply))
    invented = in_reply - from_tools
    checks.append((not invented, "no invented listing ids"
                   + ("" if not invented else f"  INVENTED {sorted(invented)}")))

    for bad in g.get("must_not_mention_ids", []):
        checks.append((str(bad) not in reply, f"does not mention listing {bad}"))

    for bad in g.get("reply_must_not", []):
        checks.append((bad.lower() not in reply.lower(), f"never says '{bad}'"))

    for pat in g.get("reply_must_not_match", []):
        hit = re.search(pat, reply, re.M)
        checks.append((hit is None, f"no match /{pat}/"))

    # meta-commentary leaking into the reply. A customer must never read
    # "The user is asking..." — that is the model narrating itself.
    meta = re.search(META_LEAK, reply)
    checks.append((meta is None, "no meta-commentary"
                   + ("" if meta is None else f"  LEAKED '{meta.group(0)}'")))

    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run one golden by id prefix, e.g. A4")
    args = ap.parse_args()

    goldens = AGENT_GOLDENS
    if args.only:
        goldens = [g for g in goldens if g["id"].startswith(args.only)]

    areas = known_area_names()
    print(f"agent model : {MODEL}")
    print(f"goldens     : {len(goldens)}\n")

    total_fail = 0
    for g in goldens:
        inputs = g["input"] if isinstance(g["input"], list) else [g["input"]]
        try:
            run = run_agent(inputs)
        except Exception as e:
            print(f"{'ERROR':<6} {g['id']}  {type(e).__name__}: {e}")
            total_fail += 1
            continue

        checks = grade_deterministic(g, run, areas)

        failed = [c for ok, c in checks if not ok]
        total_fail += bool(failed)
        print(f"{'PASS' if not failed else 'FAIL':<6} {g['id']}")
        print(f"       input  : {' | '.join(inputs)}")
        print(f"       tools  : {run['tool_names'] or '(none)'}")
        for ok, label in checks:
            print(f"         {'ok  ' if ok else 'MISS'} {label}")
        if failed:
            print(f"       reply  : {run['reply'][:300]}")
        print()

    print(f"{len(goldens) - total_fail}/{len(goldens)} goldens passed")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
