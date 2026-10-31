"""Market data venues. Each exposes open_events, books and outcomes."""
from __future__ import annotations

from collections import Counter
from typing import Callable, Iterable

from ..core import Event
from ..rules import SkipEvent


def parse_all(raw_events: Iterable[dict], parse: Callable[[dict], Event]) -> tuple[list[Event], Counter]:
    events: list[Event] = []
    skips: Counter = Counter()
    for raw in raw_events:
        try:
            events.append(parse(raw))
        except SkipEvent as skip:
            skips[str(skip).split(":")[0]] += 1
        except Exception:  # one malformed listing can't be allowed to take the rest with it
            skips["parse_error"] += 1
    return events, skips
