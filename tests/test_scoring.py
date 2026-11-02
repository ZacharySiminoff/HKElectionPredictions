import random

import pytest

import scoring
from polling import Snapshot, prob_within, seat_pmf
from scoring import (
    build_leaderboard,
    group_number_summary,
    group_shares,
    number_expected,
    number_points,
    score_prediction,
    win_prob,
    winner_points,
)
from tests.fakes import CO08, HOUSE, NE_SEN, TEST_RACES, TX_SEN, fake_snapshot_data

SNAP = Snapshot(fake_snapshot_data(tx_dem=0.75, ne_ind=0.25, co_dem=0.8), snapshot_id="s1")
TALARICO, PAXTON = TX_SEN["options"]


def pick(pid, race_id, choice=None, number=None):
    return {"participant_id": pid, "race_id": race_id, "choice": choice, "numeric_value": number}


def result(race_id, choice=None, number=None):
    return {"race_id": race_id, "choice": choice, "numeric_value": number}


class TestWinnerScoring:
    def test_correct_pick_scores_inverse_probability(self):
        assert score_prediction(TX_SEN, pick("a", TX_SEN["id"], TALARICO), result(TX_SEN["id"], TALARICO), SNAP) \
            == pytest.approx(1 / 0.75)
        assert score_prediction(TX_SEN, pick("a", TX_SEN["id"], PAXTON), result(TX_SEN["id"], PAXTON), SNAP) \
            == pytest.approx(1 / 0.25)

    def test_wrong_pick_scores_zero(self):
        assert score_prediction(TX_SEN, pick("a", TX_SEN["id"], PAXTON), result(TX_SEN["id"], TALARICO), SNAP) == 0

    @pytest.mark.parametrize("race", [TX_SEN, NE_SEN, CO08])
    def test_every_pick_has_expected_value_one(self, race):
        for option in race["options"]:
            p = win_prob(race, option, SNAP)
            assert p * winner_points(race, option, SNAP) == pytest.approx(1)

    def test_probability_floor_caps_points(self):
        snap = Snapshot(fake_snapshot_data(tx_dem=0.999))
        assert winner_points(TX_SEN, PAXTON, snap) == pytest.approx(1 / scoring.MIN_PROB)

    def test_missing_odds_fall_back_to_uniform(self):
        assert winner_points(TX_SEN, TALARICO, None) == pytest.approx(2)
        assert winner_points(TX_SEN, TALARICO, Snapshot({"races": {}})) == pytest.approx(2)

    def test_missing_prediction_or_result_scores_zero(self):
        assert score_prediction(TX_SEN, None, result(TX_SEN["id"], TALARICO), SNAP) == 0
        assert score_prediction(TX_SEN, pick("a", TX_SEN["id"], TALARICO), None, SNAP) == 0


class TestSeatScoring:
    pmf = SNAP.pmf(HOUSE)

    def test_exact_guess_beats_near_beats_far(self):
        exact = number_points(240, 240, self.pmf)
        near = number_points(240, 244, self.pmf)
        far = number_points(240, 255, self.pmf)
        miss = number_points(240, 280, self.pmf)
        assert exact > near > far > miss == 0

    def test_window_scores_match_definition(self):
        expected = sum(1 / prob_within(self.pmf, 240, k) for k in scoring.SEAT_WINDOWS) / len(scoring.SEAT_WINDOWS)
        assert number_points(240, 240, self.pmf) == pytest.approx(expected)

    @pytest.mark.parametrize("guess", [215, 230, 240, 255, 270])
    def test_every_guess_has_expected_value_one(self, guess):
        # Unless the probability floor kicks in, E[score] = 1 under the scoring distribution.
        ev = sum(p * number_points(guess, HOUSE["min_value"] + i, self.pmf) for i, p in enumerate(self.pmf))
        assert ev == pytest.approx(1, abs=1e-9)
        assert number_expected(guess, self.pmf, self.pmf) == pytest.approx(1)

    def test_unlikely_region_pays_more(self):
        assert number_points(275, 275, self.pmf) > number_points(240, 240, self.pmf)

    def test_live_expectation_rises_when_odds_move_toward_guess(self):
        shifted = seat_pmf([[230, 0.95], [240, 0.9], [250, 0.5], [260, 0.1]])
        assert number_expected(255, self.pmf, shifted) > 1


