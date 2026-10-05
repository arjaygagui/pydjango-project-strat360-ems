# STRAT360 Municipal EMS — Python (Django)

A Django port of the municipal EMS:
**choose a city (all 84 provinces) → city analytics → voters list → voter profile (+ machinery)**.

- **Framework:** Django 6 + mysqlclient (same driver as the other pydjango projects)
- **Databases** (same split as the PHP apps — `cvl_national` read-only, all transactions in `generic_360_db`):

  | Alias | Database | Access | Holds |
  |---|---|---|---|
  | `rds` | `cvl_national` (RDS) | **read-only** — MySQL session is `transaction_read_only`, so the server rejects writes | the voter roll (`cvl_<province>`) |
  | `ext` | `generic_360_db` (RDS) | read/write | political machinery (one table per EMS level, see below) + positions + audit trail (`ems_political_role`, `ems_voter_audit`); the national machinery `ems_voter_political` is **shared with CVL-NATIONAL** |
  | `default` | local SQLite (`strat360.db`) | read/write | Django logins + sessions only |

- **No Django models on RDS.** `rds` and `ext` are used with raw SQL only; `strat360/routers.py`
  only ever migrates `default`, so Django never creates or alters an RDS table.
- **Secrets:** the Django secret key and RDS credentials live in `.env` (next to `manage.py`),
  not in `settings.py`. `.env` is git-ignored — never share it.
- **Login required:** every page and API needs a signed-in user (`LoginRequiredMiddleware`).
  There are no default accounts — create one with `manage.py createsuperuser`, or reset one
  with `manage.py changepassword <username>`. Sign-ins last 8 hours.

## Run it

```powershell
.\run.ps1
```

Then open <http://127.0.0.1:8000/> and sign in.

**Putting it online:** see [DEPLOY.md](DEPLOY.md), which covers hosting options, least-privilege
database accounts, production `.env`, gunicorn/nginx/HTTPS and the post-launch checklist. Every
setting is listed in [.env.example](.env.example). Production mode is `DJANGO_DEBUG=False`, which
gives HTTPS-only cookies, HSTS and no debug pages. Sign-in locks for 15 minutes after 5 failed
attempts for a username, or 20 from one IP. `/healthz` is an unauthenticated uptime check.

## Levels: landing page + Province-Wide EMS

After signing in, `/` is the **landing page** (the STRAT360-EMS `landing.php` design), where you
choose the EMS level: National, Province-Wide, City/Municipal or Barangay. Levels that aren't built
yet show "Coming soon". **Switch Mode** in the sidebar returns here.

**Province-Wide** is the `provincial` app, at `/province/`:
- **Picker:** region → province (any of the 84).
- **Dashboard:** in the STRAT360-PROVINCE layout, ranked by city/municipality, including a Data
  Summary drill-down.
- **Drill-down:** each municipality opens its barangay breakdown, with **Open in Voters List**
  (the province Voters List for that city).
- **Voters List** (`/province/voters/`): the city Voters List (same template, same filters), plus a
  **City / Municipality** filter. Barangay and precinct unlock once a city is chosen. **View** opens
  the voter's profile in their city's EMS. It stays about 1 s even on NCR (7.5M voters):
  - Totals come from the summary.
  - Province-wide name search uses the roll's full-text index, which matches whole words or the
    start of words. With a city chosen, names match any part, like the city list.
  - EMS filters start from the small generic_360_db tables.
  - Counts for "without card" and "Active" are taken from the summary, not from a province scan.
- **Smart Card Holders** (`/province/cards/`): the city page's template in the layout of
  STRAT360-EMS `smart-card.php`:
  - KPIs, a holders-per-city chart, and city rankings. Clicking a city opens its barangay
    breakdown, with **Show these cardholders**.
  - Services by category (chart + list), and the directory with City → Barangay filters.
  - Status changes run in the card's own city scope, with the same checks and audit trail.
  - **Issue Card** (`/province/cards/new/`) is the city form, with a province-wide surname search.
    The card is saved under the cardholder's own city, with the same one-card rule, numbering
    and audit.
  - The city page gained the same two charts (per barangay, services).
