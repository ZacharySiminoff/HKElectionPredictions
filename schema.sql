-- H.K's Election Prediction - database schema.
-- Run this whole file in the Supabase SQL Editor. It is safe to re-run, and
-- upgrades a database created by an earlier version of this file.
--
-- Races are NOT seeded here: they live in race_catalog.py and are copied in
-- from the app (Admin -> Races -> "Sync race list").
--
-- Security model:
--   * RLS is enabled on every table with NO policies, so anon/authenticated
--     roles (the public API keys) can read or write nothing.
--   * Privileges for anon/authenticated are revoked explicitly as well.
--   * The Streamlit backend connects with the service_role (secret) key,
--     which is granted table privileges below and bypasses RLS.
--   * Participant/admin permissions are enforced in the Streamlit backend
--     (database.py). Triggers below re-check deadlines and values as a
--     second line of defence.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

-- Each refresh of the odds data is kept as one immutable row.
create table if not exists public.polling_snapshots (
    id uuid primary key default gen_random_uuid(),
    fetched_at timestamptz not null default now(),
    note text not null default '',
    data jsonb not null
);

create index if not exists polling_snapshots_fetched_at_idx
    on public.polling_snapshots (fetched_at desc);

-- Single-row settings table (the boolean PK + check guarantees one row).
create table if not exists public.app_settings (
    id boolean primary key default true check (id),
    prediction_deadline timestamptz not null
);

-- The snapshot whose probabilities set the points (1/p) for each pick.
alter table public.app_settings
    add column if not exists active_snapshot_id uuid
        references public.polling_snapshots (id) on delete set null;

-- Fetch fresh odds automatically when the newest snapshot is older than
-- this many minutes (0 = off). Auto snapshots don't change the scoring one.
alter table public.app_settings
    add column if not exists auto_refresh_minutes integer not null default 0
        check (auto_refresh_minutes between 0 and 1440);

create table if not exists public.participants (
    id uuid primary key default gen_random_uuid(),
    name text not null check (length(btrim(name)) between 1 and 60),
    -- SHA-256 hex digest of the invite code; the plain code is never stored.
    invite_code_hash text not null unique check (invite_code_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default now()
);

-- Names are unique case-insensitively so two people can't both be "Alex".
create unique index if not exists participants_name_lower_key
    on public.participants (lower(btrim(name)));

create table if not exists public.races (
    id text primary key check (id ~ '^[a-z0-9_]+$'),
    title text not null,
    race_type text not null check (race_type in ('winner', 'number')),
    -- For 'winner' races: JSON array of candidate names.
    options jsonb,
    -- For 'number' races: inclusive bounds.
    min_value integer,
    max_value integer,
    sort_order integer not null default 0,
    check (
        (race_type = 'winner'
            and jsonb_typeof(options) = 'array'
            and jsonb_array_length(options) >= 2
            and min_value is null and max_value is null)
        or
        (race_type = 'number'
            and options is null
            and min_value is not null and max_value is not null
            and min_value <= max_value)
    )
);

alter table public.races add column if not exists short text not null default '';
alter table public.races add column if not exists category text not null default 'Other';
-- Where the odds come from (market slugs/tickers); see race_catalog.py.
alter table public.races add column if not exists source jsonb;

-- The composite primary key means one prediction per participant per race.
-- The app upserts on this key, so re-submitting edits instead of duplicating.
create table if not exists public.predictions (
    participant_id uuid not null references public.participants (id) on delete cascade,
    race_id text not null references public.races (id) on delete cascade,
    choice text,
    numeric_value integer,
    updated_at timestamptz not null default now(),
    primary key (participant_id, race_id),
    -- Exactly one of choice / numeric_value must be set.
    check ((choice is null) <> (numeric_value is null))
);

-- One official result per race.
create table if not exists public.results (
    race_id text primary key references public.races (id) on delete cascade,
    choice text,
    numeric_value integer,
    entered_at timestamptz not null default now(),
    check ((choice is null) <> (numeric_value is null))
);

-- ---------------------------------------------------------------------------
-- Integrity triggers (defence in depth; the backend validates first)
-- ---------------------------------------------------------------------------

-- Checks that a prediction/result value fits its race's type, options, range.
create or replace function public.check_value_matches_race()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    r public.races%rowtype;
begin
    select * into r from public.races where id = new.race_id;
    if not found then
        raise exception 'Unknown race %', new.race_id;
    end if;

    if r.race_type = 'winner' then
        if new.choice is null or not (r.options ? new.choice) then
            raise exception 'Invalid choice % for race %', new.choice, new.race_id;
        end if;
    else
        if new.numeric_value is null
           or new.numeric_value < r.min_value
           or new.numeric_value > r.max_value then
            raise exception 'Value % out of range for race %', new.numeric_value, new.race_id;
        end if;
    end if;
    return new;
end;
$$;

-- Rejects prediction writes after the deadline, using the database clock.
create or replace function public.enforce_prediction_deadline()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    deadline timestamptz;
begin
    select prediction_deadline into deadline from public.app_settings where id;
    if deadline is null or now() >= deadline then
        raise exception 'Prediction deadline has passed';
    end if;
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists predictions_check_value on public.predictions;
create trigger predictions_check_value
    before insert or update on public.predictions
    for each row execute function public.check_value_matches_race();

drop trigger if exists predictions_deadline on public.predictions;
create trigger predictions_deadline
    before insert or update on public.predictions
    for each row execute function public.enforce_prediction_deadline();

drop trigger if exists results_check_value on public.results;
create trigger results_check_value
    before insert or update on public.results
    for each row execute function public.check_value_matches_race();

-- Trigger functions should not be callable through the API.
revoke all on function public.check_value_matches_race() from public, anon, authenticated;
revoke all on function public.enforce_prediction_deadline() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- Row level security + privileges
-- ---------------------------------------------------------------------------

alter table public.app_settings      enable row level security;
alter table public.participants      enable row level security;
alter table public.races             enable row level security;
alter table public.predictions       enable row level security;
alter table public.results           enable row level security;
alter table public.polling_snapshots enable row level security;

-- Supabase grants anon/authenticated access to new public tables by default;
-- remove it explicitly. With RLS on and no policies they'd see nothing anyway.
revoke all on table
    public.app_settings, public.participants, public.races,
    public.predictions, public.results, public.polling_snapshots
from anon, authenticated;

grant usage on schema public to service_role;
grant select, insert, update, delete on table
    public.app_settings, public.participants, public.races,
    public.predictions, public.results, public.polling_snapshots
to service_role;

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

-- Default deadline: 6pm US Eastern on election day (Nov 3, 2026), when the
-- first polls close. Change it from the app's Admin page.
insert into public.app_settings (id, prediction_deadline)
values (true, '2026-11-03 18:00:00-05')
on conflict (id) do nothing;

-- Remove the fictional demo races from the first version (their
-- predictions/results cascade). Real races come from race_catalog.py.
delete from public.races
where id in ('northbridge_governor', 'lakeshire_senate', 'port_ellis_mayor');
