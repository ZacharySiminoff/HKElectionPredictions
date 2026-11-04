"""Streamlit UI for H.K's Election Prediction.

All rules (deadline, admin access, validation) are enforced in
database.GameService; this file only renders pages and reports errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import altair as alt
import pandas as pd
import streamlit as st

import scoring
from database import GameError, GameService, SupabaseStore
from polling import CALLED_THRESHOLD, MARKET_CALL_THRESHOLD, pmf_mean, pmf_quantile, prob_within
from race_catalog import party_of, short_name

SITE_NAME = "H.K's Election Prediction"
PARTY_COLORS = {"D": "#2563eb", "R": "#dc2626", "I": "#7c3aed"}
NEUTRAL = "#6b7280"
CATEGORY_ORDER = ["Senate", "Governor", "House", "House total"]

st.set_page_config(page_title=SITE_NAME, page_icon="🗳️", layout="wide")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@st.cache_resource
def get_service() -> GameService:
    from supabase import create_client

    # The client appends /rest/v1 itself; tolerate the API URL being pasted with it.
    url = st.secrets["supabase"]["url"].strip().rstrip("/").removesuffix("/rest/v1")
    client = create_client(url, st.secrets["supabase"]["service_role_key"])
    return GameService(SupabaseStore(client), admin_password=st.secrets["admin"]["password"])


def get_timezone() -> ZoneInfo:
    name = st.secrets.get("app", {}).get("timezone", "UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def format_time(dt: datetime | None, tz: ZoneInfo, short: bool = False) -> str:
    if dt is None:
        return "—"
    fmt = "%b %d, %I:%M %p %Z" if short else "%a %b %d, %Y at %I:%M %p %Z"
    return dt.astimezone(tz).strftime(fmt)


def format_remaining(delta: timedelta) -> str:
    total_minutes = int(delta.total_seconds() // 60)
    days, rem = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem, 60)
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def party_color(label: str | None) -> str:
    return PARTY_COLORS.get(party_of(label or ""), NEUTRAL)


def odds_bar(race: dict, probs: dict[str, float] | None) -> None:
    """Horizontal bar split by each candidate's win probability."""
    if not probs:
        st.caption("No odds available yet.")
        return
    segments = []
    for option in race["options"]:
        p = probs.get(option, 0.0)
        if p <= 0:
            continue
        label = f"{escape(short_name(option))} {p:.0%}" if p >= 0.12 else f"{p:.0%}"
        segments.append(
            f'<div style="width:{p * 100:.2f}%;background:{party_color(option)};color:#fff;'
            f'padding:4px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
            f'font-size:0.85rem;font-weight:600">{label}</div>'
        )
    st.markdown(
        '<div style="display:flex;width:100%;border-radius:6px;overflow:hidden;margin:2px 0 6px">'
        + "".join(segments) + "</div>",
        unsafe_allow_html=True,
    )


def flash(kind: str, message: str) -> None:
    st.session_state.setdefault("flash", []).append((kind, message))


def show_flashes() -> None:
    for kind, message in st.session_state.pop("flash", []):
        st.toast(message, icon="✅" if kind == "success" else "⚠️")


try:
    service = get_service()
except (KeyError, FileNotFoundError) as exc:
    missing = exc.args[0] if isinstance(exc, KeyError) and exc.args else "the secrets file"
    st.error(
        f"Missing secret: `{missing}`. The app needs `[supabase]` url and service_role_key, "
        "and `[admin]` password. Locally, put them in `.streamlit/secrets.toml` (see "
        "`.streamlit/secrets.toml.example`). On Streamlit Community Cloud, paste them into the "
        "app's **Settings → Secrets**, including the `[section]` lines. See README.md."
    )
    st.stop()

tz = get_timezone()

try:
    service.maybe_auto_refresh()
    settings = service.settings()
    races = service.races()
except GameError as exc:
    st.error(str(exc))
    st.stop()

deadline = settings["prediction_deadline"]
locked = service.is_locked()
races_by_id = {r["id"]: r for r in races}