- **Social Services** (`/province/social/`): one template shared with the city page, in the
  STRAT360 `social-services.php` layout:
  - KPIs: total requests, ₱ requested, ₱ released, and release rate (₱ released / ₱ requested).
  - A requests-vs-₱ chart per area, rankings with release-rate pills, and a drill-down. The
    province goes city → barangays; the city goes barangay → puroks, as in CALOOCAN-EMS.
  - Assistance-type charts, and the request directory with City → Barangay filters.
  - Status changes run in the record's own city scope.
  - **Record Service** (`/province/social/new/`) is the full application form, for any voter in
    the province, found through `/province/api/search-voters/` (surname prefix on idx_fullname).
    The record is saved under the beneficiary's own city, so that city's EMS shows it too.
- **Quick Count** (`/province/quick-count/`): the city page's template.
  - Turnout is per city / municipality. Each city is summed from its barangays exactly as its own
    Quick Count page does it, so the province always equals the sum of its city pages.
  - Also shows cardholder scans across the province, and the top precincts with a City column.
  - Attendance is simulated, as on the city page.
  - Top precincts come from `generic_360_db.muni_roll_precincts`, per-precinct totals that
    `build_roll_summary` now writes in the same scan as the barangay totals. A live province-wide
    GROUP BY precinct takes ~20 s on NCR.
  - Precincts over 1,000 voters (the COMELEC clustered-precinct cap) are left out of the ranking at
    both levels. They are roll quirks; e.g. Pulilan's `0087D` spans 12 barangays with up to 8,867 voters.
- **Heat Map** (`/province/heat-map/`): the city heat map's template with one pin per city / municipality.
  - Each town shows voters, smart cards, supporters, beneficiaries, sectors, households and EMS
    activity, from the same modules grouped by city. A town's numbers equal its own city heat map.
  - The detail panel links to the province Voters List for that town.
  - Town pins are OpenStreetMap town centres, stored once in `muni_barangay_geo` (barangay = ''):
    ```powershell
    venv\Scripts\python.exe manage.py geocode_cities --province bulacan   # ~2 s per town; or "Locate cities" (staff)
    ```
    Districts that share one city (Caloocan City 1st–3rd) are fanned out around its centre and
    marked approximate. `TOWN_ALIASES` in `geo.py` fixes misspelt roll names for the lookup only
    (e.g. `CALUMPT` → Calumpit).
- **AI Analytics** (`/province/ai-analytics/`): the city page with province quick prompts.
  - Gemini gets `ai.province_snapshot()`: per-city aggregates (voters, machinery, cards, social
    services, sectors, households, 30-day activity) plus pre-computed rankings. No names or records.
  - The system prompt is shared, with the unit swapped (barangay ↔ city/municipality).
  - Rankings at both levels leave out zero values, so an all-zero metric never names a "leader".
- **Transaction List** (`/province/transactions/`): the city page's audit trail (`ems_voter_audit`) for
  every voter in the province, with a City / Municipality filter and column. The CSV export adds the city.
  A city filtered here matches that city's own Transaction List.
- **Data:** it reuses every municipal data module (machinery, smart cards, social services,
  sectors), grouped by municipality. City-level data rolls up automatically.

Voter totals per municipality and barangay come from a **pre-computed summary**,
`generic_360_db.muni_roll_summary`, because counting a whole province live takes up to 30 s (NCR).

```powershell
venv\Scripts\python.exe manage.py build_roll_summary --all --missing    # first time (~10 min, read-only)
venv\Scripts\python.exe manage.py build_roll_summary --province bulacan # after the roll changes
```

A province without a summary shows a "prepare" screen, where staff can build it with one click.

