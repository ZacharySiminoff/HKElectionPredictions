"""Polling parsers (on real API responses saved in tests/fixtures) and the seat maths."""

import json
from pathlib import Path

import pytest

import polling
from polling import (
    PollingError,
    Snapshot,
    extend_with_ladder,
    fetch_snapshot,
    parse_kalshi_brackets,
    parse_kalshi_ladder,
    parse_polymarket_r_brackets,
    parse_polymarket_winner,
    pmf_mean,
    pmf_quantile,
    prob_within,
    seat_pmf,
    survival_from_brackets,
)
from race_catalog import RACES
from tests.fakes import HOUSE, NE_SEN, TX_SEN

FIXTURES = Path(__file__).parent / "fixtures"
AK_SEN = next(r for r in RACES if r["id"] == "ak_senate")


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class TestPolymarketWinner:
    def test_party_named_markets(self):
        odds = parse_polymarket_winner(load("polymarket_tx_senate"), TX_SEN)
        assert set(odds["probs"]) == set(TX_SEN["options"])
        assert sum(odds["probs"].values()) == pytest.approx(1)
        assert odds["probs"]["James Talarico (D)"] > 0.5
        assert odds["resolved"] is None

    def test_independent_option_ignores_placeholder_democrat_market(self):
        odds = parse_polymarket_winner(load("polymarket_ne_senate"), NE_SEN)
        assert odds["probs"]["Dan Osborn (I)"] == pytest.approx(0.285, abs=0.01)

    def test_candidate_named_markets(self):
        odds = parse_polymarket_winner(load("polymarket_ak_senate"), AK_SEN)
        assert odds["probs"]["Mary Peltola (D)"] == pytest.approx(0.725, abs=0.01)

    def test_resolution_detected(self):
        event = load("polymarket_tx_senate")
        for m in event["markets"]:
            if "Democrat" in m["question"]:
                m.update(closed=True, outcomePrices='["1", "0"]')
            elif "Republican" in m["question"]:
                m.update(closed=True, outcomePrices='["0", "1"]')
        odds = parse_polymarket_winner(event, TX_SEN)
        assert odds["resolved"] == "James Talarico (D)"
        assert Snapshot({"races": {TX_SEN["id"]: odds}}).called(TX_SEN["id"]) == ("James Talarico (D)", 1.0, True)

    def test_missing_candidate_raises(self):
        race = {**TX_SEN, "options": ["Nobody (D)", "Ken Paxton (R)"],
                "source": {**TX_SEN["source"], "match": {"Nobody (D)": ["zzz"], "Ken Paxton (R)": ["Paxton"]}}}
        with pytest.raises(PollingError):
            parse_polymarket_winner(load("polymarket_tx_senate"), race)


class TestHouseSeats:
    def test_kalshi_brackets_and_ladder(self):
        brackets = parse_kalshi_brackets(load("kalshi_d_house_brackets"))
        assert (None, 209) in [(lo, hi) for lo, hi, _ in brackets]
        assert (250, None) in [(lo, hi) for lo, hi, _ in brackets]
        points = survival_from_brackets(brackets)
        ladder = parse_kalshi_ladder(load("kalshi_d_house_ladder"))
        extended = extend_with_ladder(points, ladder)
        assert extended[-1][0] == 258 and len(extended) > len(points)
        survival = [s for _, s in extended]
        assert survival == sorted(survival, reverse=True)

        pmf = seat_pmf(extended)
        assert sum(pmf) == pytest.approx(1)
        assert 225 < pmf_quantile(pmf, 0.5) < 260
        # The pmf reproduces the bracket probabilities it was built from.
        p_218_221 = next(p for lo, hi, p in brackets if lo == 218) / sum(p for *_, p in brackets)
        assert sum(pmf[218:222]) == pytest.approx(p_218_221, abs=1e-9)

    def test_polymarket_republican_brackets_convert_to_democratic(self):
        brackets = parse_polymarket_r_brackets(load("polymarket_r_house_seats"), 435)
        spans = {(lo, hi) for lo, hi, _ in brackets}
        assert (246, None) in spans      # R below 190
        assert (241, 245) in spans       # R 190-194
        assert (None, 205) in spans      # R 230+
        pmf = seat_pmf(survival_from_brackets(brackets))
        assert sum(pmf) == pytest.approx(1)

    def test_seat_pmf_basics(self):
        pmf = seat_pmf([[200, 0.75], [210, 0.25]], lo=0, hi=435)
        assert len(pmf) == 436
        assert sum(pmf) == pytest.approx(1)
        assert sum(pmf[200:210]) == pytest.approx(0.5)
        assert sum(pmf[:200]) == pytest.approx(0.25)
        assert sum(pmf[210:]) == pytest.approx(0.25)
        assert pmf_mean(pmf) == pytest.approx(204.5, abs=3)
        assert prob_within(pmf, 205, 4) == pytest.approx(0.45)

    def test_non_monotone_input_is_repaired(self):
        pmf = seat_pmf([[200, 0.6], [210, 0.7], [220, 0.2]])
        assert all(p >= 0 for p in pmf) and sum(pmf) == pytest.approx(1)

    def test_no_points_raises(self):
        with pytest.raises(PollingError):
            seat_pmf([])


class TestFetchSnapshot:
    def fake_get(self, url):
        if "texas-senate" in url:
            return load("polymarket_tx_senate")
        if "nebraska-senate" in url:
            return load("polymarket_ne_senate")
        if "KXDHOUSESEATS" in url:
            return load("kalshi_d_house_brackets")
        if "KXDHOUSEWON" in url:
            return load("kalshi_d_house_ladder")
        if "republican-house-seats" in url:
            return load("polymarket_r_house_seats")
        raise RuntimeError(f"network down: {url}")

    def test_success_and_per_race_errors(self):
        broken = {**TX_SEN, "id": "broken", "source": {**TX_SEN["source"], "polymarket": "nope"}}
        data = fetch_snapshot([TX_SEN, NE_SEN, HOUSE, broken], get_json=self.fake_get)
        assert set(data["races"]) == {TX_SEN["id"], NE_SEN["id"], HOUSE["id"]}
        assert "broken" in data["errors"]
        assert data["races"][HOUSE["id"]]["source"] == "Kalshi"
        json.dumps(data)  # must be storable as jsonb
        snap = Snapshot(data)
        assert snap.pmf(HOUSE) is not None
        assert snap.called(TX_SEN["id"]) is None

    def test_house_falls_back_to_polymarket(self):
        def get(url):
            if "kalshi" in url:
                raise RuntimeError("kalshi down")
            return self.fake_get(url)

        data = fetch_snapshot([HOUSE], get_json=get)
        assert data["races"][HOUSE["id"]]["source"] == "Polymarket"
        assert data["races"][HOUSE["id"]]["notes"]

    def test_called_threshold(self):
        snap = Snapshot({"races": {TX_SEN["id"]: {"kind": "winner", "probs": {
            "James Talarico (D)": polling.CALLED_THRESHOLD, "Ken Paxton (R)": 1 - polling.CALLED_THRESHOLD}}}})
        assert snap.called(TX_SEN["id"])[0] == "James Talarico (D)"
