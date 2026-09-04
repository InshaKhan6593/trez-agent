"""Outbound WhatsApp Cloud API client."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

class WhatsAppError(RuntimeError):
    pass


# Read credentials at CALL time, not import time. Reading them at import meant
# a running server kept using a dead token after .env was updated — uvicorn
# --reload watches .py files, not .env. That cost an hour of "webhook is
# unreachable" debugging when the webhook was fine and only the send failed.
def _creds() -> tuple[str, str, str]:
    load_dotenv(override=True)
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not phone_id or not token:
        raise WhatsAppError("WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN missing")
    return phone_id, token, os.getenv("WHATSAPP_API_VERSION", "v22.0")


def send(payload: dict, *, timeout: float = 20.0) -> dict:
    """POST one message payload built by agents.whatsapp models.

    Meta returns 200 with an error body for some failures, so we check the
    body as well as the status code.
    """
    phone_id, token, api_version = _creds()

    r = httpx.post(
        f"https://graph.facebook.com/{api_version}/{phone_id}/messages",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", {})
        raise WhatsAppError(
            f"HTTP {r.status_code} {err.get('code')}/{err.get('error_subcode')}: "
            f"{err.get('message')} — {err.get('error_data', {}).get('details', '')}"
        )
    return body


def upload_media(path: str | Path, mime: str = "image/jpeg") -> str:
    """Upload a LOCAL file to Meta and return its media id.

    This is why no public hosting is needed: we push the bytes rather than
    handing Meta a URL to fetch. Files on this machine work fine.

    The id is reusable for ~30 days, so cache it per asset.
    """
    phone_id, token, api_version = _creds()
    p = Path(path)
    if not p.is_file():
        raise WhatsAppError(f"media file not found: {p}")

    with p.open("rb") as fh:
        r = httpx.post(
            f"https://graph.facebook.com/{api_version}/{phone_id}/media",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (p.name, fh, mime)},
            data={"messaging_product": "whatsapp", "type": mime},
            timeout=60.0,
        )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", {})
        raise WhatsAppError(f"upload failed HTTP {r.status_code} "
                            f"{err.get('code')}: {err.get('message')}")
    return body["id"]


def mark_read(message_id: str) -> None:
    """Blue ticks. Cheap, and makes the bot feel responsive while it thinks."""
    try:
        send({"messaging_product": "whatsapp", "status": "read",
              "message_id": message_id})
    except Exception as e:                      # never let this break a reply
        log.warning("mark_read failed: %s", e)


def send_typing(message_id: str) -> None:
    """Typing indicator — shown until we send the real reply or ~25s pass."""
    try:
        send({"messaging_product": "whatsapp", "status": "read",
              "message_id": message_id,
              "typing_indicator": {"type": "text"}})
    except Exception as e:
        log.warning("send_typing failed: %s", e)
