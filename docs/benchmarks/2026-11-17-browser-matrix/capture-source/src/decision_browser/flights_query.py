"""Immutable historical pilot query; changing it requires a new fixture corpus.

Historical evidence parsing is date-independent. The live entry point refuses an
expired departure before creating a provider or reserving any paid/effect work.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType


@dataclass(frozen=True)
class FlightsQuery:
    origin: str = "JFK"
    destination: str = "LAX"
    departure_date: str = "2026-11-10"
    trip_type: str = "one_way"
    adults: int = 1
    cabin: str = "economy"


FIXED_QUERY = FlightsQuery()
DEPARTURE = date.fromisoformat(FIXED_QUERY.departure_date)
DATE_LABEL = f"{DEPARTURE:%B} {DEPARTURE.day}, {DEPARTURE.year}"
DATE_DONE = f"Done. Search for one-way flights departing on {DATE_LABEL}"
JFK = "John F. Kennedy International Airport (JFK)"
LAX = "Los Angeles International Airport (LAX)"
AIRPORTS = MappingProxyType({
    FIXED_QUERY.origin: ("New York", "John F. Kennedy International Airport"),
    FIXED_QUERY.destination: ("Los Angeles", "Los Angeles International Airport"),
})


def query_is_current() -> bool:
    return DEPARTURE >= datetime.now(UTC).date()