def races_in_order() -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for race in races:
        groups.setdefault(race.get("category") or "Other", []).append(race)
    ordered = [c for c in CATEGORY_ORDER if c in groups] + [c for c in groups if c not in CATEGORY_ORDER]
    return [(c, groups[c]) for c in ordered]


# ---------------------------------------------------------------------------
# Sidebar: sign-in + navigation
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title(f"🗳️ {SITE_NAME}")
    participant = st.session_state.get("participant")

    if participant:
        st.success(f"Signed in as **{participant['name']}**")
        if st.button("Sign out"):
            st.session_state.pop("participant", None)
            st.rerun()
    else:
        with st.form("login"):
            st.subheader("Sign in")
            name = st.text_input("Name")
            code = st.text_input("Invite code", type="password")
            if st.form_submit_button("Sign in"):
                try:
                    st.session_state["participant"] = service.authenticate(name, code)
                    st.rerun()
                except GameError as exc:
                    st.error(str(exc))

    page = st.radio(
        "Page",
        ["Live scoreboard", "My picks", "Group vs markets", "Admin"],
        label_visibility="collapsed",
    )

    st.divider()
    st.caption(f"Deadline: {format_time(deadline, tz)}")
    if locked:
        st.caption("🔒 Picks are locked.")
    else:
        st.caption(f"⏳ {format_remaining(deadline - service.now())} left")
    scoring_snap = service.scoring_snapshot()
    live_snap = service.live_snapshot()
    st.caption(f"Scoring odds: {format_time(scoring_snap.fetched_at if scoring_snap else None, tz, True)}")
    st.caption(f"Live odds: {format_time(live_snap.fetched_at if live_snap else None, tz, True)}")

show_flashes()

if not races:
    st.warning("No races yet. An admin needs to open **Admin → Races** and sync the race list.")


# ---------------------------------------------------------------------------
# Live scoreboard
# ---------------------------------------------------------------------------

STATUS_STYLE = {
    "correct": "background-color: rgba(22, 163, 74, 0.45); font-weight: 600",
    "wrong": "background-color: rgba(220, 38, 38, 0.40); text-decoration: line-through",
    "scored": "background-color: rgba(22, 163, 74, 0.45); font-weight: 600",
    "scored_zero": "background-color: rgba(220, 38, 38, 0.40)",
    "none": "color: #9ca3af",
}
# Lighter versions for provisional market calls.
PROVISIONAL_STYLE = {
    "correct": "background-color: rgba(22, 163, 74, 0.22); font-weight: 600",
    "wrong": "background-color: rgba(220, 38, 38, 0.20); text-decoration: line-through",
}
PENDING_TINT = {"D": "rgba(37, 99, 235, 0.16)", "R": "rgba(220, 38, 38, 0.16)", "I": "rgba(124, 58, 237, 0.18)"}


def cell_text(race: dict, cell: scoring.Cell) -> str:
    if cell.status == "none":
        return "—"
    pick = short_name(cell.pick) if race["race_type"] == "winner" else cell.pick
    call = " (call)" if cell.provisional else ""
    if cell.status == "pending":
        if cell.live_prob is not None:
            return f"{pick} · {cell.potential:.2f} pts · {cell.live_prob:.0%}"
        return f"{pick} · up to {cell.potential:.2f} pts"
    if cell.status == "wrong":
        return f"✗ {pick} 0/{cell.potential:.2f}{call}"
    return f"✓ {pick} +{cell.points:.2f}{call}" if cell.points > 0 else f"{pick} +0"


def cell_style(race: dict, cell: scoring.Cell) -> str:
    if cell.status == "pending":
        if race["race_type"] == "winner":
            return f"background-color: {PENDING_TINT.get(party_of(cell.pick), 'transparent')}"
        return ""
    if cell.status == "scored" and cell.points <= 0:
        return STATUS_STYLE["scored_zero"]
    if cell.provisional:
        return PROVISIONAL_STYLE.get(cell.status, "")
    return STATUS_STYLE.get(cell.status, "")


