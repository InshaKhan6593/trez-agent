"""WhatsApp Cloud API message models.

Typed with Pydantic so a message that violates Meta's limits fails HERE, in
our code, instead of returning an opaque 400 from Graph API in production.

Limits below are from the Cloud API reference (v25/v26) and are hard:

  interactive list   10 sections max, and 10 rows TOTAL across all sections
  row.title          24 chars      <- the binding constraint for us
  row.description    72 chars
  row.id             200 chars
  header.text        60 chars
  footer.text        60 chars
  action.button      20 chars
  body.text          1024 chars for interactive (4096 for plain text)

Formatting: WhatsApp is NOT markdown. It supports *bold*, _italic_,
~strike~, ```mono```, "- " bullets, "1. " numbers and "> " quotes.
It does NOT support tables, **double asterisks**, # headers or --- rules.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

_UNSUPPORTED = (
    (re.compile(r"^\s*\|.*\|\s*$", re.M), ""),      # markdown tables
    (re.compile(r"^\s*[-–—]{3,}\s*$", re.M), ""),   # horizontal rules
    (re.compile(r"^#{1,6}\s*", re.M), ""),          # headers
)


def to_whatsapp_text(text: str) -> str:
    """Strip what WhatsApp cannot render. **bold** -> *bold*."""
    for rx, repl in _UNSUPPORTED:
        text = rx.sub(repl, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)   # paired first
    text = text.replace("**", "*")                   # any strays
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Safety net. The prompt tells the model never to print internal ids, but a
# leaked "**ID: 50719041**" makes the bot read like a debug console.
# Observed in production 2026-09-04.
#
# Only ids we KNOW came from tool output are removed. An earlier version
# stripped any bare 8-digit number and ate a real price ("Rs 10750000 ka
# ghar" -> "Rs ka ghar"), so blanket digit matching is not safe here.


def strip_internal_ids(text: str, known_ids: set[int] | None = None) -> str:
    """Remove listing ids from customer-facing prose, with their labels."""
    if not known_ids:
        return text
    ids = "|".join(str(i) for i in known_ids)
    # optional *bold* wrapper, optional "ID:" label, optional brackets,
    # plus a trailing dash/colon the model uses to attach a description
    text = re.sub(
        rf"\**\s*[\(\[]?\s*(?:id|ID|Id)?\s*[:=]?\s*(?:{ids})\s*[\)\]]?\s*\**"
        rf"\s*[-–—:]?\s*",
        " ",
        text,
    )
    text = re.sub(r"^([ \t]*(?:\d+[.)]|[-*]))\s+", r"\1 ", text, flags=re.M)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate(text: str, limit: int) -> str:
    """Trim to a hard limit on a word boundary where possible."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


# --------------------------------------------------------------------------
# outbound messages
# --------------------------------------------------------------------------

class TextMessage(BaseModel):
    to: str
    body: str = Field(max_length=4096)
    preview_url: bool = False

    @field_validator("body")
    @classmethod
    def _renderable(cls, v: str) -> str:
        return to_whatsapp_text(v)

    def payload(self) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "text",
            "text": {"preview_url": self.preview_url, "body": self.body},
        }


class ImageMessage(BaseModel):
    """One photo, sent either way Meta supports:

    media_id  — we uploaded the bytes; Meta hosts it. Works with LOCAL files,
                so this is what we use (see agents/media.py).
    link      — a PUBLIC https URL that Meta fetches itself. Needs hosting.

    Exactly one must be set.
    """

    to: str
    media_id: str | None = None
    link: str | None = None
    caption: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _one_source(self):
        if bool(self.media_id) == bool(self.link):
            raise ValueError("set exactly one of media_id or link")
        return self

    def payload(self) -> dict:
        image: dict = {"id": self.media_id} if self.media_id else {"link": self.link}
        if self.caption:
            image["caption"] = to_whatsapp_text(self.caption)
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "image",
            "image": image,
        }


class Row(BaseModel):
    id: str = Field(max_length=200)
    title: str = Field(max_length=24)
    description: str | None = Field(default=None, max_length=72)


class Section(BaseModel):
    title: str = Field(max_length=24)
    rows: list[Row] = Field(min_length=1, max_length=10)


class ListMessage(BaseModel):
    """Interactive list — the right shape for "here are 5 properties".

    The customer taps one and the webhook returns row.id, so we get an exact
    listing id back instead of trying to parse "the second one" from free text.
    """

    to: str
    body: str = Field(max_length=1024)
    button: str = Field(max_length=20)
    sections: list[Section] = Field(min_length=1, max_length=10)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)

    @field_validator("sections")
    @classmethod
    def _max_ten_rows_total(cls, v: list[Section]) -> list[Section]:
        total = sum(len(s.rows) for s in v)
        if total > 10:
            raise ValueError(
                f"WhatsApp allows 10 rows TOTAL across all sections, got {total}"
            )
        return v

    def payload(self) -> dict:
        interactive: dict = {
            "type": "list",
            "body": {"text": to_whatsapp_text(self.body)},
            "action": {
                "button": self.button,
                "sections": [s.model_dump(exclude_none=True) for s in self.sections],
            },
        }
        if self.header:
            interactive["header"] = {"type": "text", "text": self.header}
        if self.footer:
            interactive["footer"] = {"text": self.footer}
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "interactive",
            "interactive": interactive,
        }


