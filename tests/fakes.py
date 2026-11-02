"""In-memory stand-in for SupabaseStore, mirroring schema.sql's constraints."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

from database import DuplicateError
from race_catalog import RACES

RACES_BY_ID = {r["id"]: r for r in RACES}
TX_SEN = RACES_BY_ID["tx_senate"]
NE_SEN = RACES_BY_ID["ne_senate"]
CO08 = RACES_BY_ID["co08_house"]
HOUSE = RACES_BY_ID["dem_house_seats"]
TEST_RACES = [TX_SEN, NE_SEN, CO08, HOUSE]


def fake_snapshot_data(tx_dem=0.75, ne_ind=0.25, co_dem=0.80, survival=None, resolved=None) -> dict:
    """Snapshot data shaped like polling.fetch_snapshot() output."""
    resolved = resolved or {}

    def winner(race, p_first):
        a, b = race["options"]
        return {"kind": "winner", "probs": {a: p_first, b: 1 - p_first}, "raw": {a: p_first, b: 1 - p_first},
                "resolved": resolved.get(race["id"]), "source": "test", "url": "", "volume": 0}

    return {
        "fetched_at": "2026-11-02T12:00:00+00:00",
        "races": {
            TX_SEN["id"]: winner(TX_SEN, tx_dem),
            NE_SEN["id"]: winner(NE_SEN, ne_ind),
            CO08["id"]: winner(CO08, co_dem),
            HOUSE["id"]: {"kind": "number", "source": "test", "url": "", "notes": [],
                          "survival": survival or [[220, 0.9], [230, 0.7], [240, 0.5], [250, 0.3], [260, 0.1]]},
        },
        "errors": {},
    }


class InMemoryStore:
    def __init__(self, deadline: datetime, races: list[dict] | None = None):
        self.settings = {"prediction_deadline": deadline, "active_snapshot_id": None, "auto_refresh_minutes": 0}
        self.races = [copy.deepcopy(r) for r in (TEST_RACES if races is None else races)]
        self.participants: dict[str, dict] = {}
        self.predictions: dict[tuple[str, str], dict] = {}
        self.results: dict[str, dict] = {}
        self.snapshots: list[dict] = []
        self._tick = 0

    # -- settings --

    def get_settings(self) -> dict:
        return dict(self.settings)

    def update_settings(self, **fields) -> None:
        self.settings.update(fields)

    # -- races --

    def list_races(self) -> list[dict]:
        return sorted(copy.deepcopy(self.races), key=lambda r: r["sort_order"])

    def upsert_races(self, rows: list[dict]) -> None:
        by_id = {r["id"]: r for r in self.races}
        for row in rows:
            by_id[row["id"]] = copy.deepcopy(row)
        self.races = list(by_id.values())

    def delete_races(self, race_ids: list[str]) -> None:
        self.races = [r for r in self.races if r["id"] not in race_ids]
        for key in [k for k in self.predictions if k[1] in race_ids]:
            del self.predictions[key]
        for rid in race_ids:
            self.results.pop(rid, None)

    # -- participants --

    def get_participant_by_code_hash(self, code_hash: str) -> dict | None:
        for p in self.participants.values():
            if p["invite_code_hash"] == code_hash:
                return {"id": p["id"], "name": p["name"]}
        return None

    def create_participant(self, name: str, code_hash: str) -> dict:
        for p in self.participants.values():
            if p["invite_code_hash"] == code_hash or p["name"].strip().lower() == name.strip().lower():
                raise DuplicateError("That record already exists.")
        pid = str(uuid.uuid4())
        self.participants[pid] = {"id": pid, "name": name, "invite_code_hash": code_hash}
        return {"id": pid, "name": name}

    def list_participants(self) -> list[dict]:
        return sorted(
            ({"id": p["id"], "name": p["name"]} for p in self.participants.values()),
            key=lambda p: p["name"],
        )

    def delete_participant(self, participant_id: str) -> None:
        self.participants.pop(participant_id, None)
        for key in [k for k in self.predictions if k[0] == participant_id]:
            del self.predictions[key]

    # -- predictions --

    def list_predictions(self, participant_id: str | None = None) -> list[dict]:
        return [
            dict(p) for p in self.predictions.values()
            if participant_id is None or p["participant_id"] == participant_id
        ]

    def upsert_predictions(self, rows: list[dict]) -> None:
        for row in rows:
            assert row["participant_id"] in self.participants, "FK violation"
            assert (row["choice"] is None) != (row["numeric_value"] is None), "check violation"
            self.predictions[(row["participant_id"], row["race_id"])] = dict(row)

    def delete_prediction(self, participant_id: str, race_id: str) -> None:
        self.predictions.pop((participant_id, race_id), None)

    # -- results --

    def list_results(self) -> list[dict]:
        return [dict(r) for r in self.results.values()]

    def upsert_result(self, row: dict) -> None:
        self.results[row["race_id"]] = dict(row)

    def delete_result(self, race_id: str) -> None:
        self.results.pop(race_id, None)

    # -- snapshots --

    def insert_snapshot(self, data: dict, note: str) -> dict:
        self._tick += 1
        fetched = datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=self._tick)
        row = {"id": str(uuid.uuid4()), "fetched_at": fetched.isoformat(), "note": note, "data": copy.deepcopy(data)}
        self.snapshots.append(row)
        return {k: row[k] for k in ("id", "fetched_at", "note")}

    def list_snapshots(self, limit: int = 100) -> list[dict]:
        rows = sorted(self.snapshots, key=lambda r: r["fetched_at"], reverse=True)[:limit]
        return [{k: r[k] for k in ("id", "fetched_at", "note")} for r in rows]

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        return next((copy.deepcopy(r) for r in self.snapshots if r["id"] == snapshot_id), None)

    def latest_snapshot(self) -> dict | None:
        if not self.snapshots:
            return None
        row = max(self.snapshots, key=lambda r: r["fetched_at"])
        return {k: row[k] for k in ("id", "fetched_at", "note")}
