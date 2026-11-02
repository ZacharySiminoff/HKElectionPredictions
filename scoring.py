"""Scoring rules.

Candidate races: a correct pick scores 1/p, where p is the candidate's win
probability in the *scoring snapshot* (chosen on the Admin page). A wrong
pick scores 0. If the snapshot's probabilities are right, every pick has
expected value p * 1/p = 1, so no race or candidate is worth more than
another in expectation; you gain by disagreeing with the market and being
right.

House seat count: the same idea applied to windows around the guess. For
each window size k in SEAT_WINDOWS, landing within k seats scores
1 / P(|X - guess| <= k) under the snapshot's seat distribution, otherwise
0. The final score is the average across windows. Each window, and so the
average, has expected value 1 for any guess, while closer guesses collect
more windows.

Probabilities are floored at MIN_PROB so a near-zero long shot can't be
worth an unbounded amount (max 1 / MIN_PROB points per race).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable

from polling import Snapshot, prob_within

MIN_PROB = 0.01
SEAT_WINDOWS = (2, 5, 10, 20)


# ---------------------------------------------------------------------------
# Probabilities with fallbacks
# ---------------------------------------------------------------------------


def win_prob(race: dict, option: str, odds: Snapshot | None) -> float:
    """Probability of `option` winning, floored. Uniform if the snapshot lacks it."""
    probs = odds.probs(race["id"]) if odds else None
    if probs and option in probs:
        return max(probs[option], MIN_PROB)
    return max(1 / len(race["options"]), MIN_PROB)


def race_pmf(race: dict, odds: Snapshot | None) -> list[float]:
    """Seat distribution for a number race; uniform over its range as a fallback."""
    pmf = odds.pmf(race) if odds else None
    if pmf is None:
        n = race["max_value"] - race["min_value"] + 1
        pmf = [1 / n] * n
    return pmf


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def winner_points(race: dict, option: str, odds: Snapshot | None) -> float:
    """Points a correct pick of `option` would earn."""
    return 1 / win_prob(race, option, odds)


def number_points(guess: int, actual: int, pmf: list[float], lo: int = 0) -> float:
    total = 0.0
    for k in SEAT_WINDOWS:
        if abs(actual - guess) <= k:
            total += 1 / max(prob_within(pmf, guess, k, lo), MIN_PROB)
    return total / len(SEAT_WINDOWS)


def number_expected(guess: int, scoring_pmf: list[float], live_pmf: list[float], lo: int = 0) -> float:
    """Expected number_points if the truth follows live_pmf."""
    return sum(
        prob_within(live_pmf, guess, k, lo) / max(prob_within(scoring_pmf, guess, k, lo), MIN_PROB)
        for k in SEAT_WINDOWS
    ) / len(SEAT_WINDOWS)


def score_prediction(race: dict, prediction: dict | None, result: dict | None,
                     odds: Snapshot | None) -> float:
    """Points for one decided prediction. Missing prediction or result scores zero."""
    if prediction is None or result is None:
        return 0.0
    if race["race_type"] == "winner":
        pick = prediction.get("choice")
        return winner_points(race, pick, odds) if pick == result.get("choice") else 0.0
    guess, actual = prediction.get("numeric_value"), result.get("numeric_value")
    if guess is None or actual is None:
        return 0.0
    return number_points(guess, actual, race_pmf(race, odds), race["min_value"])


def display_value(row: dict) -> str:
    """Human-readable value of a prediction or result row."""
    if row.get("choice") is not None:
        return str(row["choice"])
    if row.get("numeric_value") is not None:
        return str(row["numeric_value"])
    return "—"


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    pick: str | None
    # "correct" / "wrong" (winner races), "scored" (number race), "pending", "none"
    status: str
    points: float = 0.0
    # Expected final points for this race under the live odds (== points once decided).
    expected: float = 0.0
    # Live probability that the pick wins (winner races, pending only).
    live_prob: float | None = None


@dataclass(frozen=True)
class LeaderboardRow:
    rank: int
    participant_id: str
    name: str
    points: float
    projected: float
    cells: dict[str, Cell] = field(default_factory=dict)


def _cell(race: dict, prediction: dict | None, result: dict | None,
          scoring_odds: Snapshot | None, live_odds: Snapshot | None) -> Cell:
    if prediction is None:
        return Cell(pick=None, status="none")
    pick = display_value(prediction)

    if result is not None:
        points = score_prediction(race, prediction, result, scoring_odds)
        if race["race_type"] == "winner":
            status = "correct" if points > 0 else "wrong"
        else:
            status = "scored"
        return Cell(pick=pick, status=status, points=points, expected=points)

    live = live_odds or scoring_odds
    if race["race_type"] == "winner":
        q = win_prob(race, prediction["choice"], live)
        return Cell(pick=pick, status="pending", live_prob=q,
                    expected=q * winner_points(race, prediction["choice"], scoring_odds))
    expected = number_expected(
        prediction["numeric_value"], race_pmf(race, scoring_odds), race_pmf(race, live), race["min_value"]
    )
    return Cell(pick=pick, status="pending", expected=expected)


def build_leaderboard(
    participants: Iterable[dict],
    races: Iterable[dict],
    predictions: Iterable[dict],
    results: Iterable[dict],
    scoring_odds: Snapshot | None = None,
    live_odds: Snapshot | None = None,
) -> list[LeaderboardRow]:
    """Live standings for everyone, at any time.

    points    = points from races with a confirmed result
    projected = points + expected points from undecided races (live odds)

    Ranked by points, then projected; ties share a rank (1, 1, 3, ...).
    """
    races = list(races)
    results_by_race = {r["race_id"]: r for r in results}
    picks = {(p["participant_id"], p["race_id"]): p for p in predictions}

    rows = []
    for person in participants:
        cells = {
            race["id"]: _cell(race, picks.get((person["id"], race["id"])),
                              results_by_race.get(race["id"]), scoring_odds, live_odds)
            for race in races
        }
        points = sum(c.points for c in cells.values())
        projected = sum(c.expected for c in cells.values())
        rows.append((person, round(points, 6), round(projected, 6), cells))

    rows.sort(key=lambda r: (-r[1], -r[2], r[0]["name"].casefold()))

    board = []
    previous = None
    rank = 0
    for position, (person, points, projected, cells) in enumerate(rows, start=1):
        if (points, projected) != previous:
            rank = position
            previous = (points, projected)
        board.append(LeaderboardRow(rank, person["id"], person["name"], points, projected, cells))
    return board


# ---------------------------------------------------------------------------
# Group consensus
# ---------------------------------------------------------------------------


def group_shares(race: dict, predictions: Iterable[dict]) -> tuple[dict[str, float], int]:
    """Share of participants picking each option, and how many picked."""
    picks = [p["choice"] for p in predictions if p["race_id"] == race["id"] and p.get("choice")]
    n = len(picks)
    return {opt: (picks.count(opt) / n if n else 0.0) for opt in race["options"]}, n


def group_numbers(race: dict, predictions: Iterable[dict]) -> list[int]:
    return sorted(
        p["numeric_value"] for p in predictions
        if p["race_id"] == race["id"] and p.get("numeric_value") is not None
    )


def group_number_summary(values: list[int]) -> dict | None:
    if not values:
        return None
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": values[0],
        "max": values[-1],
    }
