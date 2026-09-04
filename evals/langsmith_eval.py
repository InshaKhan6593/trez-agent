"""LangSmith-native evaluation.

Uses LangSmith datasets + client.evaluate(), so every run becomes an
"experiment" you can diff against previous ones in the UI.

Two kinds of evaluator, deliberately:

  DETERMINISTIC  plain Python. Tool choice, tool args, invented ids, WhatsApp
                 formatting, reasoning leaks. Free, exact, no model involved.
                 These are the checks that actually catch retrieval bugs.

  LLM-AS-JUDGE   openevals prebuilt prompts (RAG_GROUNDEDNESS, TOOL_SELECTION).
                 These are tuned by LangChain — much better than criteria
                 hand-written in an afternoon, which scored honest replies at
                 0.2 because the wording drifted into "answer quality".

Usage:
    uv run python -m evals.langsmith_eval --push     # create/refresh dataset
    uv run python -m evals.langsmith_eval            # run an experiment
    uv run python -m evals.langsmith_eval --no-judge # deterministic only (free)
"""

from __future__ import annotations

import argparse
import os
import re

from langsmith import Client

from .goldens import AGENT_GOLDENS
from .run_agent_evals import ID_RE, LISTING_IN_TEXT, META_LEAK, run_agent

DATASET = os.getenv("LANGSMITH_DATASET", "trez-agent-goldens")
JUDGE = os.getenv("JUDGE_MODEL", "openai/gpt-4o-mini")


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------

def push_dataset(client: Client) -> str:
    """Create or refresh the dataset from goldens.py (goldens.py is the source
    of truth; LangSmith is a mirror you can browse and annotate)."""
    if client.has_dataset(dataset_name=DATASET):
        ds = client.read_dataset(dataset_name=DATASET)
        for ex in client.list_examples(dataset_id=ds.id):
            client.delete_example(example_id=ex.id)
    else:
        ds = client.create_dataset(
            dataset_name=DATASET,
            description="Trez WhatsApp agent goldens. Expected values verified "
                        "against the live Postgres inventory.",
        )

    client.create_examples(
        dataset_id=ds.id,
        examples=[
            {
                "inputs": {"messages": g["input"] if isinstance(g["input"], list)
                           else [g["input"]]},
                "outputs": {  # reference_outputs — what SHOULD happen
                    "must_call": g.get("must_call", []),
                    "must_not_call": g.get("must_not_call", []),
                    "args_contain": g.get("args_contain", {}),
                    "reply_must_not": g.get("reply_must_not", []),
                    "must_not_mention_ids": sorted(g.get("must_not_mention_ids", [])),
                    "reply_must_not_match": g.get("reply_must_not_match", []),
                },
                "metadata": {"golden_id": g["id"], "note": g.get("note", "")},
            }
            for g in AGENT_GOLDENS
        ],
    )
    return ds.id


# ---------------------------------------------------------------------------
# target — the thing under test
# ---------------------------------------------------------------------------

def target(inputs: dict) -> dict:
    run = run_agent(inputs["messages"])
    return {
        "reply": run["reply"],
        "tool_names": run["tool_names"],
        "tool_calls": run["tool_calls"],
        "tool_outputs": run["tool_outputs"],
    }


# ---------------------------------------------------------------------------
# deterministic evaluators
# ---------------------------------------------------------------------------

def _same(a, b) -> bool:
    """Tool args arrive as strings ('21109'), goldens hold ints (21109).
    Compare by string form so a type mismatch is not reported as a failure —
    this was a real bug in the first version of this harness."""
    return str(a).strip().lower() == str(b).strip().lower()


def tool_selection(outputs: dict, reference_outputs: dict) -> dict:
    names = outputs["tool_names"]
    missing = [t for t in reference_outputs["must_call"] if t not in names]
    forbidden = [t for t in reference_outputs["must_not_call"] if t in names]
    ok = not missing and not forbidden
    return {"key": "tool_selection", "score": int(ok),
            "comment": "ok" if ok else f"missing={missing} forbidden={forbidden}"}


def tool_arguments(outputs: dict, reference_outputs: dict) -> dict:
    want = reference_outputs["args_contain"]
    if not want:
        return {"key": "tool_arguments", "score": 1, "comment": "n/a"}
    bad = []
    for k, v in want.items():
        got = [c["args"].get(k) for c in outputs["tool_calls"] if k in c["args"]]
        if not any(_same(g, v) for g in got):
            bad.append(f"{k}: want {v}, got {got}")
    return {"key": "tool_arguments", "score": int(not bad),
            "comment": "ok" if not bad else "; ".join(bad)}


def no_invented_listings(outputs: dict) -> dict:
    """Every listing id in the reply must have come from a tool result.
    This is the single most important check in the suite."""
    from_tools = set(ID_RE.findall(outputs["tool_outputs"]))
    invented = set(LISTING_IN_TEXT.findall(outputs["reply"])) - from_tools
    return {"key": "no_invented_listings", "score": int(not invented),
            "comment": "ok" if not invented else f"INVENTED {sorted(invented)}"}


