"""Data access and server-side rules for the prediction game.

Layers:
  * SupabaseStore  - thin persistence over the Supabase Python client. It
                     knows nothing about permissions.
  * GameService    - every rule lives here: input validation, the prediction
                     deadline, and admin authorisation. The Streamlit UI only
                     calls GameService, so hiding a widget is never the only
                     thing stopping an action.

Tests drive GameService with an in-memory store (tests/fakes.py) that
implements the same methods as SupabaseStore.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import polling
import race_catalog
import scoring

# Unambiguous characters (no 0/O, 1/I/L) so codes are easy to read aloud.
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 10
MAX_NAME_LENGTH = 60
RACE_COLUMNS = ("id", "title", "short", "category", "race_type", "options",
                "min_value", "max_value", "sort_order", "source")


class GameError(Exception):
    """Base class for errors that are safe to show to the user."""


class ValidationError(GameError):
    pass


class DeadlineError(GameError):
    pass


class AuthError(GameError):
    pass


class DuplicateError(GameError):
    pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_invite_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_invite_code(code: str) -> str:
    normalized = normalize_invite_code(code)
    if not normalized:
        raise ValidationError("Invite code is required.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_invite_code() -> str:
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
    return f"{raw[:5]}-{raw[5:]}"


def normalize_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValidationError("Name is required.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValidationError(f"Name must be at most {MAX_NAME_LENGTH} characters.")
    return cleaned


def validate_race_value(race: dict, value: Any) -> dict:
    """Return {"choice": ..., "numeric_value": ...} for a valid value, else raise."""
    if race["race_type"] == "winner":
        if not isinstance(value, str) or value not in race["options"]:
            raise ValidationError(f"Pick one of the listed candidates for {race['title']}.")
        return {"choice": value, "numeric_value": None}

    if race["race_type"] == "number":
        # bool is a subclass of int; reject it explicitly.
        if isinstance(value, bool):
            raise ValidationError(f"{race['title']} must be a whole number.")
        if isinstance(value, float):
            if not value.is_integer():
                raise ValidationError(f"{race['title']} must be a whole number.")
            value = int(value)
        if not isinstance(value, int):
            raise ValidationError(f"{race['title']} must be a whole number.")
        if not race["min_value"] <= value <= race["max_value"]:
            raise ValidationError(
                f"{race['title']} must be between {race['min_value']} and {race['max_value']}."
            )
        return {"choice": None, "numeric_value": value}

    raise ValidationError(f"Unknown race type {race['race_type']!r}.")


def deadline_passed(deadline: datetime, now: datetime) -> bool:
    return now >= deadline


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_snapshot(row: dict | None) -> polling.Snapshot | None:
    if row is None:
        return None
    return polling.Snapshot(
        row["data"], snapshot_id=row["id"],
        fetched_at=_parse_timestamp(row["fetched_at"]), note=row.get("note") or "",
    )


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------


class SupabaseStore:
    """Persistence over supabase-py. Must be created with the service_role key."""

    def __init__(self, client):
        self.client = client

    def _run(self, query):
        from postgrest.exceptions import APIError

        try:
            return query.execute().data
        except APIError as exc:
            message = exc.message or str(exc)
            if exc.code == "23505":
                raise DuplicateError("That record already exists.") from exc
            if "deadline has passed" in message:
                raise DeadlineError("The prediction deadline has passed.") from exc
            if exc.code in ("P0001", "23514"):
                raise ValidationError(message) from exc
            if exc.code in ("42703", "42P01", "PGRST204", "PGRST205"):
                raise GameError(
                    f"Database schema is out of date ({message}). Re-run schema.sql in Supabase."
                ) from exc
            raise

    # -- settings --

    def get_settings(self) -> dict:
        rows = self._run(self.client.table("app_settings").select("*"))
        if not rows:
            raise GameError("app_settings is empty. Run schema.sql in Supabase.")
        row = rows[0]
        return {
            "prediction_deadline": _parse_timestamp(row["prediction_deadline"]),
            "active_snapshot_id": row.get("active_snapshot_id"),
            "auto_refresh_minutes": row.get("auto_refresh_minutes") or 0,
        }

    def update_settings(self, **fields) -> None:
        if isinstance(fields.get("prediction_deadline"), datetime):
            fields["prediction_deadline"] = fields["prediction_deadline"].isoformat()
        self._run(self.client.table("app_settings").update(fields).eq("id", True))

    # -- races --

    def list_races(self) -> list[dict]:
        return self._run(self.client.table("races").select("*").order("sort_order"))

    def upsert_races(self, rows: list[dict]) -> None:
        self._run(self.client.table("races").upsert(rows, on_conflict="id"))

    def delete_races(self, race_ids: list[str]) -> None:
        self._run(self.client.table("races").delete().in_("id", race_ids))

    # -- participants --

    def get_participant_by_code_hash(self, code_hash: str) -> dict | None:
        rows = self._run(
            self.client.table("participants")
            .select("id, name")
            .eq("invite_code_hash", code_hash)
            .limit(1)
        )
        return rows[0] if rows else None

    def create_participant(self, name: str, code_hash: str) -> dict:
        rows = self._run(
            self.client.table("participants").insert(
                {"name": name, "invite_code_hash": code_hash}
            )
        )
        return {"id": rows[0]["id"], "name": rows[0]["name"]}

    def list_participants(self) -> list[dict]:
        return self._run(
            self.client.table("participants").select("id, name, created_at").order("name")
        )

    def delete_participant(self, participant_id: str) -> None:
        self._run(self.client.table("participants").delete().eq("id", participant_id))

    # -- predictions --

    def list_predictions(self, participant_id: str | None = None) -> list[dict]:
        query = self.client.table("predictions").select("*")
        if participant_id is not None:
            query = query.eq("participant_id", participant_id)
        return self._run(query)

    def upsert_predictions(self, rows: list[dict]) -> None:
        self._run(
            self.client.table("predictions").upsert(rows, on_conflict="participant_id,race_id")
        )

    def delete_prediction(self, participant_id: str, race_id: str) -> None:
        self._run(
            self.client.table("predictions").delete()
            .eq("participant_id", participant_id).eq("race_id", race_id)
        )

    # -- results --

    def list_results(self) -> list[dict]:
        return self._run(self.client.table("results").select("*"))

    def upsert_result(self, row: dict) -> None:
        self._run(self.client.table("results").upsert(row, on_conflict="race_id"))

    def delete_result(self, race_id: str) -> None:
        self._run(self.client.table("results").delete().eq("race_id", race_id))

    # -- polling snapshots --

    def insert_snapshot(self, data: dict, note: str) -> dict:
        rows = self._run(self.client.table("polling_snapshots").insert({"data": data, "note": note}))
        return rows[0]

    def list_snapshots(self, limit: int = 100) -> list[dict]:
        return self._run(
            self.client.table("polling_snapshots")
            .select("id, fetched_at, note")
            .order("fetched_at", desc=True)
            .limit(limit)
        )

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        rows = self._run(self.client.table("polling_snapshots").select("*").eq("id", snapshot_id))
        return rows[0] if rows else None

    def latest_snapshot(self) -> dict | None:
        """id/fetched_at/note of the newest snapshot (no data; fetch it with get_snapshot)."""
        rows = self._run(
            self.client.table("polling_snapshots")
            .select("id, fetched_at, note")
            .order("fetched_at", desc=True)
            .limit(1)
        )
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Game rules
# ---------------------------------------------------------------------------


class GameService:
    def __init__(
        self,
        store,
        admin_password: str,
        clock: Callable[[], datetime] = utcnow,
        fetcher: Callable[[list[dict]], dict] = polling.fetch_snapshot,
    ):
        self.store = store
        self._admin_password = admin_password or ""
        self._clock = clock
        self._fetcher = fetcher
        self._snapshot_cache: dict[str, polling.Snapshot] = {}
        self._refresh_lock = threading.Lock()

    # -- shared --------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def settings(self) -> dict:
        return self.store.get_settings()

    def deadline(self) -> datetime:
        return self.settings()["prediction_deadline"]

    def is_locked(self) -> bool:
        return deadline_passed(self.deadline(), self.now())

    def races(self) -> list[dict]:
        return self.store.list_races()

    def _races_by_id(self) -> dict[str, dict]:
        return {race["id"]: race for race in self.store.list_races()}

    def participants(self) -> list[dict]:
        return self.store.list_participants()

    def all_predictions(self) -> list[dict]:
        """Everyone's predictions. Visible to everyone at all times."""
        return self.store.list_predictions()

    def results(self) -> dict[str, dict]:
        """Results recorded by an admin."""
        return {r["race_id"]: r for r in self.store.list_results()}

    def effective_results(self, races: list[dict] | None = None,
                          live: polling.Snapshot | None = None) -> dict[str, dict]:
        """Admin results, plus provisional market calls for the other races.

        After the deadline, a candidate race with no recorded result counts as
        decided once the live market resolves or prices its leader at
        MARKET_CALL_THRESHOLD or above. Those rows carry "provisional": True.
        If the market later drops below the threshold the call disappears;
        an admin-recorded result always wins.
        """
        results = self.results()
        if not self.is_locked():
            return results
        live = live or self.live_snapshot()
        if live is None:
            return results
        for race in races if races is not None else self.store.list_races():
            if race["race_type"] != "winner" or race["id"] in results:
                continue
            call = live.called(race["id"], polling.MARKET_CALL_THRESHOLD)
            if call:
                leader, prob, resolved = call
                results[race["id"]] = {"race_id": race["id"], "choice": leader, "numeric_value": None,
                                       "provisional": True, "market_prob": prob, "resolved": resolved}
        return results

    # -- polling snapshots ---------------------------------------------------

    def snapshot(self, snapshot_id: str | None) -> polling.Snapshot | None:
        """A saved snapshot by id (cached; snapshots never change)."""
        if not snapshot_id:
            return None
        if snapshot_id not in self._snapshot_cache:
            snap = _to_snapshot(self.store.get_snapshot(snapshot_id))
            if snap is None:
                return None
            self._snapshot_cache[snapshot_id] = snap
        return self._snapshot_cache[snapshot_id]

    def scoring_snapshot(self) -> polling.Snapshot | None:
        """The snapshot whose probabilities set each pick's points."""
        return self.snapshot(self.settings()["active_snapshot_id"])

    def live_snapshot(self) -> polling.Snapshot | None:
        """The most recent snapshot, used for live odds and projections."""
        row = self.store.latest_snapshot()
        return self.snapshot(row["id"]) if row else None

    def snapshots(self) -> list[dict]:
        return self.store.list_snapshots()

    def _fetch_and_store(self, note: str) -> tuple[dict, dict]:
        races = [r for r in self.store.list_races() if r.get("source")]
        if not races:
            raise GameError("No races with odds sources. Sync the race list first.")
        data = self._fetcher(races)
        if not data.get("races"):
            raise GameError(f"Fetching odds failed for every race: {data.get('errors')}")
        return self.store.insert_snapshot(data, note), data

    def maybe_auto_refresh(self, background: bool = True) -> bool:
        """Fetch a new live snapshot if auto-refresh is on and the latest is stale.

        Auto snapshots never change the scoring snapshot. Returns True if a
        refresh was started. Only one refresh runs at a time.
        """
        minutes = self.settings()["auto_refresh_minutes"]
        if minutes <= 0:
            return False
        latest = self.store.latest_snapshot()
        if latest and self.now() - _parse_timestamp(latest["fetched_at"]) < timedelta(minutes=minutes):
            return False
        if not self._refresh_lock.acquire(blocking=False):
            return False

        def work():
            try:
                self._fetch_and_store("auto")
            except Exception:
                pass  # the next page load will try again
            finally:
                self._refresh_lock.release()

        if background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
        return True

    def leaderboard(self) -> list[scoring.LeaderboardRow]:
        """Live standings, available at all times. Market calls count provisionally."""
        races = self.store.list_races()
        live = self.live_snapshot()
        return scoring.build_leaderboard(
            participants=self.store.list_participants(),
            races=races,
            predictions=self.store.list_predictions(),
            results=self.effective_results(races, live).values(),
            scoring_odds=self.scoring_snapshot(),
            live_odds=live,
        )

    # -- participants --------------------------------------------------------

    def authenticate(self, name: str, invite_code: str) -> dict:
        """Return {"id", "name"} if name + code match, else raise AuthError."""
        try:
            code_hash = hash_invite_code(invite_code)
            wanted = normalize_name(name).casefold()
        except ValidationError:
            raise AuthError("Name and invite code don't match.") from None
        participant = self.store.get_participant_by_code_hash(code_hash)
        if participant is None or not hmac.compare_digest(
            participant["name"].casefold().encode(), wanted.encode()
        ):
            raise AuthError("Name and invite code don't match.")
        return {"id": participant["id"], "name": participant["name"]}

    def own_predictions(self, participant_id: str) -> dict[str, dict]:
        return {p["race_id"]: p for p in self.store.list_predictions(participant_id)}

    def save_predictions(self, participant_id: str, values: dict[str, Any]) -> int:
        """Validate and upsert predictions. `values` maps race_id -> value.

        Entries whose value is None are skipped (left unanswered). Returns the
        number of predictions saved. All values are validated before anything
        is written, so a bad value never causes a partial save.
        """
        if deadline_passed(self.deadline(), self.now()):
            raise DeadlineError("The prediction deadline has passed.")

        races = self._races_by_id()
        rows = []
        for race_id, value in values.items():
            if value is None:
                continue
            race = races.get(race_id)
            if race is None:
                raise ValidationError(f"Unknown race {race_id!r}.")
            rows.append(
                {"participant_id": participant_id, "race_id": race_id, **validate_race_value(race, value)}
            )
        if rows:
            self.store.upsert_predictions(rows)
        return len(rows)

    def clear_prediction(self, participant_id: str, race_id: str) -> None:
        if deadline_passed(self.deadline(), self.now()):
            raise DeadlineError("The prediction deadline has passed.")
        self.store.delete_prediction(participant_id, race_id)

    # -- admin ---------------------------------------------------------------

    def check_admin(self, password: str | None) -> bool:
        if not self._admin_password or not password:
            return False
        return hmac.compare_digest(password.encode(), self._admin_password.encode())

    def _require_admin(self, password: str | None) -> None:
        if not self.check_admin(password):
            raise AuthError("Admin password is incorrect.")

    def admin_set_deadline(self, password: str, deadline: datetime) -> None:
        self._require_admin(password)
        if deadline.tzinfo is None:
            raise ValidationError("Deadline must include a timezone.")
        self.store.update_settings(prediction_deadline=deadline.astimezone(timezone.utc))

    def admin_create_participant(self, password: str, name: str) -> tuple[dict, str]:
        """Create a participant and return (participant, plain invite code).

        The plain code is shown once; only its hash is stored.
        """
        self._require_admin(password)
        clean = normalize_name(name)
        if any(p["name"].casefold() == clean.casefold() for p in self.store.list_participants()):
            raise DuplicateError(f"A participant named {clean!r} already exists.")
        code = generate_invite_code()
        participant = self.store.create_participant(clean, hash_invite_code(code))
        return participant, code

    def admin_delete_participant(self, password: str, participant_id: str) -> None:
        self._require_admin(password)
        self.store.delete_participant(participant_id)

    def admin_set_result(self, password: str, race_id: str, value: Any, confirmed: bool) -> None:
        self._require_admin(password)
        if not confirmed:
            raise ValidationError("Tick the confirmation box to record a result.")
        if not self.is_locked():
            raise DeadlineError("Results can only be entered after the prediction deadline.")
        race = self._races_by_id().get(race_id)
        if race is None:
            raise ValidationError(f"Unknown race {race_id!r}.")
        self.store.upsert_result({"race_id": race_id, **validate_race_value(race, value)})

    def admin_clear_result(self, password: str, race_id: str) -> None:
        self._require_admin(password)
        self.store.delete_result(race_id)

    def race_sync_plan(self, catalog: list[dict] | None = None) -> dict:
        """What admin_sync_races would change: races to add/update and remove."""
        catalog = race_catalog.RACES if catalog is None else catalog
        current = self._races_by_id()
        wanted = {r["id"] for r in catalog}
        removed = [rid for rid in current if rid not in wanted]
        changed = [
            r["id"] for r in catalog
            if r["id"] not in current
            or any(current[r["id"]].get(col) != r.get(col) for col in RACE_COLUMNS)
        ]
        predictions = self.store.list_predictions()
        doomed = sum(1 for p in predictions if p["race_id"] in removed)
        return {"changed": changed, "removed": removed, "predictions_removed": doomed}

    def admin_sync_races(self, password: str, catalog: list[dict] | None = None,
                         allow_removals: bool = False) -> dict:
        """Make the races table match race_catalog.RACES."""
        self._require_admin(password)
        catalog = race_catalog.RACES if catalog is None else catalog
        plan = self.race_sync_plan(catalog)
        if plan["removed"] and not allow_removals:
            raise ValidationError(
                f"Sync would remove {len(plan['removed'])} race(s) and "
                f"{plan['predictions_removed']} prediction(s). Confirm removals to continue."
            )
        if plan["changed"]:
            self.store.upsert_races([{col: r.get(col) for col in RACE_COLUMNS} for r in catalog])
        if plan["removed"]:
            self.store.delete_races(plan["removed"])
        return plan

    def admin_refresh_polling(self, password: str, make_active: bool, note: str = "manual") -> dict:
        """Fetch odds now and save them as a new snapshot. Returns the snapshot row."""
        self._require_admin(password)
        row, _ = self._fetch_and_store(note)
        if make_active:
            self.store.update_settings(active_snapshot_id=row["id"])
        return row

    def admin_set_active_snapshot(self, password: str, snapshot_id: str | None) -> None:
        self._require_admin(password)
        if snapshot_id is not None and self.store.get_snapshot(snapshot_id) is None:
            raise ValidationError("Unknown snapshot.")
        self.store.update_settings(active_snapshot_id=snapshot_id)

    def admin_set_auto_refresh(self, password: str, minutes: int) -> None:
        self._require_admin(password)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
            raise ValidationError("Auto-refresh must be between 0 and 1440 minutes.")
        self.store.update_settings(auto_refresh_minutes=minutes)


def predictions_table(
    participants: Iterable[dict], races: list[dict], predictions: Iterable[dict]
) -> list[dict]:
    """Rows of {"Participant": name, <race title>: value, ...} for display."""
    by_participant: dict[str, dict[str, dict]] = {}
    for p in predictions:
        by_participant.setdefault(p["participant_id"], {})[p["race_id"]] = p

    table = []
    for person in participants:
        row: dict[str, Any] = {"Participant": person["name"]}
        picks = by_participant.get(person["id"], {})
        for race in races:
            pick = picks.get(race["id"])
            row[race["title"]] = scoring.display_value(pick) if pick else "—"
        table.append(row)
    return table
