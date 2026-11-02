"""Odds data from free public prediction-market APIs (no keys needed).

Sources:
  * Polymarket Gamma API  - win probability for every candidate race, and a
                            market's resolution once the race is decided.
  * Kalshi Trade API      - Democratic House seat distribution (4-seat
                            brackets plus an "at least N" ladder).

fetch_snapshot() pulls everything once and returns a JSON-serialisable dict
that is saved as one row in polling_snapshots. Snapshot wraps a saved row
and derives win probabilities and the seat-count distribution from it.

The parse_* functions are pure so they can be tested on canned JSON.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

POLYMARKET_EVENT_URL = "https://gamma-api.polymarket.com/events/slug/{slug}"
POLYMARKET_PAGE_URL = "https://polymarket.com/event/{slug}"
KALSHI_EVENT_URL = "https://api.elections.kalshi.com/trade-api/v2/events/{ticker}"
KALSHI_PAGE_URL = "https://kalshi.com/markets/{series}"

# Market price at which a race is treated as "called" for result suggestions.
CALLED_THRESHOLD = 0.97

GetJson = Callable[[str], Any]


class PollingError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get_json(url: str) -> Any:
    import httpx

    response = httpx.get(
        url,
        params={"with_nested_markets": "true"} if "kalshi" in url else None,
        headers={"User-Agent": "hk-election-prediction/1.0", "Accept": "application/json"},
        timeout=20,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Polymarket: candidate races
# ---------------------------------------------------------------------------


def _polymarket_yes_price(market: dict) -> float | None:
    prices = market.get("outcomePrices")
    if not prices:
        return None
    if isinstance(prices, str):
        prices = json.loads(prices)
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


def parse_polymarket_winner(event: dict, race: dict) -> dict:
    """Win probabilities for race["options"] from a Polymarket event.

    Prices are normalised over the listed candidates so they sum to 1 (minor
    candidates and the market's overround are dropped).
    """
    match = race["source"]["match"]
    markets = [m for m in event.get("markets", []) if _polymarket_yes_price(m) is not None]
    raw: dict[str, float] = {}
    resolved = None
    for label in race["options"]:
        needles = [n.casefold() for n in match.get(label, [label])]
        hits = [
            m for m in markets
            if any(n in f"{m.get('groupItemTitle', '')} | {m.get('question', '')}".casefold() for n in needles)
        ]
        if not hits:
            raise PollingError(f"No Polymarket market matches {label!r}")
        market = max(hits, key=lambda m: float(m.get("volumeNum") or m.get("volume") or 0))
        raw[label] = _polymarket_yes_price(market)
        if market.get("closed") and raw[label] >= 0.99:
            resolved = label

    total = sum(raw.values())
    if total <= 0:
        raise PollingError("All prices are zero")
    return {
        "kind": "winner",
        "probs": {label: price / total for label, price in raw.items()},
        "raw": raw,
        "resolved": resolved,
        "source": "Polymarket",
        "url": POLYMARKET_PAGE_URL.format(slug=race["source"]["polymarket"]),
        "volume": float(event.get("volume") or 0),
    }


_R_BRACKET = re.compile(r"(?:(below|under)\s*(\d+))|(?:(\d+)\s*-\s*(\d+))|(?:(\d+)\s*\+)", re.I)


def parse_polymarket_r_brackets(event: dict, house_size: int) -> list[tuple[int | None, int | None, float]]:
    """Republican seat brackets -> Democratic seat brackets (D = size - R)."""
    brackets = []
    for m in event.get("markets", []):
        price = _polymarket_yes_price(m)
        found = _R_BRACKET.search(m.get("groupItemTitle") or "")
        if price is None or not found:
            continue
        below, b_hi, lo, hi, plus = found.groups()
        if below:
            r_lo, r_hi = None, int(b_hi) - 1
        elif plus:
            r_lo, r_hi = int(plus), None
        else:
            r_lo, r_hi = int(lo), int(hi)
        d_lo = None if r_hi is None else house_size - r_hi
        d_hi = None if r_lo is None else house_size - r_lo
        brackets.append((d_lo, d_hi, price))
    return brackets


# ---------------------------------------------------------------------------
# Kalshi: House seat distribution
# ---------------------------------------------------------------------------


def _kalshi_price(market: dict) -> float | None:
    result = (market.get("result") or "").lower()
    if result in ("yes", "no"):
        return 1.0 if result == "yes" else 0.0

    def num(key):
        try:
            value = market.get(key)
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    bid, ask = num("yes_bid_dollars"), num("yes_ask_dollars")
    if bid is not None and ask is not None and 0 < bid <= ask:
        return (bid + ask) / 2
    return num("last_price_dollars")


def _kalshi_markets(payload: dict) -> list[dict]:
    event = payload.get("event") or {}
    return event.get("markets") or payload.get("markets") or []


def parse_kalshi_brackets(payload: dict) -> list[tuple[int | None, int | None, float]]:
    """Mutually exclusive seat brackets as (lo, hi, prob), bounds inclusive."""
    brackets = []
    for m in _kalshi_markets(payload):
        price = _kalshi_price(m)
        kind, floor, cap = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
        if price is None:
            continue
        if kind == "less" and cap is not None:
            brackets.append((None, int(cap) - 1, price))
        elif kind == "between" and floor is not None and cap is not None:
            brackets.append((int(floor), int(cap), price))
        elif kind == "greater" and floor is not None:
            brackets.append((int(floor) + 1, None, price))
        elif kind == "greater_or_equal" and floor is not None:
            brackets.append((int(floor), None, price))
    return brackets


def parse_kalshi_ladder(payload: dict) -> dict[int, float]:
    """"At least N" markets as {N: P(X >= N)}."""
    ladder = {}
    for m in _kalshi_markets(payload):
        price = _kalshi_price(m)
        if price is None or m.get("floor_strike") is None:
            continue
        if m.get("strike_type") == "greater_or_equal":
            ladder[int(m["floor_strike"])] = price
        elif m.get("strike_type") == "greater":
            ladder[int(m["floor_strike"]) + 1] = price
    return ladder


# ---------------------------------------------------------------------------
# Seat distribution maths
# ---------------------------------------------------------------------------


def survival_from_brackets(brackets) -> list[list[float]]:
    """[(lo, hi, p)] -> [[seat, P(X >= seat)], ...] at each bracket's lower bound."""
    total = sum(p for _, _, p in brackets)
    if total <= 0:
        raise PollingError("Seat brackets have no probability")
    lows = sorted({lo for lo, _, _ in brackets if lo is not None})
    return [
        [lo, sum(p for b_lo, _, p in brackets if b_lo is not None and b_lo >= lo) / total]
        for lo in lows
    ]


def extend_with_ladder(points: list[list[float]], ladder: dict[int, float]) -> list[list[float]]:
    """Use an "at least N" ladder to shape the tail above the last bracket.

    The ladder is used for its *ratios* only, anchored at the top bracket's
    threshold, so the brackets (usually the deeper market) set the level.
    """
    if not points or not ladder:
        return points
    anchor_seat, anchor_s = points[-1]
    base = ladder.get(int(anchor_seat))
    if not base or base <= 0:
        return points
    extra = [[seat, anchor_s * min(1.0, p / base)] for seat, p in sorted(ladder.items()) if seat > anchor_seat]
    return points + extra


def seat_pmf(points: list[list[float]], lo: int = 0, hi: int = 435) -> list[float]:
    """Probability of each seat count lo..hi from survival points.

    Between known points the mass is spread evenly (a linear CDF). Beyond the
    outermost points it decays geometrically, starting at the density of the
    neighbouring interval, so open-ended brackets get a plausible tail.
    """
    pts = sorted((int(s), min(1.0, max(0.0, float(p)))) for s, p in points if lo < s <= hi)
    # Enforce a non-increasing survival curve.
    clean: list[tuple[int, float]] = []
    for seat, s in pts:
        if clean and seat == clean[-1][0]:
            continue
        clean.append((seat, min(s, clean[-1][1]) if clean else s))
    if not clean:
        raise PollingError("No usable seat data")

    n = hi - lo + 1
    pmf = [0.0] * n

    def put(seat, mass):
        pmf[min(max(seat, lo), hi) - lo] += mass

    for (s1, p1), (s2, p2) in zip(clean, clean[1:]):
        width = s2 - s1
        for seat in range(s1, s2):
            put(seat, (p1 - p2) / width)

    def tail(start, step, mass, density):
        if mass <= 0:
            return
        density = density if density > 0 else mass / 10
        d0 = min(density, mass)
        ratio = 1 - d0 / mass
        seat, remaining, current = start, mass, d0
        while lo <= seat <= hi and remaining > 1e-12:
            take = min(current, remaining)
            put(seat, take)
            remaining -= take
            current *= ratio
            seat += step
        if remaining > 0:
            put(seat - step, remaining)

    first_density = (clean[0][1] - clean[1][1]) / (clean[1][0] - clean[0][0]) if len(clean) > 1 else 0
    last_density = (clean[-2][1] - clean[-1][1]) / (clean[-1][0] - clean[-2][0]) if len(clean) > 1 else 0
    tail(clean[0][0] - 1, -1, 1 - clean[0][1], first_density)
    tail(clean[-1][0], +1, clean[-1][1], last_density)

    total = sum(pmf)
    return [p / total for p in pmf]


def pmf_quantile(pmf: list[float], q: float, lo: int = 0) -> int:
    acc = 0.0
    for i, p in enumerate(pmf):
        acc += p
        if acc >= q:
            return lo + i
    return lo + len(pmf) - 1


def pmf_mean(pmf: list[float], lo: int = 0) -> float:
    return sum((lo + i) * p for i, p in enumerate(pmf))


def prob_within(pmf: list[float], guess: int, k: int, lo: int = 0) -> float:
    """P(|X - guess| <= k)."""
    start = max(0, guess - k - lo)
    end = min(len(pmf), guess + k - lo + 1)
    return sum(pmf[start:end]) if start < end else 0.0


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _fetch_winner(race: dict, get_json: GetJson) -> dict:
    slug = race["source"]["polymarket"]
    return parse_polymarket_winner(get_json(POLYMARKET_EVENT_URL.format(slug=slug)), race)


def _fetch_seats(race: dict, get_json: GetJson) -> dict:
    src = race["source"]
    size = int(src.get("house_size", race["max_value"]))
    notes = []
    try:
        brackets = parse_kalshi_brackets(get_json(KALSHI_EVENT_URL.format(ticker=src["kalshi_brackets"])))
        points = survival_from_brackets(brackets)
        source, url = "Kalshi", KALSHI_PAGE_URL.format(series=src["kalshi_brackets"].split("-")[0].lower())
        if src.get("kalshi_ladder"):
            try:
                ladder = parse_kalshi_ladder(get_json(KALSHI_EVENT_URL.format(ticker=src["kalshi_ladder"])))
                points = extend_with_ladder(points, ladder)
            except Exception as exc:  # tail refinement is optional
                notes.append(f"Kalshi ladder unavailable: {exc}")
    except Exception as exc:
        if not src.get("polymarket_r_brackets"):
            raise
        notes.append(f"Kalshi unavailable, used Polymarket: {exc}")
        slug = src["polymarket_r_brackets"]
        points = survival_from_brackets(
            parse_polymarket_r_brackets(get_json(POLYMARKET_EVENT_URL.format(slug=slug)), size)
        )
        source, url = "Polymarket", POLYMARKET_PAGE_URL.format(slug=slug)
    seat_pmf(points, race["min_value"], race["max_value"])  # validate
    return {"kind": "number", "survival": points, "source": source, "url": url, "notes": notes}


def fetch_snapshot(races: list[dict], get_json: GetJson = http_get_json) -> dict:
    """Fetch odds for every race. Failures are recorded per race, not raised."""

    def one(race):
        try:
            if race["race_type"] == "winner":
                return race["id"], _fetch_winner(race, get_json), None
            return race["id"], _fetch_seats(race, get_json), None
        except Exception as exc:
            return race["id"], None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(one, [r for r in races if r.get("source")]))

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "races": {race_id: data for race_id, data, _ in outcomes if data is not None},
        "errors": {race_id: err for race_id, _, err in outcomes if err is not None},
    }