def result_label(result: dict | None) -> str:
    if not result:
        return "Open"
    value = scoring.display_value(result)
    if result.get("provisional"):
        how = "market resolved" if result.get("resolved") else f"market {result['market_prob']:.1%}"
        return f"Called: {value} ({how})"
    return f"Official: {value}"


def points_table(scoring_snap, live_snap, results: dict) -> None:
    """What a correct pick is worth in every race, with live odds and status."""
    rows = []
    for race in races:
        if race["race_type"] != "winner":
            continue
        result = results.get(race["id"])
        live = live_snap.probs(race["id"]) if live_snap else None
        for option in race["options"]:
            won = bool(result and result["choice"] == option)
            rows.append({
                "Race": race["short"] or race["title"],
                "Candidate": option,
                "Points if right": round(scoring.winner_points(race, option, scoring_snap), 2),
                "Live chance": (live or {}).get(option),
                "Status": ("✓ " + ("called" if result.get("provisional") else "official")) if won
                          else ("✗" if result else ""),
            })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={
            "Points if right": st.column_config.NumberColumn(format="%.2f"),
            "Live chance": st.column_config.ProgressColumn(format="percent", min_value=0.0, max_value=1.0),
        },
        height=min(36 * (len(rows) + 1) + 4, 1400),
    )
    for race in races:
        if race["race_type"] == "number":
            pmf = scoring.race_pmf(race, scoring_snap)
            lo = race["min_value"]
            median = pmf_quantile(pmf, 0.5, lo)
            st.caption(
                f"**{race['title']}:** points depend on how close you are. An exact guess at the market median "
                f"({median}) earns {scoring.number_points(median, median, pmf, lo):.2f} pts; guesses in less likely "
                "ranges earn more if right. Each person's maximum is in their scoreboard cell."
            )


@st.fragment(run_every=60)
def scoreboard() -> None:
    service.maybe_auto_refresh()
    board = service.leaderboard()
    live_snap = service.live_snapshot()
    scoring_snap = service.scoring_snapshot()
    results = service.effective_results(races, live_snap)
    st.header("Live scoreboard")
    if not board:
        st.info("No participants yet.")
        return

    official = sum(1 for r in results.values() if not r.get("provisional"))
    called = len(results) - official
    c1, c2, c3 = st.columns(3)
    c1.metric("Races decided", f"{len(results)} / {len(races)}",
              f"{official} official · {called} market calls", delta_color="off")
    c2.metric("Leader", board[0].name, f"{board[0].points:.2f} pts", delta_color="off")
    best_projection = max(board, key=lambda r: r.projected)
    c3.metric("Projected leader", best_projection.name, f"{best_projection.projected:.2f} pts", delta_color="off")

    text_rows, style_rows = [], []
    for row in board:
        text = {"#": row.rank, "Name": row.name, "Points": f"{row.points:.2f}", "Projected": f"{row.projected:.2f}"}
        style = {"#": "", "Name": "font-weight: 600", "Points": "font-weight: 700", "Projected": ""}
        for race in races:
            cell = row.cells[race["id"]]
            text[race["short"] or race["id"]] = cell_text(race, cell)
            style[race["short"] or race["id"]] = cell_style(race, cell)
        text_rows.append(text)
        style_rows.append(style)

    text_df = pd.DataFrame(text_rows)
    style_df = pd.DataFrame(style_rows, columns=text_df.columns)
    column_config = {}
    for race in races:
        help_text = f"{race['title']} — {result_label(results.get(race['id']))}"
        column_config[race["short"] or race["id"]] = st.column_config.TextColumn(help=help_text)
    st.dataframe(
        text_df.style.apply(lambda _: style_df, axis=None),
        hide_index=True, width="stretch", column_config=column_config,
        height=min(38 * (len(text_df) + 1) + 4, 800),
    )
    st.caption(
        "**Points** = races decided so far. **Projected** = points + expected points from undecided "
        "races at the live odds. 🟩 right · 🟥 wrong (shows what it would have paid) · lighter colours and "
        f"\"(call)\" = the market has called it (≥{MARKET_CALL_THRESHOLD:.0%}) but it isn't official yet. "
        "Undecided cells are tinted by party and show points if right and the live chance. "
        f"Updates every minute. Live odds: {format_time(live_snap.fetched_at if live_snap else None, tz, True)}."
    )

    st.subheader("Points per race")
    points_table(scoring_snap, live_snap, results)

    with st.expander("How scoring works"):
        st.markdown(
            f"""
- **Candidate races:** a correct pick earns **1/p** points, where *p* is the candidate's win
  probability in the scoring snapshot; a wrong pick earns 0. If the markets are right, every
  pick is worth exactly 1 point on average, so you gain by beating the market.
- **House seats:** for each window of ±{', ±'.join(map(str, scoring.SEAT_WINDOWS))} seats around your guess,
  landing inside earns 1 / P(inside), using the market's seat distribution. Your score is the average over
  the windows. Every guess is worth 1 point on average, and closer guesses collect more windows.
- Probabilities are floored at {scoring.MIN_PROB:.0%}, so no race is worth more than
  {1 / scoring.MIN_PROB:.0f} points.
"""
        )


