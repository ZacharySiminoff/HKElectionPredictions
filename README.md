# H.K's Election Prediction

A prediction game for the 2026 midterms, built with Streamlit and Supabase. Participants sign in with a name and invite code. They pick winners in 19 races and guess the total number of Democratic House seats. Points come from live prediction-market odds: a correct pick earns 1/p.

## Races

Races are defined in [race_catalog.py](race_catalog.py) and copied into the database from the Admin page.

- **Senate (12):** the competitive races, meaning every race whose favorite was under 90% on Polymarket on Nov 2: AK, FL (special), IA, KS, LA, ME, MI, MS, NE (Osborn, independent), OH (special), SC, TX.
- **Governor (5):** GA, IA, KS, OH, TX.
- **House (2):** CO-08, MI-07.
- **House total:** Democratic seats (0–435).

## Data sources (free, no API keys)

| What | Source | Notes |
|---|---|---|
| Win probability per candidate | [Polymarket Gamma API](https://gamma-api.polymarket.com) | Prices normalized to sum to 1 across the listed candidates |
| House seat distribution | [Kalshi API](https://api.elections.kalshi.com/trade-api/v2): `KXDHOUSESEATS-27` (4-seat brackets) + `KXDHOUSEWON-26NOV03` ("at least N" ladder, shapes the >249 tail) | Falls back to Polymarket's Republican-seat brackets if Kalshi fails |
| Results as they come in | The same Polymarket markets: a race shows as **called** once its price passes 97%, and **resolved** once the market settles | The admin confirms each result. Official AP/Decision Desk feeds are paid; markets usually move within minutes of a call. |

Every refresh is saved as a **snapshot**. The admin chooses which one is the **scoring snapshot**: its probabilities set the points for each pick. The newest snapshot supplies the **live** odds for projections. To fetch new live odds every N minutes on election night, turn on auto-refresh. Auto snapshots never change the scoring snapshot.

## Scoring

- **Candidate races:** a correct pick scores **1/p**, where *p* is the candidate's probability in the scoring snapshot. A wrong pick scores 0. If the market is right, every pick is worth exactly 1 point on average, so you only gain by beating the market.
- **House seats:** this uses the same idea with windows. For each window k ∈ {±2, ±5, ±10, ±20}, landing within k seats of your guess scores 1 / P(within k), using the snapshot's seat distribution. The final score is the average across windows. Every guess has expected value 1, the same as the races. Closer guesses collect more windows, and guesses in less likely regions pay more.
- Probabilities are floored at 1%, so a race is worth at most 100 points.
- **Projected** score = points so far + expected points from undecided races at the live odds.

The seat distribution comes from the market brackets. Probability is spread evenly within each bracket, and the open-ended tails decay geometrically. The result reproduces every bracket's probability exactly.

## Pages

- **Live scoreboard** (always visible, refreshes every minute): a color-coded table. Green means right, red means wrong, and undecided cells are tinted by party and show the pick's live chance.
- **My picks:** click a candidate button to pick; it saves immediately. Each button shows the points a correct pick earns, the odds bar is always visible, and the group's split appears under each race. The House guess shows the market distribution with your guess marked.
- **Group vs markets:** the market, live, and group percentages for every candidate; races where the group's favorite differs from the market's; and the House distribution with everyone's guesses.
- **Admin:** race sync, odds refresh and snapshot selection, auto-refresh, deadline, invites, and results (one-click "record market calls", plus manual entry).

## Security model

- **Secrets** live in `.streamlit/secrets.toml`, which is gitignored.
- **The backend uses the Supabase secret (service_role) key.** RLS is on for every table, with no policies. `anon` and `authenticated` have their privileges revoked, and `service_role` gets explicit grants.
- **Rules are enforced in `GameService`** ([database.py](database.py)):
  - The deadline is checked on the server for every save and clear.
  - Every admin action requires the admin password, compared in constant time.
  - Results can only be entered after the deadline, and each needs a confirmation tick.
- **The database enforces them too:**
  - One prediction per person per race (composite primary key).
  - Unique names and invite-code hashes.
  - Triggers that reject invalid candidates, out-of-range numbers, and prediction writes after the deadline.

## Supabase setup / upgrade

1. **SQL Editor → New query** → paste all of [schema.sql](schema.sql) → **Run**. The script is safe to re-run. On an existing database it:
   - Adds the `polling_snapshots` table and the new columns.
   - Removes the three fictional demo races, along with their predictions and results.
2. Keep `public` in **Project Settings → Data API → Exposed schemas** (the default).
3. Use the **secret** key (`sb_secret_…`, or the legacy `service_role` key) in `.streamlit/secrets.toml`. Set `url` to the bare project URL, without `/rest/v1`.

To also clear out test participants:
```sql
truncate public.results, public.predictions, public.participants;
```

## Election-day runbook

1. Re-run `schema.sql` (see above), then start the app.
2. **Admin → Races → Sync race list.** Confirm the removal of the old demo races if asked.
3. **Admin → Polling snapshots → Refresh odds now**, with "Use for scoring" ticked.
4. **Admin → Prediction deadline:** set it, e.g. **Nov 3, 6:00 PM ET**, before the first polls close. *(A database created earlier keeps its old deadline until you change it.)*
5. **Admin → Participants:** create an invite for each person and send them the codes.
6. Shortly before the deadline, **Refresh odds now** again with "Use for scoring" ticked, so points reflect the final odds.
7. After the deadline, set **auto-refresh to about 5 minutes**. The scoreboard projections will then follow the live markets.
8. As races are called, open **Admin → Election results**, tick the confirmation box and **Record selected results**. Check calls against AP if you like.
9. **House total:** enter it manually once known. Until then it counts toward projected points only.

## Run locally on macOS

```bash
cd ~/Documents/Code/HKElectionPrediction
python3.13 -m venv .venv            # first time only
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # first time; then fill it in
pytest                              # offline; uses saved API responses in tests/fixtures
streamlit run app.py                # http://localhost:8501
```

## Changing races

Edit [race_catalog.py](race_catalog.py). Each candidate needs a label like `"Name (D)"` and match strings that identify its Polymarket market. Find a race's slug in its Polymarket URL, e.g. `polymarket.com/event/texas-senate-election-winner`. Then use **Admin → Races → Sync race list**.

## Deploying (Streamlit Community Cloud)

Push to GitHub, create the app at share.streamlit.io, and paste your `secrets.toml` contents into the app's **Secrets** settings. Auto-refresh only runs while someone has the site open.