# ---------------------------------------------------------------------------
# Reading saved snapshots
# ---------------------------------------------------------------------------


class Snapshot:
    """A saved polling snapshot (one row of polling_snapshots)."""

    def __init__(self, data: dict, snapshot_id: str | None = None,
                 fetched_at: datetime | None = None, note: str = ""):
        self.id = snapshot_id
        self.data = data or {}
        self.fetched_at = fetched_at
        self.note = note
        self._pmf_cache: dict[str, list[float]] = {}

    def race(self, race_id: str) -> dict | None:
        return self.data.get("races", {}).get(race_id)

    @property
    def errors(self) -> dict[str, str]:
        return self.data.get("errors", {})

    def probs(self, race_id: str) -> dict[str, float] | None:
        entry = self.race(race_id)
        return entry.get("probs") if entry and entry.get("kind") == "winner" else None

    def resolved(self, race_id: str) -> str | None:
        entry = self.race(race_id)
        return entry.get("resolved") if entry else None

    def pmf(self, race: dict) -> list[float] | None:
        entry = self.race(race["id"])
        if not entry or entry.get("kind") != "number":
            return None
        if race["id"] not in self._pmf_cache:
            self._pmf_cache[race["id"]] = seat_pmf(entry["survival"], race["min_value"], race["max_value"])
        return self._pmf_cache[race["id"]]

    def called(self, race_id: str) -> tuple[str, float, bool] | None:
        """(leader, probability, resolved) if the market has effectively decided."""
        probs = self.probs(race_id)
        if not probs:
            return None
        resolved = self.resolved(race_id)
        if resolved:
            return resolved, 1.0, True
        leader = max(probs, key=probs.get)
        if probs[leader] >= CALLED_THRESHOLD:
            return leader, probs[leader], False
        return None
