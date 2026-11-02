"""Static checks on schema.sql security settings."""

import re
from pathlib import Path

SCHEMA = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
SQL = re.sub(r"--[^\n]*", "", SCHEMA)  # strip comments
TABLES = ["app_settings", "participants", "races", "predictions", "results", "polling_snapshots"]


def test_rls_enabled_on_every_table():
    for table in TABLES:
        assert re.search(rf"alter table public\.{table}\s+enable row level security", SQL), table


def test_no_policies_or_grants_for_public_roles():
    assert "create policy" not in SQL.lower()
    for match in re.finditer(r"grant\s.*?\sto\s+([^;]+);", SQL, flags=re.S | re.I):
        roles = match.group(1)
        assert "anon" not in roles and "authenticated" not in roles


def test_public_roles_revoked_on_every_table():
    revoke = re.search(r"revoke all on table(.*?)from anon, authenticated;", SQL, re.S)
    assert revoke
    for table in TABLES:
        assert f"public.{table}" in revoke.group(1)


def test_service_role_granted_on_every_table():
    grant = re.search(r"grant select, insert, update, delete on table(.*?)to service_role;", SQL, re.S)
    assert grant
    for table in TABLES:
        assert f"public.{table}" in grant.group(1)


def test_upgrade_columns_are_idempotent():
    for column in ["active_snapshot_id", "auto_refresh_minutes", "short", "category", "source"]:
        assert re.search(rf"add column if not exists {column}\b", SQL), column