# ---------------------------------------------------------------------------
# My picks
# ---------------------------------------------------------------------------


def _save_pick(participant_id: str, race: dict, key: str) -> None:
    value = st.session_state.get(key)
    try:
        if value is None:
            service.clear_prediction(participant_id, race["id"])
            flash("success", f"Cleared {race['title']}")
        else:
            service.save_predictions(participant_id, {race["id"]: value})
            shown = short_name(value) if isinstance(value, str) else value
            flash("success", f"Saved {race['title']}: {shown}")
    except GameError as exc:
        flash("error", str(exc))


def seat_chart(race: dict, pmf: list[float], markers: list[tuple[str, int, str]]) -> alt.Chart:
    lo = race["min_value"]
    start, end = pmf_quantile(pmf, 0.003, lo), pmf_quantile(pmf, 0.997, lo)
    for _, value, _ in markers:
        start, end = min(start, value - 3), max(end, value + 3)
    data = pd.DataFrame({"Seats": list(range(start, end + 1)),
                         "Probability": [pmf[s - lo] for s in range(start, end + 1)]})
    bars = alt.Chart(data).mark_bar(color=PARTY_COLORS["D"], opacity=0.75).encode(
        x=alt.X("Seats:Q", scale=alt.Scale(domain=[start, end])),
        y=alt.Y("Probability:Q", axis=alt.Axis(format="%")),
        tooltip=["Seats", alt.Tooltip("Probability:Q", format=".1%")],
    )
    layers = [bars]
    majority = pd.DataFrame({"Seats": [218]})
    layers.append(alt.Chart(majority).mark_rule(strokeDash=[4, 4], color=NEUTRAL).encode(x="Seats:Q"))
    if markers:
        mk = pd.DataFrame([{"Seats": v, "Label": label, "Color": color} for label, v, color in markers])
        layers.append(alt.Chart(mk).mark_rule(strokeWidth=2).encode(
            x="Seats:Q", color=alt.Color("Color:N", scale=None), tooltip=["Label", "Seats"]))
    return alt.layer(*layers).properties(height=180)


def render_winner_pick(race, pid, current, scoring_snap, live_snap, predictions):
    probs = scoring_snap.probs(race["id"]) if scoring_snap else None
    odds_bar(race, probs)
    key = f"pick_{pid}_{race['id']}"
    st.segmented_control(
        race["title"], race["options"],
        default=current["choice"] if current else None,
        format_func=lambda o: f"{o} · {scoring.winner_points(race, o, scoring_snap):.2f} pts",
        key=key, on_change=_save_pick, args=(pid, race, key),
        disabled=locked, label_visibility="collapsed", width="stretch",
    )
    shares, n = scoring.group_shares(race, predictions)
    notes = []
    if n:
        notes.append("Group: " + " · ".join(f"{short_name(o)} {s:.0%}" for o, s in shares.items()) + f" ({n})")
    live = live_snap.probs(race["id"]) if live_snap else None
    if live and probs and live_snap.id != scoring_snap.id:
        notes.append("Live: " + " · ".join(f"{short_name(o)} {live.get(o, 0):.0%}" for o in race["options"]))
    if notes:
        st.caption("  |  ".join(notes))


