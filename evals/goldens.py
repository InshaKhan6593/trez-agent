"""Evaluation goldens for the Trez agent.

Every expected value here was verified against the live database on 2026-09-04.
When inventory changes, the ID-level expectations change too — that is why each
golden records WHY it expects what it does, so you can re-verify rather than
guess.

Three layers, deliberately separate:

  L1  location   -> resolve_location() only. No LLM. Free. Exact id match.
  L2  search     -> search_listings() only. No LLM. Free. Exact id-set match.
  L3  agent      -> full agent. Costs a run. Checks tool choice + args + reply.

Run L1/L2 on every commit. Run L3 before you change the prompt or the model.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# L1 — location resolution. Pure function, no LLM, no cost.
# ---------------------------------------------------------------------------

LOCATION_GOLDENS = [
    # (input, expected_location_id | None, expected_verdict, note)
    ("ask 6 me ghar chahiye",        21109, "ACCEPT",   "abbreviation appears in a real listing title"),
    ("askari six",                   21109, "ACCEPT",   "spelled-out number"),
    ("malir cantt",                   6654, "ACCEPT",   "Cantonment->cantt is not a prefix; abbr dictionary"),
    ("jauhar me flat chahiye",         232, "ACCEPT",   "word-drop from Gulistan-e-Jauhar"),
    ("koi flat hai defence me",        213, "ACCEPT",   "word-drop from DHA Defence"),
    ("dha phase 8 ka flat",           1485, "ACCEPT",   "must beat the parent DHA Defence on specificity"),
    ("3 bed apartment sector j",     17289, "ACCEPT",   "bare tail of 'Askari 5 - Sector J'"),
    ("karsaz me ghar",               11579, "ACCEPT",   "auto-onboarded 2026-09-03; nobody typed this alias"),
    ("falcon complex",                9582, "ACCEPT",   "partial name"),
    ("askri 5 me house",              6655, "ASK",      "typo -> 0.56, below accept threshold. ASK is correct"),
    ("house near garden east",        None, "NO_MATCH", "real Karachi area, zero inventory. Must not fake it"),
    ("2 bed in bahria town",          None, "NO_MATCH", "generic-word guard: must NOT match 'Gadap Town'"),
]

# ---------------------------------------------------------------------------
# L2 — search. Pure function, no LLM, no cost. Exact set equality.
# ---------------------------------------------------------------------------

SEARCH_GOLDENS = [
    {
        "name": "askari6_4bed_flat_under_4cr",
        "kwargs": dict(purpose="sale", property_type="flat", location_ids=[21109],
                       min_beds=4, max_price=40_000_000),
        "expect_ids": {52369071, 52755299},
        "note": "the canonical happy path",
    },
    {
        "name": "cheapest_house_for_sale",
        "kwargs": dict(purpose="sale", property_type="house", sort="price_asc", limit=1),
        "expect_ids": {50815130},
        "note": "7 Crore. If 53934091 (1.35 Lakh/mo RENTAL) appears, purpose leaked",
    },
    {
        "name": "all_rentals",
        "kwargs": dict(purpose="rent"),
        "expect_ids": {53934205, 53934091},
        "note": "90,000/mo flat proves the 'Thousand' parser fix survived ingest",
    },
    {
        "name": "no_house_under_1cr",
        "kwargs": dict(purpose="sale", property_type="house", max_price=10_000_000),
        "expect_ids": set(),
        "note": "empty is the CORRECT answer. Agent must say so, not substitute flats",
    },
    {
        "name": "parent_expands_to_sectors",
        "kwargs": dict(purpose="sale", property_type="flat", location_ids=[6655], limit=10),
        "expect_min_count": 6,
        "expect_areas_include": {"Askari 5 - Sector J"},
        "note": "ltree containment: Askari 5 must reach its sectors unnamed",
    },
    {
        "name": "west_open_and_park_facing",
        "kwargs": dict(purpose="sale", features=["west_open", "park_facing"]),
        "expect_ids": {52369071, 52775368},
        "note": "features are ANDed, not ORed",
    },
    {
        "name": "purpose_is_mandatory",
        "kwargs": dict(purpose="buy"),
        "expect_raises": ValueError,
        "note": "must refuse rather than guess. 'buy' is not a purpose",
    },
]

# ---------------------------------------------------------------------------
# L3 — the agent. Costs one run each. Checks tool choice, args, and honesty.
#
#   must_call        tools that MUST appear in the run
#   must_not_call    tools that must NOT appear
#   args_contain     key/value pairs that must appear in some tool call
#   reply_must_not   substrings that must not appear in the final reply
# ---------------------------------------------------------------------------

AGENT_GOLDENS = [
    {
        "id": "A1_happy_path",
        "input": "ask 6 me 4 bed flat chahiye, budget 4 crore tak",
        "must_call": ["find_location", "find_properties"],
        "args_contain": {"purpose": "sale", "location_id": 21109,
                         "max_price": 40_000_000, "min_beds": 4},
        "expect_ids": {52369071, 52755299},
        "note": "full chain: resolve -> search. 4 crore must become 40000000",
    },
    {
        "id": "A2_rental_trap",
        "input": "sabse sasta ghar dikhao",
        "must_not_mention_ids": {53934091, 53934205},
        "note": "the 518x trap. Cheapest SALE house is 7 Cr; rentals must not surface",
    },
    {
        "id": "A3_no_coverage",
        "input": "garden east me ghar chahiye",
        "must_not_call": ["find_properties"],
        "reply_must_mention_no_coverage": True,
        "note": "must admit it, then offer real areas",
    },
    {
        "id": "A4_area_hallucination",
        "input": "ghar chahiye",
        # NOT "must_call inventory_summary" — asking a qualifying question
        # without naming any area is a valid (better) answer. The requirement
        # is that IF it names areas, they are real ones.
        "reply_must_not": ["Clifton", "Saddar", "North Nazimabad", "PECHS",
                           "Gulberg", "Bahria", "Korangi", "Nazimabad"],
        "note": "OBSERVED FAILURE 2026-09-04: on a vague query qwen offered "
                "'DHA, Clifton, Gulshan, Malir, Saddar'. Clifton and Saddar are "
                "NOT in inventory. It must call inventory_summary and offer only "
                "the 8 real areas.",
    },
    {
        "id": "A5_seller_is_not_a_search",
        "input": "mera flat bechna hai gulshan me",
        "must_not_call": ["find_properties"],
        "note": "possessive 'mera' + 'bechna hai' = supply side, never a search",
    },
    {
        "id": "A6_landlord_vs_tenant",
        "input": "mera flat kiraye pe dena hai",
        "must_not_call": ["find_properties"],
        "note": "'dena' (give) = landlord. 'lena/chahiye' (take) = tenant. Opposite meanings",
    },
    {
        "id": "A7_tenant_side",
        "input": "kiraye pe flat chahiye",
        "must_call": ["find_properties"],
        "args_contain": {"purpose": "rent"},
        "note": "same word 'kiraya', opposite side from A6",
    },
    {
        "id": "A8_purpose_unknown",
        "input": "askari 6 me kya hai?",
        "note": "purpose not stated. Should ASK buy-vs-rent, or at minimum never "
                "present rent and sale prices as comparable",
    },
    {
        "id": "A9_lakh_parsing",
        "input": "50 lakh tak ka plot dikhao",
        "args_contain": {"purpose": "sale", "max_price": 5_000_000},
        "note": "50 lakh = 5000000. Both plots at 85 Lakh are ABOVE this -> empty",
    },
    {
        "id": "A10_impossible_budget",
        "input": "1 crore me ghar chahiye",
        "must_not_mention_ids": {53934091},
        "note": "zero houses under 1 Cr. Must say so, must not silently offer a flat "
                "or the rental without flagging the switch",
    },
    {
        "id": "A11_whatsapp_format",
        "input": "askari 6 me flat dikhao",
        "reply_must_not_match": [r"^\s*\|.*\|", r"\*\*", r"^#{1,6}\s", r"^\s*-{3,}\s*$"],
        "note": "WhatsApp renders none of: tables, **bold**, # headers, --- rules",
    },
    {
        "id": "A12_no_invented_ids",
        "input": "zamzama me ghar hai?",
        "note": "every id=NNNN in the reply must have appeared in a tool result. "
                "Zamzama has exactly 1 house at 48 Crore",
    },
    {
        "id": "A13_multi_turn_context",
        "input": ["askari 6 me ghar chahiye", "aur us se sasta?"],
        "note": "turn 2 has no area. Must still mean 'cheaper than Askari 6'. "
                "There are ZERO houses cheaper -> must say so, not silently show flats",
    },
]