class TestLeaderboard:
    people = [{"id": "a", "name": "Alice"}, {"id": "b", "name": "bob"}, {"id": "c", "name": "Cara"}]

    def test_shown_before_any_result_with_projections(self):
        predictions = [pick("a", TX_SEN["id"], TALARICO), pick("b", TX_SEN["id"], PAXTON)]
        board = build_leaderboard(self.people, TEST_RACES, predictions, [], SNAP, SNAP)
        assert all(r.points == 0 for r in board)
        # Under the same odds every pick projects to exactly 1 point.
        assert {r.name: r.projected for r in board} == pytest.approx({"Alice": 1, "bob": 1, "Cara": 0})
        assert board[0].cells[TX_SEN["id"]].live_prob is not None

    def test_projection_uses_live_odds(self):
        live = Snapshot(fake_snapshot_data(tx_dem=0.99), snapshot_id="live")
        predictions = [pick("a", TX_SEN["id"], TALARICO), pick("b", TX_SEN["id"], PAXTON)]
        board = build_leaderboard(self.people[:2], TEST_RACES, predictions, [], SNAP, live)
        by_name = {r.name: r for r in board}
        assert by_name["Alice"].projected == pytest.approx(0.99 / 0.75)
        assert by_name["bob"].projected == pytest.approx(0.01 / 0.25)
        assert board[0].name == "Alice"

    def test_ties_share_rank_and_sort_by_name(self):
        predictions = [pick("a", TX_SEN["id"], TALARICO), pick("b", TX_SEN["id"], TALARICO),
                       pick("c", TX_SEN["id"], PAXTON)]
        board = build_leaderboard(self.people, TEST_RACES, predictions, [result(TX_SEN["id"], TALARICO)], SNAP)
        assert [(r.rank, r.name) for r in board] == [(1, "Alice"), (1, "bob"), (3, "Cara")]
        assert board[2].cells[TX_SEN["id"]].status == "wrong"

    def test_new_race_type_scorer_can_be_patched(self, monkeypatch):
        monkeypatch.setattr(scoring, "winner_points", lambda race, option, odds: 7)
        board = build_leaderboard(self.people[:1], TEST_RACES, [pick("a", TX_SEN["id"], TALARICO)],
                                  [result(TX_SEN["id"], TALARICO)], SNAP)
        assert board[0].points == 7


class TestGroup:
    def test_group_shares(self):
        predictions = [pick("a", TX_SEN["id"], TALARICO), pick("b", TX_SEN["id"], TALARICO),
                       pick("c", TX_SEN["id"], PAXTON), pick("c", NE_SEN["id"], "Dan Osborn (I)")]
        shares, n = group_shares(TX_SEN, predictions)
        assert n == 3
        assert shares == pytest.approx({TALARICO: 2 / 3, PAXTON: 1 / 3})

    def test_number_summary(self):
        assert group_number_summary([]) is None
        summary = group_number_summary([230, 240, 250, 260])
        assert summary["median"] == 245 and summary["mean"] == 245 and summary["n"] == 4


def test_random_pmfs_keep_ev_one():
    rng = random.Random(0)
    for _ in range(20):
        points = sorted(rng.sample(range(200, 280), 6))
        levels = sorted((rng.uniform(0.05, 0.95) for _ in points), reverse=True)
        pmf = seat_pmf([[s, p] for s, p in zip(points, levels)])
        guess = rng.randint(215, 265)
        if all(prob_within(pmf, guess, k) >= scoring.MIN_PROB for k in scoring.SEAT_WINDOWS):
            assert number_expected(guess, pmf, pmf) == pytest.approx(1)
