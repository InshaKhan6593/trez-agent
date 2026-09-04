"""Inbound WhatsApp webhook payloads, typed.

Meta posts a deeply nested envelope. Parsing it with .get() chains is how you
get 3am KeyErrors, so it is modelled here and unwrapped once.

Message types a property customer actually sends:
    text          the normal case
    audio         voice notes — VERY common in Karachi. See VOICE_FALLBACK.
    interactive   they tapped a row in a list we sent -> exact listing id
    image         a photo of a property they own (sellers do this)
    location      a pin
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Sending a voice note back an unhelpful error loses the lead. Until we wire
# transcription, answer honestly in Roman Urdu and keep the conversation alive.
VOICE_FALLBACK = (
    "Abhi main voice note nahi sun sakta 🙏\n\n"
    "Baraye meherbani *type* karke bata dein — kaunsa area, "
    "ghar chahiye ya flat, aur budget kitna hai?"
)

UNSUPPORTED_FALLBACK = (
    "Yeh message main abhi parh nahi sakta 🙏\n\n"
    "Baraye meherbani *type* karke bata dein ke aapko kya chahiye."
)


class InboundMessage(BaseModel):
    """One customer message, already unwrapped."""

    from_: str
    message_id: str
    kind: Literal["text", "audio", "interactive", "image", "location", "other"]
    text: str | None = None
    media_id: str | None = None          # audio/image — fetch via GET /{media_id}
    selected_id: str | None = None       # interactive -> the row id we sent

    @property
    def is_actionable_text(self) -> bool:
        """Can this go straight to the agent as text?"""
        return self.kind in ("text", "interactive") and bool(self.agent_input)

    @property
    def agent_input(self) -> str | None:
        if self.kind == "text":
            return self.text
        if self.kind == "interactive" and self.selected_id:
            # A tapped row is an exact listing id — far better than parsing
            # "the second one" out of free text.
            return f"Customer selected listing id={self.selected_id}"
        return None

    @property
    def fallback_reply(self) -> str | None:
        """Canned reply for message types we cannot process yet."""
        if self.kind == "audio":
            return VOICE_FALLBACK
        if self.kind in ("image", "location", "other"):
            return UNSUPPORTED_FALLBACK
        return None


def parse_webhook(payload: dict) -> list[InboundMessage]:
    """Unwrap Meta's envelope: entry[] > changes[] > value.messages[].

    Returns [] for status callbacks (delivered/read), which arrive on the same
    webhook and must be ignored rather than treated as customer messages.
    """
    out: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for m in (change.get("value") or {}).get("messages") or []:
                mtype = m.get("type")
                kind = mtype if mtype in (
                    "text", "audio", "interactive", "image", "location"
                ) else "other"

                selected = None
                if kind == "interactive":
                    inter = m.get("interactive") or {}
                    selected = (
                        (inter.get("list_reply") or {}).get("id")
                        or (inter.get("button_reply") or {}).get("id")
                    )

                out.append(InboundMessage(
                    from_=m.get("from", ""),
                    message_id=m.get("id", ""),
                    kind=kind,
                    text=(m.get("text") or {}).get("body"),
                    media_id=(m.get(mtype) or {}).get("id") if kind in ("audio", "image") else None,
                    selected_id=selected,
                ))
    return out