def render_number_pick(race, pid, current, scoring_snap, predictions):
    pmf = scoring.race_pmf(race, scoring_snap)
    lo = race["min_value"]
    key = f"pick_{pid}_{race['id']}"
    guess = st.number_input(
        f"Your guess ({race['min_value']}–{race['max_value']})",
        min_value=race["min_value"], max_value=race["max_value"],
        value=current["numeric_value"] if current else None, step=1,
        placeholder="Enter a number", key=key, on_change=_save_pick, args=(pid, race, key),
        disabled=locked,
    )
    if scoring_snap and scoring_snap.pmf(race):
        c1, c2, c3 = st.columns(3)
        c1.metric("Market median", pmf_quantile(pmf, 0.5, lo))
        c2.metric("80% range", f"{pmf_quantile(pmf, 0.1, lo)}–{pmf_quantile(pmf, 0.9, lo)}")
        c3.metric("P(D majority, 218+)", f"{sum(pmf[218 - lo:]):.0%}")
    else:
        st.caption("No seat distribution yet; scoring falls back to a flat distribution.")
    markers = []
    values = scoring.group_numbers(race, predictions)
    if guess is not None:
        markers.append(("Your guess", int(guess), "#f59e0b"))
        windows = ", ".join(f"±{k}: {prob_within(pmf, int(guess), k, lo):.0%}" for k in scoring.SEAT_WINDOWS)
        st.caption(
            f"If exactly right: **{scoring.number_points(int(guess), int(guess), pmf, lo):.2f} pts**. "
            f"Market chance of landing within {windows}."
        )
    summary = scoring.group_number_summary(values)
    if summary:
        markers.append(("Group median", int(summary["median"]), "#10b981"))
        st.caption(f"Group: median {summary['median']:g}, mean {summary['mean']:.1f}, "
                   f"range {summary['min']}–{summary['max']} ({summary['n']})")
    st.altair_chart(seat_chart(race, pmf, markers), width="stretch")


def page_my_picks() -> None:
    st.header("My picks")
    participant = st.session_state.get("participant")
    if not participant:
        st.info("Sign in with your name and invite code in the sidebar.")
        return
    pid = participant["id"]
    mine = service.own_predictions(pid)
    predictions = service.all_predictions()
    scoring_snap = service.scoring_snapshot()
    live_snap = service.live_snapshot()

    if locked:
        st.warning("The deadline has passed — picks are locked.")
    else:
        st.caption(
            f"Click a candidate to pick; it saves immediately. Click again to clear. "
            f"You can change picks until {format_time(deadline, tz)}. "
            f"Points shown are what a correct pick earns (1/p)."
        )
    answered = sum(1 for r in races if r["id"] in mine)
    st.progress(answered / len(races) if races else 0, text=f"{answered} of {len(races)} races picked")

    for category, group in races_in_order():
        st.subheader(category)
        cols = st.columns(2) if category != "House total" else [st.container()]
        for i, race in enumerate(group):
            with cols[i % len(cols)].container(border=True):
                st.markdown(f"**{race['title']}**")
                if race["race_type"] == "winner":
                    render_winner_pick(race, pid, mine.get(race["id"]), scoring_snap, live_snap, predictions)
                else:
                    render_number_pick(race, pid, mine.get(race["id"]), scoring_snap, predictions)


# ---------------------------------------------------------------------------
# Group vs markets
# ---------------------------------------------------------------------------


