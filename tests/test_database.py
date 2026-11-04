from datetime import datetime, timedelta, timezone

import pytest

import race_catalog
from database import (
    AuthError,
    DeadlineError,
    DuplicateError,
    GameError,
    GameService,
    ValidationError,
    generate_invite_code,
    hash_invite_code,
    normalize_name,
    predictions_table,
    validate_race_value,
)
from tests.fakes import CO08, HOUSE, NE_SEN, TX_SEN, InMemoryStore, fake_snapshot_data

ADMIN = "correct horse battery staple"
START = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
DEADLINE = START + timedelta(hours=19)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Fetcher:
    """Stands in for polling.fetch_snapshot; returns canned data."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = 0

    def __call__(self, races):
        self.calls += 1
        return fake_snapshot_data(**self.kwargs)


@pytest.fixture
def clock():
    return Clock(START)


@pytest.fixture
def fetcher():
    return Fetcher()


@pytest.fixture
def service(clock, fetcher):
    return GameService(InMemoryStore(DEADLINE), admin_password=ADMIN, clock=clock, fetcher=fetcher)


@pytest.fixture
def alice(service):
    return service.admin_create_participant(ADMIN, "Alice")


# -- pure validation ---------------------------------------------------------


class TestValidateRaceValue:
    def test_valid_winner(self):
        assert validate_race_value(TX_SEN, "James Talarico (D)") == {
            "choice": "James Talarico (D)", "numeric_value": None,
        }

    @pytest.mark.parametrize("value", ["Nobody", "", None, 3, "james talarico (d)"])
    def test_invalid_winner(self, value):
        with pytest.raises(ValidationError):
            validate_race_value(TX_SEN, value)

    @pytest.mark.parametrize("value, expected", [(0, 0), (218, 218), (435, 435), (220.0, 220)])
    def test_valid_number(self, value, expected):
        assert validate_race_value(HOUSE, value) == {"choice": None, "numeric_value": expected}

    @pytest.mark.parametrize("value", [-1, 436, 220.5, "220", True, None])
    def test_invalid_number(self, value):
        with pytest.raises(ValidationError):
            validate_race_value(HOUSE, value)


class TestHelpers:
    def test_invite_code_hash_ignores_case_spaces_and_dashes(self):
        assert hash_invite_code("abcde-fghjk") == hash_invite_code(" ABCDE FGHJK ")

    def test_empty_invite_code_rejected(self):
        with pytest.raises(ValidationError):
            hash_invite_code(" - ")

    def test_generated_codes_are_unique_and_formatted(self):
        codes = {generate_invite_code() for _ in range(200)}
        assert len(codes) == 200
        assert all(len(c) == 11 and c[5] == "-" for c in codes)

    def test_normalize_name(self):
        assert normalize_name("  Ada   Lovelace ") == "Ada Lovelace"
        with pytest.raises(ValidationError):
            normalize_name("   ")
        with pytest.raises(ValidationError):
            normalize_name("x" * 61)


# -- authentication ----------------------------------------------------------


class TestAuthentication:
    def test_login_with_matching_name_and_code(self, service, alice):
        person, code = alice
        assert service.authenticate("alice", code.lower()) == person

    def test_wrong_name_rejected(self, service, alice):
        _, code = alice
        with pytest.raises(AuthError):
            service.authenticate("Bob", code)

    def test_wrong_code_rejected(self, service, alice):
        with pytest.raises(AuthError):
            service.authenticate("Alice", "WRONG-CODE1")

    def test_blank_inputs_rejected(self, service, alice):
        with pytest.raises(AuthError):
            service.authenticate("", "")

    def test_plain_code_not_stored(self, service, alice):
        _, code = alice
        stored = next(iter(service.store.participants.values()))
        assert code not in stored.values()
        assert stored["invite_code_hash"] == hash_invite_code(code)


# -- admin -------------------------------------------------------------------


class TestAdmin:
    @pytest.mark.parametrize("password", [None, "", "wrong", ADMIN + " "])
    def test_bad_password_rejected_everywhere(self, service, alice, clock, fetcher, password):
        clock.now = DEADLINE
        person, _ = alice
        calls = [
            lambda: service.admin_create_participant(password, "Bob"),
            lambda: service.admin_set_deadline(password, DEADLINE),
            lambda: service.admin_set_result(password, TX_SEN["id"], "Ken Paxton (R)", True),
            lambda: service.admin_clear_result(password, TX_SEN["id"]),
            lambda: service.admin_delete_participant(password, person["id"]),
            lambda: service.admin_refresh_polling(password, make_active=True),
            lambda: service.admin_set_active_snapshot(password, None),
            lambda: service.admin_set_auto_refresh(password, 5),
            lambda: service.admin_sync_races(password, allow_removals=True),
        ]
        for call in calls:
            with pytest.raises(AuthError):
                call()
        assert len(service.participants()) == 1
        assert service.results() == {}
        assert fetcher.calls == 0
        assert service.store.snapshots == []

    def test_admin_disabled_when_no_password_configured(self, clock):
        svc = GameService(InMemoryStore(DEADLINE), admin_password="", clock=clock)
        assert not svc.check_admin("")
        with pytest.raises(AuthError):
            svc.admin_create_participant("", "Bob")

    def test_duplicate_participant_name_rejected(self, service, alice):
        with pytest.raises(DuplicateError):
            service.admin_create_participant(ADMIN, "  ALICE ")

    def test_deadline_requires_timezone(self, service):
        with pytest.raises(ValidationError):
            service.admin_set_deadline(ADMIN, datetime(2026, 11, 5, 12, 0))

    def test_result_requires_confirmation(self, service, clock):
        clock.now = DEADLINE
        with pytest.raises(ValidationError):
            service.admin_set_result(ADMIN, TX_SEN["id"], "Ken Paxton (R)", confirmed=False)
        assert service.results() == {}

    def test_result_blocked_before_deadline(self, service):
        with pytest.raises(DeadlineError):
            service.admin_set_result(ADMIN, TX_SEN["id"], "Ken Paxton (R)", confirmed=True)

    def test_result_value_validated(self, service, clock):
        clock.now = DEADLINE
        with pytest.raises(ValidationError):
            service.admin_set_result(ADMIN, HOUSE["id"], 500, confirmed=True)

    @pytest.mark.parametrize("minutes", [-1, 1441, True, 2.5])
    def test_auto_refresh_bounds(self, service, minutes):
        with pytest.raises(ValidationError):
            service.admin_set_auto_refresh(ADMIN, minutes)


# -- race sync ---------------------------------------------------------------


class TestRaceSync:
    def test_sync_installs_full_catalog_and_removes_old_races(self, clock):
        old = {"id": "port_ellis_mayor", "title": "Mayor", "short": "", "category": "Other",
               "race_type": "winner", "options": ["A", "B"], "min_value": None, "max_value": None,
               "sort_order": 1, "source": None}
        svc = GameService(InMemoryStore(DEADLINE, races=[old]), admin_password=ADMIN, clock=clock)
        plan = svc.race_sync_plan()
        assert plan["removed"] == ["port_ellis_mayor"]
        with pytest.raises(ValidationError):
            svc.admin_sync_races(ADMIN)  # removals need confirmation
        svc.admin_sync_races(ADMIN, allow_removals=True)
        assert [r["id"] for r in svc.races()] == [r["id"] for r in race_catalog.RACES]
        assert svc.race_sync_plan() == {"changed": [], "removed": [], "predictions_removed": 0}

    def test_catalog_is_valid(self):
        ids = [r["id"] for r in race_catalog.RACES]
        assert len(ids) == len(set(ids))
        for race in race_catalog.RACES:
            if race["race_type"] == "winner":
                assert len(race["options"]) >= 2
                assert set(race["source"]["match"]) == set(race["options"])
                assert all(race_catalog.party_of(o) in ("D", "R", "I") for o in race["options"])
            else:
                assert race["min_value"] <= race["max_value"]
        categories = {r["category"] for r in race_catalog.RACES}
        assert categories == {"Senate", "Governor", "House", "House total"}
        assert {"ms_senate", "ne_senate", "co08_house", "mi07_house", "tx_governor"} <= set(ids)


# -- predictions + deadline ---------------------------------------------------


class TestPredictions:
    def test_save_and_update_does_not_duplicate(self, service, alice):
        person, _ = alice
        service.save_predictions(person["id"], {TX_SEN["id"]: "James Talarico (D)"})
        service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)"})
        own = service.own_predictions(person["id"])
        assert len(service.store.predictions) == 1
        assert own[TX_SEN["id"]]["choice"] == "Ken Paxton (R)"

    def test_clear_prediction(self, service, alice):
        person, _ = alice
        service.save_predictions(person["id"], {TX_SEN["id"]: "James Talarico (D)"})
        service.clear_prediction(person["id"], TX_SEN["id"])
        assert service.own_predictions(person["id"]) == {}

    def test_unanswered_races_skipped(self, service, alice):
        person, _ = alice
        assert service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)", HOUSE["id"]: None}) == 1

    def test_one_bad_value_saves_nothing(self, service, alice):
        person, _ = alice
        with pytest.raises(ValidationError):
            service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)", HOUSE["id"]: 9999})
        assert service.own_predictions(person["id"]) == {}

    def test_unknown_race_rejected(self, service, alice):
        person, _ = alice
        with pytest.raises(ValidationError):
            service.save_predictions(person["id"], {"made_up_race": "x"})

    def test_save_and_clear_rejected_at_and_after_deadline(self, service, alice, clock):
        person, _ = alice
        service.save_predictions(person["id"], {TX_SEN["id"]: "James Talarico (D)"})
        for now in (DEADLINE, DEADLINE + timedelta(seconds=1)):
            clock.now = now
            with pytest.raises(DeadlineError):
                service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)"})
            with pytest.raises(DeadlineError):
                service.clear_prediction(person["id"], TX_SEN["id"])
        assert service.own_predictions(person["id"])[TX_SEN["id"]]["choice"] == "James Talarico (D)"

    def test_extending_deadline_reopens_predictions(self, service, alice, clock):
        person, _ = alice
        clock.now = DEADLINE
        service.admin_set_deadline(ADMIN, DEADLINE + timedelta(hours=1))
        assert service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)"}) == 1

    def test_everyone_visible_and_scoreboard_available_before_deadline(self, service, alice):
        person, _ = alice
        service.save_predictions(person["id"], {TX_SEN["id"]: "Ken Paxton (R)"})
        assert len(service.all_predictions()) == 1
        board = service.leaderboard()
        assert board[0].cells[TX_SEN["id"]].status == "pending"


# -- polling snapshots --------------------------------------------------------


class TestSnapshots:
    def test_refresh_saves_each_version_and_any_can_be_selected(self, service, fetcher):
        first = service.admin_refresh_polling(ADMIN, make_active=True)
        fetcher.kwargs = {"tx_dem": 0.6}
        second = service.admin_refresh_polling(ADMIN, make_active=False)

        assert [s["id"] for s in service.snapshots()] == [second["id"], first["id"]]
        assert service.scoring_snapshot().id == first["id"]
        assert service.live_snapshot().id == second["id"]
        assert service.live_snapshot().probs(TX_SEN["id"])["James Talarico (D)"] == 0.6

        service.admin_set_active_snapshot(ADMIN, second["id"])
        assert service.scoring_snapshot().id == second["id"]
        service.admin_set_active_snapshot(ADMIN, first["id"])
        assert service.scoring_snapshot().probs(TX_SEN["id"])["James Talarico (D)"] == 0.75

    def test_unknown_snapshot_rejected(self, service):
        with pytest.raises(ValidationError):
            service.admin_set_active_snapshot(ADMIN, "nope")

    def test_refresh_fails_cleanly_when_every_race_fails(self, clock):
        svc = GameService(InMemoryStore(DEADLINE), admin_password=ADMIN, clock=clock,
                          fetcher=lambda races: {"races": {}, "errors": {"x": "boom"}})
        with pytest.raises(GameError):
            svc.admin_refresh_polling(ADMIN, make_active=True)
        assert svc.snapshots() == []

    def test_auto_refresh_respects_interval_and_keeps_scoring_snapshot(self, service, fetcher, clock):
        assert service.maybe_auto_refresh(background=False) is False  # off by default
        first = service.admin_refresh_polling(ADMIN, make_active=True)
        service.admin_set_auto_refresh(ADMIN, 10)

        latest = datetime.fromisoformat(service.store.latest_snapshot()["fetched_at"])
        clock.now = latest + timedelta(minutes=5)
        assert service.maybe_auto_refresh(background=False) is False
        clock.now = latest + timedelta(minutes=11)
        assert service.maybe_auto_refresh(background=False) is True
        assert fetcher.calls == 2
        assert service.snapshots()[0]["note"] == "auto"
        assert service.scoring_snapshot().id == first["id"]


# -- end to end ----------------------------------------------------------------


def test_full_flow_submission_to_leaderboard(service, clock):
    service.admin_refresh_polling(ADMIN, make_active=True)  # TX D 75%, NE I 25%, CO-08 D 80%
    alice, alice_code = service.admin_create_participant(ADMIN, "Alice")
    bob, bob_code = service.admin_create_participant(ADMIN, "Bob")

    a = service.authenticate("Alice", alice_code)
    b = service.authenticate("Bob", bob_code)
    service.save_predictions(a["id"], {
        TX_SEN["id"]: "James Talarico (D)",   # favourite, 1/0.75
        NE_SEN["id"]: "Dan Osborn (I)",       # underdog, 1/0.25
        CO08["id"]: "Gabe Evans (R)",         # wrong
        HOUSE["id"]: 240,
    })
    service.save_predictions(b["id"], {
        TX_SEN["id"]: "Ken Paxton (R)",       # wrong
        NE_SEN["id"]: "Pete Ricketts (R)",    # wrong
        CO08["id"]: "Manny Rutinel (D)",      # 1/0.8
    })

    clock.now = DEADLINE + timedelta(minutes=1)
    for race_id, value in {
        TX_SEN["id"]: "James Talarico (D)",
        NE_SEN["id"]: "Dan Osborn (I)",
        CO08["id"]: "Manny Rutinel (D)",
    }.items():
        service.admin_set_result(ADMIN, race_id, value, confirmed=True)

    board = service.leaderboard()
    assert [r.name for r in board] == ["Alice", "Bob"]
    assert board[0].points == pytest.approx(1 / 0.75 + 1 / 0.25)
    assert board[1].points == pytest.approx(1 / 0.8)
    assert board[0].cells[CO08["id"]].status == "wrong"
    assert board[0].cells[HOUSE["id"]].status == "pending"
    assert board[0].projected > board[0].points  # House still to come
    assert board[1].cells[HOUSE["id"]].status == "none"

    service.admin_set_result(ADMIN, HOUSE["id"], 240, confirmed=True)
    board = service.leaderboard()
    assert board[0].cells[HOUSE["id"]].status == "scored"
    assert board[0].points > 1 / 0.75 + 1 / 0.25

    table = predictions_table(service.participants(), service.races(), service.all_predictions())
    bob_row = next(row for row in table if row["Participant"] == "Bob")
    assert bob_row[HOUSE["title"]] == "—"


# -- provisional market calls --------------------------------------------------


class TestMarketCalls:
    def setup_picks(self, service, fetcher, **odds):
        fetcher.kwargs = {}
        service.admin_refresh_polling(ADMIN, make_active=True)  # scoring: TX D 75%
        person, _ = service.admin_create_participant(ADMIN, "Alice")
        service.save_predictions(person["id"], {TX_SEN["id"]: "James Talarico (D)", NE_SEN["id"]: "Dan Osborn (I)"})
        fetcher.kwargs = odds
        service.admin_refresh_polling(ADMIN, make_active=False)  # live
        return person

    def test_called_races_count_provisionally_after_deadline(self, service, fetcher, clock):
        self.setup_picks(service, fetcher, tx_dem=0.99, ne_ind=0.5)
        assert service.effective_results() == {}  # never before the deadline
        assert service.leaderboard()[0].points == 0

        clock.now = DEADLINE
        results = service.effective_results()
        assert set(results) == {TX_SEN["id"]}
        assert results[TX_SEN["id"]]["provisional"] is True
        cell = service.leaderboard()[0].cells[TX_SEN["id"]]
        assert cell.status == "correct" and cell.provisional
        assert cell.points == pytest.approx(1 / 0.75)  # scored at the scoring snapshot's odds
        assert service.results() == {}  # nothing written to the database

    def test_below_threshold_or_number_race_not_called(self, service, fetcher, clock):
        self.setup_picks(service, fetcher, tx_dem=0.97)
        clock.now = DEADLINE
        assert service.effective_results() == {}

    def test_resolved_market_counts(self, service, fetcher, clock):
        self.setup_picks(service, fetcher, tx_dem=0.6, resolved={TX_SEN["id"]: "Ken Paxton (R)"})
        clock.now = DEADLINE
        assert service.effective_results()[TX_SEN["id"]]["choice"] == "Ken Paxton (R)"
        assert service.leaderboard()[0].cells[TX_SEN["id"]].status == "wrong"

    def test_admin_result_overrides_market_call(self, service, fetcher, clock):
        self.setup_picks(service, fetcher, tx_dem=0.99)
        clock.now = DEADLINE
        service.admin_set_result(ADMIN, TX_SEN["id"], "Ken Paxton (R)", confirmed=True)
        result = service.effective_results()[TX_SEN["id"]]
        assert result["choice"] == "Ken Paxton (R)" and not result.get("provisional")
        cell = service.leaderboard()[0].cells[TX_SEN["id"]]
        assert cell.status == "wrong" and not cell.provisional