def banned_content(outputs: dict, reference_outputs: dict) -> dict:
    reply = outputs["reply"]
    hits = [b for b in reference_outputs["reply_must_not"] if b.lower() in reply.lower()]
    hits += [str(i) for i in reference_outputs["must_not_mention_ids"] if str(i) in reply]
    hits += [p for p in reference_outputs["reply_must_not_match"]
             if re.search(p, reply, re.M)]
    return {"key": "banned_content", "score": int(not hits),
            "comment": "ok" if not hits else f"found {hits}"}


def whatsapp_safe(outputs: dict) -> dict:
    """WhatsApp renders none of: tables, **bold**, # headers, --- rules."""
    bad = [p for p in (r"^\s*\|.*\|", r"\*\*", r"^#{1,6}\s", r"^\s*-{3,}\s*$")
           if re.search(p, outputs["reply"], re.M)]
    return {"key": "whatsapp_safe", "score": int(not bad),
            "comment": "ok" if not bad else f"unrenderable: {bad}"}


def no_reasoning_leak(outputs: dict) -> dict:
    hit = META_LEAK.search(outputs["reply"])
    return {"key": "no_reasoning_leak", "score": int(hit is None),
            "comment": "ok" if not hit else f"leaked '{hit.group(0)}'"}


DETERMINISTIC = [tool_selection, tool_arguments, no_invented_listings,
                 banned_content, whatsapp_safe, no_reasoning_leak]


# ---------------------------------------------------------------------------
# LLM judges — openevals prebuilt prompts
# ---------------------------------------------------------------------------

# openevals' create_llm_as_judge could not parse ChatOpenRouter's structured
# output (KeyError: 'score'). The valuable part of openevals is the tuned
# PROMPT TEXT, not its plumbing — and the prompts are plain f-strings. So we
# format their prompt ourselves and score it with a structured-output call
# that is verified to work against OpenRouter.
_SCORE_SCHEMA = {
    "title": "score",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasoning": {"type": "string", "description": "Why you gave this score."},
        "score": {
            "type": "number",
            "description": "0.0 to 1.0. 1.0 means the criteria are fully met, "
                           "0.0 means not met at all.",
        },
    },
    "required": ["reasoning", "score"],
}


def build_judges():
    from langchain_openrouter import ChatOpenRouter
    from openevals.prompts import RAG_GROUNDEDNESS_PROMPT

    model = ChatOpenRouter(model=JUDGE, temperature=0).with_structured_output(_SCORE_SCHEMA)

    def _score(key: str, prompt: str) -> dict:
        r = model.invoke(prompt)
        return {"key": key, "score": float(r["score"]), "comment": r["reasoning"][:400]}

    def judge_groundedness(inputs: dict, outputs: dict) -> dict | None:
        # Does the reply stay inside what the tools actually returned?
        # This is exactly A9's bug: claiming plots exist under 50 lakh when
        # the only plots cost 85 lakh.
        #
        # GATED: with no tool output there is no context to be grounded in.
        # Ungated, this scored 0.00 on replies that correctly asked a
        # qualifying question first — conflating "invented something" with
        # "hasn't retrieved yet". Returning None records no feedback.
        if not outputs["tool_outputs"].strip():
            return None
        return _score("groundedness", RAG_GROUNDEDNESS_PROMPT.format(
            context=outputs["tool_outputs"],
            outputs=outputs["reply"],
        ))

    # NOTE: openevals' TOOL_SELECTION_PROMPT is deliberately NOT used.
    # It assumes an agent should always call a tool, so it scored 0.00 every
    # time this agent correctly asked a qualifying question instead of
    # searching — which the system prompt mandates. It measured 0.30 across
    # the suite while the deterministic tool_selection check measured 0.92.
    # Wrong instrument for a conversational agent; the deterministic
    # must_call / must_not_call check is the honest signal here.

    return [judge_groundedness]


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="create/refresh the dataset only")
    ap.add_argument("--no-judge", action="store_true", help="deterministic only (free)")
    ap.add_argument("--prefix", default="trez")
    args = ap.parse_args()

    client = Client()

    if args.push:
        ds_id = push_dataset(client)
        print(f"dataset '{DATASET}' refreshed with {len(AGENT_GOLDENS)} examples\n  {ds_id}")
        return 0

    if not client.has_dataset(dataset_name=DATASET):
        push_dataset(client)
        print(f"created dataset '{DATASET}'")

    evaluators = list(DETERMINISTIC)
    if not args.no_judge:
        evaluators += build_judges()

    print(f"dataset    : {DATASET}")
    print(f"evaluators : {[getattr(e, '__name__', str(e)) for e in evaluators]}")
    print(f"judge      : {'(none)' if args.no_judge else JUDGE}\n")

    results = client.evaluate(
        target,
        data=DATASET,
        evaluators=evaluators,
        experiment_prefix=args.prefix,
        max_concurrency=2,
    )
    print(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