# --------------------------------------------------------------------------
# rendering listings
# --------------------------------------------------------------------------

def listing_row(listing: dict, fmt_pkr=None) -> Row:
    """One search result as a tappable row.

    24 chars is brutally tight — "3.9 Crore | 4 bed | Askari 6" is 28. So the
    title carries price + beds only, and the area goes in the description.

    Accepts either `price_pkr` (an int, formatted with fmt_pkr) or
    `price_display` (already a string, e.g. straight from tool output).
    """
    if listing.get("price_display"):
        price = listing["price_display"]
    else:
        price = fmt_pkr(listing["price_pkr"])
    price = price.replace("Crore", "Cr").replace("Lakh", "L").strip()
    beds = f"{listing['bedrooms']}bed " if listing.get("bedrooms") else ""
    title = truncate(f"{price} · {beds}{listing['property_type']}", 24)

    bits = [listing.get("area") or ""]
    if listing.get("area_sqyd"):
        bits.append(f"{int(listing['area_sqyd'])} sq.yd")
    if listing.get("features"):
        bits.append(", ".join(listing["features"][:2]).replace("_", " "))
    return Row(
        id=str(listing["id"]),
        title=title,
        description=truncate(" · ".join(b for b in bits if b), 72),
    )


def listings_as_list(to: str, listings: list[dict], fmt_pkr,
                     body: str, header: str | None = None) -> ListMessage:
    """Up to 10 listings as one interactive list."""
    return ListMessage(
        to=to,
        header=header,
        body=body,
        button="Dekhein",                       # "view" — max 20 chars
        sections=[Section(title="Available",
                          rows=[listing_row(x, fmt_pkr) for x in listings[:10]])],
    )


# --------------------------------------------------------------------------
# turning one agent turn into WhatsApp messages
# --------------------------------------------------------------------------

# find_properties returns lines like:
#   id=52369071 | 3.9 Crore | flat | 4 bed | ...
_TOOL_ROW = re.compile(
    r"id=(?P<id>\d+)\s*\|\s*(?P<price>[^|]+)\|\s*(?P<ptype>[^|]+)\|"
    r"\s*(?P<beds>[^|]+)\|\s*(?P<baths>[^|]+)\|\s*(?P<area_sqyd>[^|]+)\|"
    r"\s*(?P<area>[^|]+)\|\s*(?P<features>[^|]*)\|\s*(?P<photos>\d+)"
)


def _parse_tool_rows(tool_outputs: str) -> list[dict]:
    """Recover structured listings from what the tool actually returned.

    Deliberately parsed from TOOL OUTPUT, never from the model's prose. The
    model writes the language; the data comes from the database. That keeps
    invented listings structurally impossible in the interactive list.
    """
    out = []
    for m in _TOOL_ROW.finditer(tool_outputs or ""):
        d = m.groupdict()
        beds = re.search(r"\d+", d["beds"])
        sqyd = re.search(r"[\d.]+", d["area_sqyd"])
        out.append({
            "id": int(d["id"]),
            "price_display": d["price"].strip(),
            "property_type": d["ptype"].strip(),
            "bedrooms": int(beds.group()) if beds else None,
            "area_sqyd": float(sqyd.group()) if sqyd else None,
            "area": d["area"].strip(),
            "features": [f.strip().replace(" ", "_")
                         for f in d["features"].split(",") if f.strip()],
            "photos": int(d["photos"]),
        })
    return out


def build_reply(to: str, reply_text: str, tool_outputs: str,
                fmt_pkr, header: str | None = None) -> list[BaseModel]:
    """One agent turn -> the WhatsApp messages to actually send.

    If the turn produced listings, send an interactive list so the customer can
    TAP one (the webhook then hands us the exact listing id). Otherwise send
    plain text. Either way the prose is stripped of markdown WhatsApp cannot
    render.
    """
    listings = _parse_tool_rows(tool_outputs)
    # strip only ids the tools actually returned — never blanket digit matching
    body = strip_internal_ids(to_whatsapp_text(reply_text),
                              {x["id"] for x in listings})

    if not listings:
        return [TextMessage(to=to, body=body)]

    # An interactive body is capped at 1024; keep the prose short and let the
    # rows carry the detail.
    return [
        listings_as_list(to=to, listings=listings, fmt_pkr=fmt_pkr,
                         body=truncate(body, 1024), header=header)
    ]