def page_group() -> None:
    st.header("Group vs markets")
    predictions = service.all_predictions()
    scoring_snap = service.scoring_snapshot()
    live_snap = service.live_snapshot()
    results = service.effective_results(races, live_snap)

    rows = []
    disagreements = []
    for race in races:
        if race["race_type"] != "winner":
            continue
        shares, n = scoring.group_shares(race, predictions)
        market = scoring_snap.probs(race["id"]) if scoring_snap else None
        live = live_snap.probs(race["id"]) if live_snap else None
        if market and n:
            m_fav = max(market, key=market.get)
            g_fav = max(shares, key=shares.get)
            if m_fav != g_fav:
                disagreements.append(f"{race['short']}: group → {short_name(g_fav)}, market → {short_name(m_fav)}")
        result = results.get(race["id"])
        for option in race["options"]:
            rows.append({
                "Race": race["short"] or race["title"],
                "Candidate": option + (" ✓" if result and result["choice"] == option else ""),
                "Market": (market or {}).get(option),
                "Live": (live or {}).get(option),
                "Group": shares.get(option) if n else None,
                "Picks": round(shares.get(option, 0) * n),
            })

    if disagreements:
        st.info("Where the group's favourite differs from the market's: " + "; ".join(disagreements))
    pct = dict(format="percent", min_value=0.0, max_value=1.0)
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={
            "Market": st.column_config.ProgressColumn("Market (scoring)", **pct),
            "Live": st.column_config.ProgressColumn("Market (live)", **pct),
            "Group": st.column_config.ProgressColumn("Group picks", **pct),
        },
        height=min(36 * (len(rows) + 1) + 4, 900),
    )

    for race in races:
        if race["race_type"] != "number":
            continue
        st.subheader(race["title"])
        lo = race["min_value"]
        pmf = scoring.race_pmf(race, live_snap or scoring_snap)
        summary = scoring.group_number_summary(scoring.group_numbers(race, predictions))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Market median (live)", pmf_quantile(pmf, 0.5, lo))
        c2.metric("Market mean (live)", f"{pmf_mean(pmf, lo):.1f}")
        c3.metric("Group median", f"{summary['median']:g}" if summary else "—")
        c4.metric("Group mean", f"{summary['mean']:.1f}" if summary else "—")
        name_by_id = {p["id"]: p["name"] for p in service.participants()}
        markers = [
            (name_by_id.get(p["participant_id"], "?"), p["numeric_value"], "#f59e0b")
            for p in predictions if p["race_id"] == race["id"] and p.get("numeric_value") is not None
        ]
        if race["id"] in results:
            markers.append(("Actual", results[race["id"]]["numeric_value"], "#16a34a"))
        st.altair_chart(seat_chart(race, pmf, markers), width="stretch")
        st.caption("Bars: live market distribution. Orange: each person's guess. Green: actual result. "
                   "Dashed: 218 (majority).")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def admin_races(password: str) -> None:
    st.subheader("Races")
    plan = service.race_sync_plan()
    if not plan["changed"] and not plan["removed"]:
        st.caption(f"✓ {len(races)} races, in sync with race_catalog.py.")
        return
    st.write(f"race_catalog.py has **{len(plan['changed'])}** new or changed race(s).")
    allow = True
    if plan["removed"]:
        st.warning(
            f"Syncing removes {len(plan['removed'])} race(s) not in the catalog "
            f"({', '.join(plan['removed'])}) and their {plan['predictions_removed']} prediction(s)."
        )
        allow = st.checkbox("Yes, remove them")
    if st.button("Sync race list", type="primary", disabled=not allow):
        try:
            service.admin_sync_races(password, allow_removals=allow)
            st.rerun()
        except GameError as exc:
            st.error(str(exc))


