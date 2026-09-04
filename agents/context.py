"""Who the current conversation is with.

A tool that sends photos needs the recipient's phone number, but that must NOT
be a tool argument — the model would then be free to invent or alter it, and
could be talked into sending media to a different number.

So the transport sets it out-of-band and the tool reads it. A ContextVar keeps
it correct under concurrent webhook requests, where a module global would not.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_recipient: ContextVar[str | None] = ContextVar("wa_recipient", default=None)


def current_recipient() -> str | None:
    return _recipient.get()


@contextmanager
def recipient(phone: str):
    """Bind the recipient for one agent turn."""
    token = _recipient.set(phone)
    try:
        yield
    finally:
        _recipient.reset(token)
