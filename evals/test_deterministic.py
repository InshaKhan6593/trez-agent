"""L1 + L2 — deterministic checks. No LLM, no cost, milliseconds.

Run these on every commit. If they fail, no agent built on top can be correct,
so there is no point looking at agent behaviour until these are green.

    uv run python -m evals.test_deterministic     # readable report
    uv run pytest evals/test_deterministic.py     # CI
"""

from __future__ import annotations

import sys

from agents.db import decide, resolve_location, search_listings

from .goldens import LOCATION_GOLDENS, SEARCH_GOLDENS


def check_locations() -> list[tuple[bool, str]]:
    out = []
    for text, want_id, want_verdict, note in LOCATION_GOLDENS:
        cands = resolve_location(text)
        verdict = decide(cands)
        got_id = cands[0]["location_id"] if cands else None
        ok = (verdict == want_verdict) and (want_id is None or got_id == want_id)
        got = f"{cands[0]['name']} ({got_id})" if cands else "—"
        out.append((ok, f"{verdict:<9} {text:<30} -> {got:<34} {note}"))
    return out


def check_search() -> list[tuple[bool, str]]:
    out = []
    for g in SEARCH_GOLDENS:
        name, kwargs = g["name"], g["kwargs"]

        if "expect_raises" in g:
            try:
                search_listings(**kwargs)
                out.append((False, f"{name:<32} did NOT raise {g['expect_raises'].__name__}"))
            except g["expect_raises"]:
                out.append((True, f"{name:<32} correctly refused"))
            continue

        rows = search_listings(**kwargs)
        ids = {r["id"] for r in rows}

        if "expect_ids" in g:
            ok = ids == g["expect_ids"]
            detail = f"got {sorted(ids) or '[]'}"
            if not ok:
                detail += f"  expected {sorted(g['expect_ids']) or '[]'}"
        else:
            ok = len(rows) >= g.get("expect_min_count", 0)
            areas = {r["area"] for r in rows}
            if "expect_areas_include" in g:
                ok = ok and g["expect_areas_include"] <= areas
            detail = f"{len(rows)} rows across {sorted(areas)}"

        out.append((ok, f"{name:<32} {detail}"))
    return out


def main() -> int:
    failed = 0
    for title, results in [("L1  location resolution", check_locations()),
                           ("L2  listing search", check_search())]:
        print(f"\n=== {title} " + "=" * (58 - len(title)))
        for ok, line in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {line}")
            failed += not ok
    total = len(LOCATION_GOLDENS) + len(SEARCH_GOLDENS)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


# --- pytest entry points ---------------------------------------------------

def test_locations():
    bad = [line for ok, line in check_locations() if not ok]
    assert not bad, "\n".join(bad)


def test_search():
    bad = [line for ok, line in check_search() if not ok]
    assert not bad, "\n".join(bad)


if __name__ == "__main__":
    sys.exit(main())