def admin_polling(password: str) -> None:
    st.subheader("Polling snapshots")
    st.caption("Win probabilities from Polymarket; House seat distribution from Kalshi. "
               "Every refresh is saved. The **scoring** snapshot sets each pick's points.")

    c1, c2 = st.columns([2, 1])
    make_active = c1.checkbox("Use the new snapshot for scoring", value=not locked,
                              help="Usually on before the deadline and off afterwards, so points stay fixed.")
    if c2.button("Refresh odds now", type="primary"):
        with st.spinner("Fetching odds…"):
            try:
                row = service.admin_refresh_polling(password, make_active=make_active)
                snap = service.snapshot(row["id"])
                if snap and snap.errors:
                    flash("error", f"Saved, but {len(snap.errors)} race(s) failed — see the snapshot details.")
                else:
                    flash("success", "Saved a new snapshot.")
                st.rerun()
            except GameError as exc:
                st.error(str(exc))

    with st.form("auto_refresh"):
        minutes = st.number_input(
            "Auto-refresh live odds every N minutes (0 = off)", min_value=0, max_value=1440,
            value=int(settings["auto_refresh_minutes"]), step=1,
            help="Runs while anyone has the site open. Auto snapshots never change the scoring snapshot.",
        )
        if st.form_submit_button("Save auto-refresh"):
            service.admin_set_auto_refresh(password, int(minutes))
            st.rerun()

    snapshots = service.snapshots()
    if not snapshots:
        st.info("No snapshots yet. Click **Refresh odds now**.")
        return
    active_id = settings["active_snapshot_id"]

    def label(row):
        fetched = datetime.fromisoformat(row["fetched_at"])
        tag = " — ⭐ scoring" if row["id"] == active_id else ""
        return f"{format_time(fetched, tz, True)} ({row['note']}){tag}"

    ids = [row["id"] for row in snapshots]
    labels = {row["id"]: label(row) for row in snapshots}
    chosen = st.selectbox("Snapshots (newest first)", ids, format_func=labels.get,
                          index=ids.index(active_id) if active_id in ids else 0)
    if st.button("Use this snapshot for scoring", disabled=chosen == active_id):
        service.admin_set_active_snapshot(password, chosen)
        st.rerun()

    snap = service.snapshot(chosen)
    if snap:
        with st.expander("Snapshot details"):
            if snap.errors:
                st.error("Failed races: " + "; ".join(f"{k}: {v}" for k, v in snap.errors.items()))
            detail = []
            for race in races:
                if race["race_type"] == "winner":
                    probs = snap.probs(race["id"])
                    detail.append({"Race": race["title"], "Odds": " · ".join(
                        f"{short_name(o)} {probs.get(o, 0):.1%}" for o in race["options"]) if probs else "—"})
                else:
                    pmf = snap.pmf(race)
                    detail.append({"Race": race["title"], "Odds": (
                        f"median {pmf_quantile(pmf, .5, race['min_value'])}, 80% "
                        f"{pmf_quantile(pmf, .1, race['min_value'])}–{pmf_quantile(pmf, .9, race['min_value'])}"
                    ) if pmf else "—"})
            st.dataframe(pd.DataFrame(detail), hide_index=True, width="stretch")


def admin_deadline(password: str) -> None:
    st.subheader("Prediction deadline")
    local_deadline = deadline.astimezone(tz)
    with st.form("deadline"):
        col1, col2 = st.columns(2)
        new_date = col1.date_input("Date", value=local_deadline.date())
        new_time = col2.time_input("Time", value=local_deadline.time().replace(second=0, microsecond=0))
        st.caption(f"Timezone: {tz.key}")
        if st.form_submit_button("Update deadline"):
            try:
                service.admin_set_deadline(password, datetime.combine(new_date, new_time, tzinfo=tz))
                st.rerun()
            except GameError as exc:
                st.error(str(exc))


def admin_participants(password: str) -> None:
    st.subheader("Participants")
    new_code = st.session_state.pop("new_invite", None)
    if new_code:
        st.success(
            f"Invite code for **{new_code[0]}**: `{new_code[1]}`  \n"
            "Copy it now — it is not stored and won't be shown again."
        )
    with st.form("add_participant", clear_on_submit=True):
        new_name = st.text_input("New participant name")
        if st.form_submit_button("Create invite"):
            try:
                person, code = service.admin_create_participant(password, new_name)
                st.session_state["new_invite"] = (person["name"], code)
                st.rerun()
            except GameError as exc:
                st.error(str(exc))

    people = service.participants()
    if not people:
        st.caption("No participants yet.")
    for person in people:
        col1, col2 = st.columns([4, 1])
        col1.write(person["name"])
        if col2.button("Remove", key=f"del_{person['id']}"):
            service.admin_delete_participant(password, person["id"])
            st.rerun()


