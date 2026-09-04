"""Sending listing photos from the LOCAL media store.

No public hosting is required. Meta's /media endpoint accepts an upload, keeps
the file, and returns an id we send instead of a URL — so files sitting on this
machine at data/media-store/<assetId>.jpeg are directly usable.

Each photo is uploaded once and its id cached in listing_media. Meta expires
media ids after roughly 30 days, so anything older is re-uploaded.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from .db import DSN
from .whatsapp import ImageMessage
from .whatsapp_client import send, upload_media

load_dotenv()
log = logging.getLogger(__name__)

MEDIA_STORE = Path(os.getenv("MEDIA_STORE", "data/media-store"))

# Meta expires uploaded media ids; refresh well before the ~30 day limit.
MEDIA_TTL = timedelta(days=25)

# WhatsApp has no album/carousel — every photo is a separate message.
# More than a handful and the customer's chat is buried.
MAX_PHOTOS = 4


def listing_photos(listing_id: int, limit: int = MAX_PHOTOS) -> list[dict]:
    """Photos for a listing, with any cached Meta id."""
    sql = """
        SELECT asset_id, file, meta_media_id, meta_uploaded_at
        FROM listing_media
        WHERE listing_id = %s
        ORDER BY seq
        LIMIT %s
    """
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        return [
            {"asset_id": a, "file": f, "media_id": mid, "uploaded_at": ts}
            for a, f, mid, ts in cur.execute(sql, (listing_id, limit)).fetchall()
        ]


def _cache_media_id(listing_id: int, asset_id: str, media_id: str) -> None:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE listing_media
                  SET meta_media_id = %s, meta_uploaded_at = now()
                WHERE listing_id = %s AND asset_id = %s""",
            (media_id, listing_id, asset_id),
        )
        conn.commit()


def ensure_media_id(listing_id: int, photo: dict) -> str | None:
    """Return a usable Meta media id, uploading from local disk if needed."""
    fresh = (
        photo.get("media_id")
        and photo.get("uploaded_at")
        and photo["uploaded_at"] > datetime.now(timezone.utc) - MEDIA_TTL
    )
    if fresh:
        return photo["media_id"]

    path = MEDIA_STORE / photo["file"]
    if not path.is_file():
        log.warning("media file missing on disk: %s", path)
        return None
    try:
        media_id = upload_media(path)
    except Exception:
        log.exception("upload failed for %s", path)
        return None
    _cache_media_id(listing_id, photo["asset_id"], media_id)
    return media_id


def send_listing_photos(to: str, listing_id: int, caption: str | None = None,
                        limit: int = MAX_PHOTOS) -> int:
    """Send up to `limit` photos. Returns how many were actually delivered.

    Returns 0 when a listing has no photos — 3 plots in this catalogue have
    none, so the caller must not promise pictures before checking.
    """
    sent = 0
    for i, photo in enumerate(listing_photos(listing_id, limit)):
        media_id = ensure_media_id(listing_id, photo)
        if not media_id:
            continue
        try:
            # caption on the first photo only, or it repeats on every message
            send(ImageMessage(to=to, media_id=media_id,
                              caption=caption if i == 0 else None).payload())
            sent += 1
        except Exception:
            log.exception("send failed for listing %s photo %s", listing_id, photo["asset_id"])
    return sent
