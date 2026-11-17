"""The ledger's records (ports ``UsageEvent.swift`` and ``Timestamp.swift``)."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class UsageEvent:
    """One model response. The first five counts are disjoint;
    ``cache_write_1h`` is the part of ``cache_write`` cached for an hour."""

    harness: str
    account: str
    session: str
    cwd: str | None
    model: str
    ts: float
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0
    cost: float | None = None
    tier: str | None = None
    # Droid and Cline report one total per session, not per request. Such
    # an event never crosses a model's long-context threshold: its summed
    # input says nothing about any single request's prompt size
    # (``isSessionTotal``).
    session_total: bool = False

    @property
    def total(self) -> int:
        return (self.input + self.output + self.cache_read
                + self.cache_write + self.reasoning)


@dataclass
class LedgerRecord:
    """An event under its dedup key. ``msg`` is the response's message id,
    which sidechain copies also collide on."""

    key: str
    event: UsageEvent
    msg: str | None = None
    sidechain: bool = False
    # OpenCode: a re-read under an existing key that does not replace its
    # row (the totals tied) still moves the row to this record's model, so
    # a reader that names models differently renames old rows in place.
    relabels_model: bool = False
    # Session-total sources (Droid, Cline) rewrite one file per session, so
    # a re-read under the same key replaces whatever the file said before,
    # even when the totals went down (``authoritative``).
    authoritative: bool = False
    # The account in ``key`` is a real account id more than one profile can
    # read — the Cursor feed when the reply names an ``account_id``. An
    # authoritative write then removes a stale row for the same event
    # under any profile's account home. Without it the account is the
    # profile itself, so only this profile's stale rows match
    # (``accountSpansProfiles``).
    account_spans_profiles: bool = False
    # Two events from one file can share a timestamp, model and session
    # (Grok turns that end in the same second). The lookalike cleanup
    # would delete one of them; these sources never used a counts-bearing
    # key, so they skip it.
    skips_lookalikes: bool = False


# ``JSONValue.maxTokens``: no real response logs more. A larger count is a
# corrupt line; capping it keeps sums inside SQLite's 64-bit integers.
MAX_TOKENS = 1 << 40


def json_int(value) -> int:
    """``JSONValue.int``: a non-negative integer from a JSON number, else 0,
    at most ``MAX_TOKENS``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value <= 0 or value == float("inf"):
        return 0
    return MAX_TOKENS if value >= MAX_TOKENS else int(value)


def json_str(value) -> str | None:
    """``JSONValue.string``: a trimmed string, ``None`` when blank."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def first_present(obj: dict, *keys):
    """Swift's ``a ?? b`` on a dict: the first key present, even as null."""
    for key in keys:
        if key in obj:
            return obj[key]
    return None


_UTC = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$"
)


def parse_timestamp(text) -> float | None:
    """Epoch seconds from an ISO 8601 timestamp, ``None`` when unreadable.

    The common UTC shape is parsed by hand (any number of fraction
    digits); anything else, such as an offset, goes through ``datetime``.
    """
    if not isinstance(text, str):
        return None
    m = _UTC.match(text)
    if m:
        year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
        # timegm rolls an impossible date over silently (2026-02-30 would
        # read as 2 March), so the day is checked against its month.
        if not (1 <= month <= 12 and hour < 24 and minute < 60 and second < 61):
            return None
        if not 1 <= day <= calendar.monthrange(year, month)[1]:
            return None
        fraction = float("0." + m.group(7)) if m.group(7) else 0.0
        try:
            base = calendar.timegm((year, month, day, hour, minute, second))
        except (ValueError, OverflowError):
            return None
        return base + fraction
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()