def admin_results(password: str) -> None:
    st.subheader("Election results")
    if not locked:
        st.info("Results can be entered once the prediction deadline has passed.")
        return
    results = service.results()

    # Suggestions from the live markets.
    live = service.live_snapshot()
    st.caption(f"Market calls from the latest snapshot ({format_time(live.fetched_at if live else None, tz, True)}). "
               "Refresh odds first for the newest calls.")
    st.info(
        f"Races at ≥{MARKET_CALL_THRESHOLD:.0%} already count on the scoreboard as provisional "
        "\"(call)\" results, updating with each snapshot. Recording a result here makes it official "
        "and overrides the market — use that to confirm calls, or to correct a market call that's wrong."
    )
    suggestions = {}
    for race in races:
        call = live.called(race["id"]) if live else None
        if call and (race["id"] not in results or results[race["id"]]["choice"] != call[0]):
            leader, p, resolved = call
            suggestions[race["id"]] = (leader, f"{race['title']}: {leader} — "
                                       + ("market resolved" if resolved else f"{p:.1%}"))
    if suggestions:
        with st.form("accept_calls"):
            picked = st.multiselect(
                f"Races the markets have called (≥{CALLED_THRESHOLD:.0%} or resolved)",
                list(suggestions), default=list(suggestions), format_func=lambda rid: suggestions[rid][1],
            )
            confirmed = st.checkbox("I confirm these are the official results (e.g. AP calls)")
            if st.form_submit_button("Record selected results", type="primary"):
                try:
                    for rid in picked:
                        service.admin_set_result(password, rid, suggestions[rid][0], confirmed)
                    flash("success", f"Recorded {len(picked)} result(s).")
                    st.rerun()
                except GameError as exc:
                    st.error(str(exc))
    else:
        st.caption("No new market calls.")

    for category, group in races_in_order():
        with st.expander(f"Enter {category} results manually"):
            for race in group:
                current = results.get(race["id"])
                with st.form(f"result_{race['id']}"):
                    st.markdown(f"**{race['title']}**" + (f" — recorded: {scoring.display_value(current)}" if current else ""))
                    if race["race_type"] == "winner":
                        options = race["options"]
                        value = st.selectbox(
                            "Winner", options, placeholder="Choose the winner",
                            index=options.index(current["choice"]) if current and current["choice"] in options else None,
                        )
                    else:
                        pmf = scoring.race_pmf(race, live)
                        st.caption(f"Live market median: {pmf_quantile(pmf, 0.5, race['min_value'])}")
                        value = st.number_input(
                            "Actual value", min_value=race["min_value"], max_value=race["max_value"],
                            value=current["numeric_value"] if current else None, step=1,
                        )
                    confirmed = st.checkbox("I confirm this is the official result")
                    col1, col2 = st.columns(2)
                    save = col1.form_submit_button("Save result", type="primary")
                    clear = col2.form_submit_button("Clear result", disabled=current is None)
                    if save:
                        if value is None:
                            st.error("Choose a value first.")
                        else:
                            try:
                                service.admin_set_result(password, race["id"], value, confirmed)
                                st.rerun()
                            except GameError as exc:
                                st.error(str(exc))
                    if clear:
                        service.admin_clear_result(password, race["id"])
                        st.rerun()


def page_admin() -> None:
    st.header("Admin")
    password = st.session_state.get("admin_password")
    if not service.check_admin(password):
        with st.form("admin_login"):
            entered = st.text_input("Admin password", type="password")
            if st.form_submit_button("Unlock"):
                if service.check_admin(entered):
                    st.session_state["admin_password"] = entered
                    st.rerun()
                else:
                    st.error("Incorrect password.")
        return

    if st.button("Lock admin"):
        st.session_state.pop("admin_password", None)
        st.rerun()

    admin_races(password)
    admin_polling(password)
    admin_deadline(password)
    admin_participants(password)
    admin_results(password)


{
    "Live scoreboard": scoreboard,
    "My picks": page_my_picks,
    "Group vs markets": page_group,
    "Admin": page_admin,
}[page]()
