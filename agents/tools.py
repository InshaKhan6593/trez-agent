"""LangChain tools — thin wrappers over agents.db.

The model never writes SQL. It fills these typed signatures; our code compiles
them to parameterised queries. That removes the whole NL2SQL failure class,
where a model invents a filter value and gets zero rows with no error.

The docstrings are not comments — the model reads them to choose a tool. Vague
docstring, wrong tool choice.
"""

from __future__ import annotations

from langchain.tools import tool

from .context import current_recipient
from .media import listing_photos, send_listing_photos
from .db import (
    decide,
    substring_locations,
    fmt_pkr,
    inventory_overview,
    resolve_location,
    search_listings,
)


@tool
def inventory_summary() -> str:
    """Which AREAS we cover and roughly what they cost. Nothing more.

    USE ONLY when the customer gave no area AND no budget — e.g. "kya hai?",
    "ghar chahiye". Also use it to offer alternatives after a search found
    nothing.

    DO NOT USE to answer a budget question. This returns price RANGES for a
    whole area. A range that spans the customer's number does NOT mean a
    property exists at that price — e.g. an area shown as "85 Lakh - 85 Lakh"
    has NOTHING under 50 Lakh. If the customer named any price, you MUST call
    find_properties instead. It is the only tool that knows individual prices.
    """
    rows = inventory_overview()
    if not rows:
        return "No active inventory."
    body = "\n".join(
        f"{r['area']} — {r['n']} {r['property_type']}(s) for {r['purpose']}, "
        f"{fmt_pkr(r['lo'])} to {fmt_pkr(r['hi'])}"
        for r in rows
    )
    return (
        body
        + "\n\n[These are AREA RANGES, not individual properties. Do not tell "
          "the customer something is available at their budget based on this. "
          "Call find_properties to check.]"
    )


@tool
def find_location(text: str) -> str:
    """Resolve an area name the customer typed (e.g. 'ask 6', 'malir cantt',
    'jauhar', 'karsaz') into a location_id.

    ALWAYS call this before searching by area. Never invent a location_id.
    If the result is ambiguous, ask the customer which area they mean."""
    cands = resolve_location(text)
    if not cands:
        # Trigram missed. Does the word appear INSIDE any real area name?
        # "town" -> Gadap Town, Gulshan-e-Iqbal Town. Offer, never assume.
        near = substring_locations(text)
        if near:
            lines = [f"{c['name']} (location_id={c['location_id']}, "
                     f"{c['stock']} listings)" for c in near]
            return (
                f"'{text}' is not an area by itself, but these real areas contain "
                "that word. ASK the customer which one they mean — do not pick "
                "for them, and do not treat their word as an area name:\n"
                + "\n".join(lines)
            )
        return (
            f"No location matching '{text}'. We do not cover that area. "
            "Do NOT repeat their word back as if it were a place name. "
            "Call inventory_summary to tell the customer what we DO have."
        )
    lines = [
        f"{c['name']} (location_id={c['location_id']}, {c['stock']} listings)"
        for c in cands
    ]
    if decide(cands) == "ACCEPT":
        return f"Resolved to {lines[0]}"
    return "Ambiguous — ask the customer which they mean:\n" + "\n".join(lines)


@tool
def find_properties(
    purpose: str,
    property_type: str | None = None,
    location_id: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_beds: int | None = None,
    features: list[str] | None = None,
    limit: int = 5,
) -> str:
    """Search Trez Enterprises property inventory.

    purpose: REQUIRED — 'sale' or 'rent'. Never guess; ask the customer if
        unclear. Rent prices are per month, sale prices are total.
    property_type: 'house', 'flat' or 'plot'.
    location_id: from find_location. A parent area automatically includes all
        its sub-areas. Never invent this number.
    max_price: the customer's BUDGET, in PKR. "budget 4 crore", "4 crore tak",
        "under 4 crore" all mean max_price=40000000.
    min_price: ONLY when they state a floor ("5 crore se upar"). Leave it None
        otherwise. NEVER set min_price equal to max_price — that searches for
        an exact price and finds nothing.
        1 crore = 10000000, 1 lakh = 100000.
    features: any of west_open, east_open, corner, park_facing, sea_view,
        boulevard_facing, furnished, on_installments, ready_to_move,
        brigadier_house, ground_floor, brand_new, servant_quarter,
        near_mosque, swimming_pool, gym, parking.
    """
    # Observed 2026-09-04: for "budget 4 crore" the model set BOTH bounds to
    # 40000000, searching for an exact price and missing two 3.9 Crore flats.
    # A budget is a ceiling, so drop the redundant floor and say so.
    note = ""
    if min_price is not None and max_price is not None and min_price >= max_price:
        min_price = None
        note = ("[Note: min_price was dropped — a stated budget is a MAXIMUM. "
                "Do not set both bounds to the same number.]\n")

    try:
        rows = search_listings(
            purpose=purpose,
            property_type=property_type,
            location_ids=[location_id] if location_id else None,
            min_price=min_price,
            max_price=max_price,
            min_beds=min_beds,
            features=features,
            limit=limit,
        )
    except ValueError as e:
        return f"Cannot search: {e}. Ask the customer whether they want to buy or rent."

    if not rows:
        return note + (
            "No matching properties. Tell the customer honestly, and offer to "
            "widen the budget or area rather than inventing options."
        )

    body = "\n".join(
        f"id={r['id']} | {fmt_pkr(r['price_pkr'])} | {r['property_type']} | "
        f"{r['bedrooms'] or '-'} bed | {r['bathrooms'] or '-'} bath | "
        f"{r['area_sqyd'] or '-'} sq.yd | {r['area']} | "
        f"{', '.join(r['features'][:4])} | {r['photos']} photos"
        for r in rows
    )
    return note + (
        "[These rows are ALREADY being shown to the customer as a tappable "
        "list. Do NOT repeat the details in your reply — write ONE short "
        "sentence only, e.g. 'Askari 5 mein ye options hain:'. The 'id=' "
        "numbers are INTERNAL database keys: never write them to the "
        "customer. Refer to a property by price or area instead.]\n"
        + body
    )


@tool
def send_photos(listing_id: int) -> str:
    """Send a property's photos to the customer on WhatsApp.

    WHEN: ONLY after the customer has agreed to receive them. Search results
        end with a photo count — if a property has photos, OFFER first
        ("Tasveerein bhejun?") and call this only once they say yes.
    HOW: listing_id must be an id a search returned in this conversation.
    NEVER: never call this without asking; never call it for a property with
        0 photos; never promise photos before checking the count.

    Returns how many were sent, or an explanation if none could be.
    """
    to = current_recipient()
    if not to:
        return ("Cannot send photos here — this conversation is not on "
                "WhatsApp. Tell the customer to ask again on WhatsApp.")

    photos = listing_photos(listing_id)
    if not photos:
        return (f"Listing {listing_id} has NO photos. Tell the customer "
                "honestly and offer a visit instead. Do not apologise twice.")
    try:
        n = send_listing_photos(to, listing_id)
    except Exception as e:                       # noqa: BLE001 - surfaced to model
        return f"Photo sending failed ({type(e).__name__}). Apologise briefly."
    if n == 0:
        return (f"Could not send photos for {listing_id}. Tell the customer "
                "and offer a visit instead.")
    return (f"Sent {n} photos. They are ALREADY delivered — just confirm "
            "briefly, e.g. 'Tasveerein bhej di hain'. Do not describe them.")


TOOLS = [inventory_summary, find_location, find_properties, send_photos]
