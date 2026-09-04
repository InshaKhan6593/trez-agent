"""FastAPI webhook for WhatsApp Cloud API.

    uv run uvicorn app.webhook:app --host 0.0.0.0 --port 8000 --reload

Meta requires a reply within seconds or it RETRIES the delivery — which would
make the customer receive duplicate answers. The agent takes 5-15s with tool
calls, so we acknowledge with 200 immediately and answer in a background task.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Query, Request, Response
from langgraph.checkpoint.memory import InMemorySaver

from agents.db import fmt_pkr
from agents.graph import build_agent
from agents.context import recipient
from agents.turn import turn_tool_output
from agents.whatsapp import TextMessage, build_reply
from agents.whatsapp_client import mark_read, send, send_typing
from agents.whatsapp_inbound import InboundMessage, parse_webhook

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")   # optional but recommended

app = FastAPI(title="Trez WhatsApp agent")

# One checkpointer for the process, so a phone number keeps its history across
# messages. Swap for a Postgres checkpointer before this survives a restart.
_CHECKPOINTER = InMemorySaver()
_AGENT = build_agent(_CHECKPOINTER)

# Log what this process actually loaded. uvicorn --reload silently missed an
# edit on 2026-09-04 and the agent kept saying "main photos nahi bhej sakta"
# because send_photos was not in the running process — which looked like a
# model refusal rather than a stale server.
from agents.tools import TOOLS as _TOOLS          # noqa: E402
log.info("agent loaded with %d tools: %s",
         len(_TOOLS), ", ".join(t.name for t in _TOOLS))


# --------------------------------------------------------------------------
# verification handshake — Meta calls this once when you save the webhook
# --------------------------------------------------------------------------

@app.get("/webhook")
def verify(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("webhook verified by Meta")
        return Response(content=challenge, media_type="text/plain")
    log.warning("webhook verification FAILED (token mismatch)")
    return Response(status_code=403, content="verification failed")


# --------------------------------------------------------------------------
# inbound messages
# --------------------------------------------------------------------------

def _signature_ok(raw: bytes, header: str | None) -> bool:
    """Without this, anyone who learns your URL can POST fake customers.
    Skipped only when WHATSAPP_APP_SECRET is unset (local dev)."""
    if not APP_SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def handle(msg: InboundMessage) -> None:
    """Runs in the background, after Meta already got its 200."""
    try:
        mark_read(msg.message_id)

        fallback = msg.fallback_reply
        if fallback:                              # voice note, image, location
            log.info("%s: %s -> canned fallback", msg.from_, msg.kind)
            send(TextMessage(to=msg.from_, body=fallback).payload())
            return

        text = msg.agent_input
        if not text:
            return

        send_typing(msg.message_id)
        # thread_id = phone number, so each customer has their own history
        # bind the recipient so send_photos knows who to send to, without
        # exposing the phone number as a tool argument the model could alter
        with recipient(msg.from_):
            state = _AGENT.invoke(
                {"messages": [{"role": "user", "content": text}]},
                {"configurable": {"thread_id": msg.from_}},
            )

        reply = state["messages"][-1].content or ""
        # THIS turn only. state["messages"] is the full thread history, so
        # scanning all of it re-sent every previous search to the customer.
        tool_outputs = turn_tool_output(state["messages"])
        for out in build_reply(msg.from_, reply, tool_outputs, fmt_pkr):
            send(out.payload())
        log.info("%s: replied (%d chars, %d tool msgs)",
                 msg.from_, len(reply), tool_outputs.count("\n"))

        # NOTE: photos are NOT auto-sent when a row is tapped.
        # An earlier version did, which meant the customer received images
        # without being asked — the opposite of the intended behaviour.
        # Tapping now just tells the agent which listing they mean; the agent
        # describes it and OFFERS ("Tasveerein bhejun?"), and only calls
        # send_photos once the customer agrees. One path, always consented.

    except Exception:
        log.exception("handler failed for %s", msg.from_)
        try:
            send(TextMessage(
                to=msg.from_,
                body="Maaf kijiye, abhi kuch masla ho gaya hai. "
                     "Baraye meherbani thori dair baad dobara koshish karein.",
            ).payload())
        except Exception:
            log.exception("could not even send the error reply")


@app.post("/webhook")
async def inbound(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not _signature_ok(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("bad signature — rejecting")
        return Response(status_code=403)

    payload = await request.json()
    messages = parse_webhook(payload)      # [] for delivered/read callbacks
    for m in messages:
        log.info("inbound %s from %s", m.kind, m.from_)
        background.add_task(handle, m)

    # 200 immediately — anything slower and Meta retries, double-replying.
    return {"status": "ok", "queued": len(messages)}


@app.get("/health")
def health():
    return {"ok": True, "agent": "trez"}