**Nationwide** is the `national` app, at `/national/` (the landing page's National card):
- **Dashboard:** the province dashboard one level up. All 84 provinces are ranked flat, with a
  Region filter. KPIs, charts and supporter/cardholder rankings are per province.
- **Drill-down:** each province opens its city / municipality breakdown, with **Open in Voters List**
  (the national Voters List for that province).
- **Data:** voter totals come from `muni_roll_summary` for the whole country (67,842,618 voters),
  cached 10 min. EMS numbers are grouped by province (`national/data.py`).
- **Voters List** (`/national/voters/`): pick a region or province first. Browsing all 67.8M
  voters at once isn't offered.
  - **With a region:** its provinces, plus a region-wide name search. Each province is searched in
    parallel with `provincial.voters.name_search`, about 0.7 s even for "Santos" in Region III
    (224k matches). The page shows per-province match counts and the first 50 A–Z.
  - **With a province:** the full province Voters List (every filter, City → Barangay), with
    Region / Province selectors in its header. **View** switches to the voter's province and city.
- **Smart Card Holders** (`/national/cards/`): the shared cards template (`nat=True`).
  - Province rankings with a city drill-down, and KPIs / services for the
    country or a region.
  - The directory has Region → Province → City → Barangay filters. Without a province, names come
    from each card's own roll (`voter_briefs`), so the search is by card number. With a province it
    also searches surnames.
  - Status changes and **Issue Card** (pick the province, then the voter) run in the card's own city.
- **Social Services** (`/national/social/`): the shared social template (`nat=True`), with province
  rankings → cities, the directory with Region → Province → City → Barangay filters, and **Record
  Service** (pick the province, then the beneficiary).
- **Quick Count** (`/national/quick-count/`): the shared Quick Count template (`nat=True`), for the
  country or one region (Region filter).
  - Turnout per province. Each province is summed from its cities exactly as the province page does
    it (`quickcount.province_areas`), so a province's row equals its Province-Wide Quick Count.
  - Top precincts across the country / region come from `muni_roll_precincts` in one query
    (`rollsummary.largest_precincts_in`, ~0.3 s, cached 6 h), with Province and City columns.
  - Cardholder scans cover every province; names come from each card's own roll (`voter_briefs`).
  - Every voter link opens the Nationwide profile (`/national/voters/<slug>/<id>/`).
  - Precinct codes that are spreadsheet error values on the roll (e.g. `#REF!` in Davao de Oro,
    10 "precincts" / 11,349 voters) are left out of the precinct rankings at every level.
- **Heat Map** (`/national/heat-map/`): the shared heat map template (`nat=True`) with one pin per
  province, for the country or one region (Region filter, kept by the export link).
  - Per province: voters, cards, national machinery, beneficiaries, sectors (from `national/data.py`)
    plus social services, households and EMS activity (`heatmap.py` grouped by `province_slug`).
    A province's numbers equal its Province-Wide heat map, except machinery, which is each level's own.
  - The activity feed and counts keep only national machinery entries (`audit_level_sql('nat')`); names
    come from each entry's own roll. Voter links open the Nationwide profile.
  - Province pins are OpenStreetMap province centres (looked up as admin-level "states", province
    names only), stored in `muni_barangay_geo` with municipality = '' and barangay = '':
    ```powershell
    venv\Scripts\python.exe manage.py geocode_provinces    # ~2 min for all 84; or "Locate provinces" (staff)
    ```
    `PROVINCE_SEARCH` maps roll names to OSM names (NCR → Metro Manila, Western Samar → Samar,
    North Cotabato → Cotabato). SGA (BARMM's Special Geographic Area) has no boundary of its own and is
    placed inside Cotabato around Pikit, marked approximate.
  - Per-level unit names and links (Voters List, Locate, Transaction List) come from `views.HEAT_UNITS`.
- **AI Analytics** (`/national/ai-analytics/`): the shared AI page (`nat=True`) with national quick
  prompts and an "Analyse" selector (all regions or one region).
  - Gemini gets `ai.national_snapshot()`: per-province aggregates (voters, national machinery, cards,
    social services, sectors, households, 30-day activity) plus rankings. No names or records.
  - The province and national snapshots share `ai._rollup_snapshot()`; the system prompt's unit is
    "province" (`ai.LEVELS['national']`).
- **Transaction List** (`/national/transactions/`): the shared audit-trail page (`nat=True`) with
  Region → Province → City filters.
  - Without a province: every province's entries, with Province and City columns. Names and places come
    from each entry's own roll (`voter_briefs`), so the search is by TX ID or detail text.
  - With a province: that province's list, joined to its roll (surname search, City filter). The scope
    carries `'level': 'nat'`, so it still keeps only national machinery entries.
  - The CSV export adds Province / City columns; paging, export and Reset keep the region and province.

**Barangay** is the `barangay` app, at `/barangay/` (the landing page's Barangay card). It covers one
barangay of one city, for barangay-level clients. The PHP BARANGAY-EMS it is modelled on is
entirely sample data (a made-up "San Roque, Cebu City" with ten puroks, a Committed / Undecided /
Inactive sentiment tag and leader recruitment targets); here every number is real:
- **Choose a barangay** (`/barangay/select/`): Region → Province → City → Barangay, with voter counts.
  The scope (`barangay/data.py`, `bscope`) is a city scope plus `barangay` (display name) and
  `barangays` (its raw roll spellings), so the municipal modules accept it.
- **Purok** — the roll has no purok field. A voter's purok is the **Purok / sitio** saved on their
  Personal Details (`muni_voter_details.purok`, added by `household_setup`), else one named in their
  roll address (`household.purok_in_address`: "PUROK 3", "PRK. 1B", "PUROK PAG-ASA" → "Purok 3",
  "Purok 1B", "Purok Pag Asa"), else **Unspecified**. Typed puroks are normalised the same way
  (`purok_label`: "prk 7" → "Purok 7"), and the profile suggests the address one. Addresses name a
  purok for only ~5–25% of voters (Pulilan 5%, Malolos 13%, Bustos 25%), so purok views fill in
  as staff tag voters; every purok view also shows the same breakdown by **precinct** (real).
- **Machinery**: its own table `brgy_voter_political`, positions Barangay Coordinator (top) →
  Supporter, plus Opposition. **Sentiment** is derived from it: Committed = supporters +
  coordinators, Opposition, Untagged = everyone else.
- **Barangay Analytics** (`/barangay/`): KPIs, voters vs. committed per purok, the sentiment
  doughnut, sentiment by purok, purok rankings (click → that purok's voters), the same per
  precinct, top leaders and sectors.
- **Voters List** (`/barangay/voters/`): the PHP "Tagged Supporter Roster" over the real roll —
  name, purok, precinct, sentiment, position, coordinator and card, with filters for each plus
  sector. Puroks read from an address are marked "(address)" until saved.
- **Puroks & Precincts** (`/barangay/puroks/`) and **Leaders** (`/barangay/leaders/`: each
  coordinator's supporters, cardholders among them and puroks reached).
- **Smart Card Holders** / **Social Services** (+ Issue Card / Record Service): the city pages
  limited to the barangay (`smartcard._area` / `social._area` add the barangay), ranked by purok;
  social services rank by the record's own purok, drilling into assistance types.
- **Quick Count** (`/barangay/quick-count/`): turnout per precinct. The barangay's check-ins are the
  same as its row on the city's Quick Count and are shared out over its precincts by each precinct's
  own factor (`quickcount.precinct_areas`), so they add up exactly; cardholder scans are the barangay's.
- **Heat Map** (`/barangay/heat-map/`): one pin for the barangay (its stored OpenStreetMap centre,
  staff can "Locate barangay") and its puroks as the ranked heat list with the detail panel —
  puroks and polling places have no coordinates anywhere. CSV export per purok.
- **AI Analytics** (`/barangay/ai-analytics/`): Gemini gets per-purok and per-precinct aggregates
  (`barangay.views.snapshot` → `ai._rollup_snapshot`, unit "purok"), the derived sentiment and a note
  on how many voters have a purok. No names or records.
- **Transaction List** (`/barangay/transactions/`): the city list limited to the barangay's voters
  (`transactions._area`), keeping only the Barangay EMS's own machinery entries (`meta.level = 'brgy'`).
- Speed: the barangay's roll is read once (ids, precincts, and only the addresses that may name a
  purok) and cached 6 h — ~1 s cold even for Batasan Hills, QC (85,637 voters).

### Each level is its own product
The City/Municipal EMS (LGUs), Province-Wide EMS and Nationwide EMS (party lists / national
positions) go to different clients. A voter profile opened from a level stays in that level:
`/voters/<id>/`, `/province/voters/<id>/` and `/national/voters/<slug>/<id>/` are the same views
(`municipal/views.py`, `level=` in the URL conf). The city is taken from the voter's roll row.
Every action on the profile (personal details, household, machinery, card, social service) posts
to that level's URLs and comes back to the same profile. The routes are in `PROFILE_ROUTES`.
Drill-downs never switch products either: they open the same level's pages filtered to that
city or province.

**Positions per level** (`LEVEL_ROLE_CODES`):
- **Barangay:** barangay coordinator, supporter, opposition.
- **City:** municipal coordinator, barangay coordinator, supporter, opposition.
- **Province:** adds provincial coordinator.
- **National:** adds regional and provincial coordinators.

**Each level keeps its own machinery.** A voter can be a Provincial Coordinator for the
provincial client and a Municipal Coordinator for the LGU at the same time, and neither
overwrites the other (see *Political machinery* below). A level shows only its own machinery:
no roll-up of lower levels' positions. Smart cards, social services, personal details,
households and the audit table stay shared.

Each level's top position takes no superior there. Downline searches follow the coordinator's
rank: region (1), province (2), city (3), barangay (4). A regional coordinator's downlines can be
in other provinces of the region, so picks are sent as `slug:id`.

## Political machinery (one per EMS level)

| Level | Table (generic_360_db) | Notes |
|---|---|---|
| Barangay | `brgy_voter_political` | created by `machinery_setup` |
| City / Municipal | `muni_voter_political` | created by `machinery_setup` |
| Province-Wide | `prov_voter_political` | created by `machinery_setup` |
| Nationwide | `ems_voter_political` | the existing table, shared with CVL-NATIONAL |

The barangay, city and province tables are created `LIKE ems_voter_political` (same columns, keys and
collation) plus the foreign key to `ems_political_role`. Every function in `machinery.py` takes
the `level` and uses `machinery.TABLES[level]` (a whitelist). Audit rows written for machinery carry
`meta.level`, and each level's activity, Transaction List, heat map and AI numbers keep only their
own machinery entries (`audit_level_sql`). Older entries without `meta.level` count as the City
EMS's if they came from the demo seed (`meta.seed = 'demo'`), otherwise as the national one's.

```powershell
venv\Scripts\python.exe manage.py machinery_setup                   # create the per-level tables
venv\Scripts\python.exe manage.py machinery_setup --move-city-mock  # one-off: move the City demo rows out of ems_voter_political
```

`municipal/machinery.py` is a port of CVL-NATIONAL's `api/political.php`, with the same rules at every level:

- Ranks: Regional (1) → Provincial (2) → City/Municipal (3) → Barangay (4) → Supporter (5);
  Opposition is a tag with no rank, superior or downlines.
- One position per voter per level; a downline must sit at a lower rank than its superior; loops are refused.
- Downline areas: a city coordinator's downlines come from the same city, a barangay
  coordinator's from the same barangay (checked on the server too, not only by the search).
  In the Barangay EMS every search and downline stays in its barangay.
- Every change writes `ems_voter_audit` rows (`political.assign`, `.downline_add`, `.upline_set`,
  `.downline_remove`, `.upline_clear`, `.unassign`) in the same transaction, with the signed-in
  username as the actor. The profile's **Activity** card shows this trail.
- Positions offered: see *Positions per level* above.

Profile actions: **Assign / Change Position**, **Add &lt;role&gt;s**, **Set/Change superior**,
**Detach** a downline (×), **Remove position** — all posted to `voters/<id>/political/`.

## Social services (Caloocan-EMS format)

Table **`generic_360_db.muni_social_services`** — same columns as CALOOCAN-EMS's
`caloocan_ems_db.social_services` (`voter_id`, `assistance_type`, `claimant`, `beneficiary`,
`barangay`, `amount`, `status`, `created_at`), plus `province_slug` + `municipality` (whose
record it is), `created_by`, and `is_mock` (seeded demo rows).

- Assistance types: Medical, Financial, Livelihood, Educational, Burial, Senior Citizen Support,
  Food Pack / Relief. Statuses: Pending, Approved, Released (default), Rejected.
- Every record / status change writes an `ems_voter_audit` row on the beneficiary, worded like
  Caloocan's activity log: `Recorded social service: <type> (PHP 12,345.00)`.
- Pages: **Social Services** (`/social/` — KPIs, filters, list, change status), **Record Service**
  (`/social/new/`, or `?voter=<id>` from a profile), and a card on each voter profile.

```powershell
venv\Scripts\python.exe manage.py social_setup                                  # create the table (idempotent)
venv\Scripts\python.exe manage.py seed_social_mock --province bulacan --city BUSTOS --count 100
venv\Scripts\python.exe manage.py clear_social_mock [--province bulacan --city BUSTOS]   # remove mock rows only
```

Mock rows (`is_mock = 1`) and their activity entries (`meta.mock = true`) are removed by
`clear_social_mock`; real records are never touched. CVL-NATIONAL's own
`ems_social_service_request` tables are left to that app.

## Smart cards (Caloocan-EMS format)

Table **`generic_360_db.muni_smart_cards`** has the same columns as CALOOCAN-EMS's
`caloocan_ems_db.smart_cards` (`voter_id`, `card_number`, `status`, `civil_status`, `gender`,
`service`, `barangay`, `issued_date`, `created_at`). It adds `province_slug` + `municipality`,
`created_by` and `is_mock`.

- One card per voter. The card number is `SC-<yy>-<6-digit serial>`, e.g. `SC-26-000301`.
- Status is `active` / `pending` (Caloocan), plus `revoked` so a card can be withdrawn.
  Revoked cards don't count as cardholders.
- Issuing a card or changing its status writes `card.issue` / `card.status` to `ems_voter_audit`.
- Pages and features:
  - **Smart Card Holders** (`/cards/`): KPIs, barangay ranking, filters, list, and status changes.
  - **Issue Card** (`/cards/new/`, or `?voter=<id>`).
  - A card panel and a "Cardholder" badge on each voter profile.
  - A with/without-card filter and badges on the Voters List.
  - Cardholder KPI, highlights, chart series and columns on City Analytics.

```powershell
venv\Scripts\python.exe manage.py smartcard_setup                               # create the table (idempotent)
venv\Scripts\python.exe manage.py seed_card_mock --province bulacan --city BUSTOS --count 300
venv\Scripts\python.exe manage.py clear_card_mock [--province bulacan --city BUSTOS]     # remove mock cards only
```

Like Caloocan's seeded cards, mock cards have no activity entries. `clear_card_mock` removes the
mock cards and any activity written for them later.

## Personal details + household (Caloocan-EMS format)

There are two tables in `generic_360_db`, created by `manage.py household_setup` (idempotent):

- **`muni_voter_details`** holds one row per voter (`province_slug` + `voter_id`). It stores the
  voter profile's editable **Personal Details**. The columns are Caloocan's `voter_details`
  plus `civil_status`. Caloocan's free-text "opposition" field is left out, because the Political
  Position card's Opposition tag already covers it.
  - Blank fields are pre-filled from the voter's smart card and newest social-service application.
    Each pre-filled field says where it came from, and saving stores it.
  - **Purok / sitio** (`purok`, nullable) groups voters in the Barangay EMS. It is suggested from
    the roll address and normalised on save ("prk 7" → "Purok 7").
- **`muni_household_members`** follows Caloocan's `household_members` and lists the people under a
  voter, who becomes the household head.
  - A member is either a registered voter of the same city, linked by `member_voter_id` (name and
    precinct come from the roll and link to their profile), or someone not on the roll, typed in
    by hand (e.g. a minor).
  - A registered voter can be listed in only one household.
  - Profiles show a "Household Head" or "Household Member" badge.

**Sectors** are stored in **`muni_voter_sectors`**, one row per voter per sector, since a voter
can belong to several. They are ticked in Personal Details on the profile and saved as a whole set.

- The sector list is PHP's dashboard six (Senior Citizen, Single Parent, Youth, PWD, OFW, TODA)
  plus a generic version of PHP's `ems.php` sector filter list.
- They feed the Voters List's Sector filter and column, the Sectoral Demographics chart, and City
  Analytics' Sectoral Overview table.

Saving details and adding/removing members write `details.update` and
`household.member_add` / `household.member_remove` to `ems_voter_audit`. A member added from the
roll also gets an entry on their own profile.

## Transaction List (real audit trail)

**Transaction List** (`/transactions/`, sidebar → Audit) replaces the PHP pages, which generate
mock rows. It reads `generic_360_db.ems_voter_audit` for the voters on the chosen city's roll,
joined read-only to `cvl_national.cvl_<slug>`, so it also includes changes made in CVL-NATIONAL.
It adds no tables and writes nothing.

- **KPIs:** today (Manila time), last 7 days, all transactions (with a per-module breakdown), and
  the number of encoders.
- **Filters:** search (TX ID such as `TX-000215`, voter surname, or detail text), action (a whole
  module or one action), encoder, and a date range in Manila dates. 25 per page.
- **Status:** derived from each entry's recorded outcome, e.g. card Active/Pending/Revoked, social
  service Released/Approved/Pending/Rejected, or Removed/Done.
- **Export CSV:** exports the current filters, up to 20,000 rows, as UTF-8 so Excel shows Ñ and ₱.
- **Mock entries:** entries written by the mock seeders are badged "mock".

All generic_360_db timestamps are stored in UTC. Rows read through `machinery._rows` are marked
UTC, so every page shows Manila time.

## Quick Count (voting day · live attendance)

**Quick Count** (`/quick-count/`) is a port of CALOOCAN-EMS `quick-count.php`. It is read-only
and writes nothing.

- **Real:**
  - Barangays, registered voters and precincts (voter roll).
  - Top precincts, each with a real sample voter (a cardholder when the precinct has one).
  - Smart cardholders: live-feed names, card numbers and barangays, plus cardholders by service.
- **Simulated:** attendance, because there is no check-in feed yet.
  - Each barangay gets a stable turnout factor from a CRC32 of its name (55–74%, Caloocan's
    formula), shown as a 12 PM snapshot of polls that opened at 6 AM.
  - Last hour, average per hour or minute, peak hour and the hourly chart are all derived from
    that, not hard-coded.
  - Flagged scans are scans of Pending cards.
  - The page is labelled "SIMULATION" and says which parts are real.
- **Differences from Caloocan:** its invented "Sectoral Turnout" (the roll has no sector data) is
  replaced by **Cardholder Turnout by Service**, and its made-up card numbers in the live feed are
  replaced by real ones.
- The precinct aggregation is cached for 6h per city.

To go live, replace `quickcount.turnout_factor` and `hourly` with real check-in counts.

## Barangay Heat Map

**Barangay Heat Map** (`/heat-map/`) uses the same layout as MUNICIPAL-EMS `heat-map.php`: 6 KPIs,
filters, a map with a metric toggle and ranked sidebar, a barangay drill-in panel, 3 charts,
rankings and an activity feed.

PHP's version is all sample data. Here every number is real, per barangay: voters, precincts,
smart cards, supporters, coordinators, opposition, social services, sectors, households and EMS
activity in the chosen period.

What changed from PHP, because there's no data for it:
- Population and budget are replaced by Supporters and Assistance Released.
- The fake purok breakdown is replaced by the barangay's top sectors.
- The "AI forecasts" section is left out.

**Locations.** No table has coordinates, so each city's barangays are looked up once, by name, on
OpenStreetMap and stored in `generic_360_db.muni_barangay_geo`.
- Only place names are sent, at 1 request per second per Nominatim's policy.
- A match more than 25 km from the town is rejected.
- Barangays that can't be found are placed near the town centre as dashed pins, labelled
  approximate.

```powershell
venv\Scripts\python.exe manage.py geocode_barangays --province bulacan --city BUSTOS   # ~2 s per barangay
```

Staff can also press **Locate barangays** on the page, which handles 25 per click. Bustos and
Basco are already located.

## AI Analytics (Google Gemini)

**AI Analytics** (`/ai-analytics/`) uses the MUNICIPAL-EMS `ai-analytics.php` layout, quick
prompts and answer format: a summary, KPI tiles, a chart, key findings and recommendations. To
turn it on, add your key to `.env` and restart:

```
GEMINI_API_KEY=your-key-from-google-ai-studio
GEMINI_MODEL=gemini-2.5-flash        # optional
```

Without a key the page shows a "not configured" notice. How it differs from PHP:

- **Only aggregates are sent**, as a snapshot of this city's numbers per barangay and category
  (see `municipal/ai.py`). No voter names, IDs or records go to Google; PHP also sends a list of
  requester names.
- **The snapshot is for the selected city.** PHP's city page calls the provincial backend.
- **The key is kept safe:** it comes from `.env` and is sent in a request header, never in a URL
  or page. PHP hard-codes it in `includes/ai_config.php`.
- **Demo data is labelled:** the snapshot says when figures include mock records, so answers
  mention it.

## User Profile

**User Profile** (`/profile/`, sidebar → User Management) uses the PHP `profile.php` layout for
the logged-in account. PHP's version shows a fixed "City Administrator" with made-up details.

Every level has its own copy in its own shell — `/barangay/profile/`, `/profile/`, `/province/profile/`,
`/national/profile/` (`views.PROFILE_LEVELS`): its deployment and scope (barangay / city / province /
country), a password form that returns to the same page, My Activity links into that level's
Transaction List, and Permissions & Modules listing that level's pages only. Each level's
`base.html` points the sidebar's User Profile link at its own copy (`{% block profile_url %}`).

- **Profile card:** initials, name, email, role (Superuser / Staff / User), username, scope,
  member since, last login, and the number of changes made.
- **Account Details:** first and last name, email and phone, saved. Phone is stored in the only
  Django model, `municipal.UserProfile`, in the local SQLite login database, since Django's `User`
  has no phone field.
- **Change Password:** Django's password rules (at least 10 characters). You stay signed in after
  changing it.
- **My Activity:** your own `ems_voter_audit` entries across all cities, counted per module, with
  the latest 10 linked to the Transaction List.
- **Permissions & Modules:** every EMS page is open to any signed-in account; User Management
  (Django Admin) needs staff.

## Demo (mock) data

Bustos, Bulacan is seeded so every page has something to show. All of it is flagged and
removable, and real records are never touched.

| What | Seeder | Flag | Remove with |
|---|---|---|---|
| 300 smart cards | `seed_card_mock` | `is_mock = 1` | `clear_card_mock` |
| 100 social services (+ activity) | `seed_social_mock` | `is_mock = 1`, `meta.mock` | `clear_social_mock` |
| Machinery: 1 municipal + 15 barangay coordinators, ~1,430 supporters, ~150 opposition | `seed_demo_mock` | `assigned_by = 'mock-seed'` | `clear_demo_mock` |
| 2,500 Personal Details (some Inactive / Transferred / Deceased) | `seed_demo_mock` | `is_mock = 1` | `clear_demo_mock` |
| 80 households, ~180 members (registered relatives + unregistered children) | `seed_demo_mock` | `is_mock = 1` | `clear_demo_mock` |
| ~2,600 sector memberships, derived from the seeded data (see below) | `seed_sector_mock` (run by `seed_demo_mock`) | `is_mock = 1` | `clear_demo_mock` |
| ~6,500 activity entries (incl. "card issued" for the mock cards) | `seed_demo_mock` | `meta.seed = 'demo'` | `clear_demo_mock` |

```powershell
venv\Scripts\python.exe manage.py seed_demo_mock  --province bulacan --city BUSTOS
venv\Scripts\python.exe manage.py clear_demo_mock --province bulacan --city BUSTOS
```

- The demo machinery lives in the City EMS's own `muni_voter_political` (CVL-NATIONAL doesn't
  see it).
- `clear_demo_mock` keeps real positions that were later placed under a mock coordinator; it only
  detaches them from that coordinator.
- Personal Details that someone later edits on a profile count as real and are kept.
- Seeding is consistent across features: about half the cardholders are supporters, Senior
  Discount cardholders are 60+, card gender and civil status carry over, and a supporter's leader
  is their barangay's coordinator.

## Map to the PHP apps

| PHP (live)                              | Django (here)                              |
|-----------------------------------------|--------------------------------------------|
| `select-city.php` + `api/muni.php`      | `views.select_city` + `views.api_municipalities` |
| `index.php` (real_data.php)             | `views.dashboard`                          |
| `ems.php`                               | `views.voters_list`                        |
| `voter-profile.php`                     | `views.voter_profile`                      |
| CVL-NATIONAL `api/political.php`        | `municipal/machinery.py` + `views.political` |
| CVL-NATIONAL `includes/audit.php`       | `machinery._audit` / `machinery.audit_for` |
| CALOOCAN-EMS `social_services` + `includes/api/add_social_service.php` | `municipal/social.py` + `views.social_*` |
| `ai-analytics.php` + `includes/ai_chat.php` | `municipal/ai.py` + `views.ai_analytics` / `api_ai` |
| `heat-map.php` (sample data)            | `municipal/heatmap.py` + `geo.py` + `views.heat_map` |
| CALOOCAN-EMS `quick-count.php`          | `municipal/quickcount.py` + `views.quick_count` |
| `transactions.php` (mock)               | `municipal/transactions.py` + `views.transactions_list` |
| CALOOCAN-EMS `smart_cards` + `smart-card.php` | `municipal/smartcard.py` + `views.cards_*` / `card_*` |
| `config/regions.php`                    | `municipal/regions.py`                     |

## Caching

Heavy voter-roll aggregates are cached in `cache/` (city list per province 24h, dashboard and
list totals per city 6h). Machinery counts are always live. Delete `cache/` to force a refresh.
