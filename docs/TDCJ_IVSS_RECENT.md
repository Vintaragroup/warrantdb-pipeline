# TDCJ IVSS Counties – Recent Intakes (last N hours)

This utility scrapes the IVSS Counties portal (https://ivss-counties.tdcj.texas.gov) and returns offenders whose latest Intake Date falls within a configurable window (default 72 hours). It uses a hybrid approach:

- Selenium (headless Chrome) to drive the site’s search UI by first-name initial (A..Z)
- HTTPX + cookies from Selenium to fetch offender detail pages quickly
- BeautifulSoup to parse the detail page and the subgrid containing Intake Date, Release Date, and Facility

Results print as JSON Lines to stdout; optionally upsert into MongoDB with dynamic collection names per location (city or facility).

## Install

Ensure requirements are installed (adds selenium and webdriver-manager):

- requirements.txt includes:
  - selenium
  - webdriver-manager
  - httpx, beautifulsoup4, lxml, python-dateutil

## Usage

Run from the repository root (or any directory) with Python:

```bash
python3 -m scripts.tdcj_ivss_recent_intakes --help
```

Common runs:

```bash
# Default: 72h window, letters a..z, headless, JSONL to stdout
python3 -m scripts.tdcj_ivss_recent_intakes

# Narrow letters for testing (e.g., only A and B)
python3 -m scripts.tdcj_ivss_recent_intakes --letters ab --verbose

# Expand a range form
python3 -m scripts.tdcj_ivss_recent_intakes --letters a-f --window-hours 48

# Upsert into Mongo with dynamic collections
# Collection chosen per record: simple_{slug(city || facility)}
python3 -m scripts.tdcj_ivss_recent_intakes --mongo-upsert --verbose

# Customize collection prefix
python3 -m scripts.tdcj_ivss_recent_intakes --mongo-upsert --collection-prefix simple_tx_
```

Notes:

- First-time run downloads a compatible ChromeDriver automatically.
- Headless mode is on by default; add `--no-headless` if you want to watch the browser.
- The site is heavily JS-driven; if HTTP fetch of detail fails due to state, the scraper falls back to Selenium for that record.

## Output schema (per line JSON)

- source: "ivss-counties"
- offender_id: GUID from `iicid` (if present)
- name
- tdcj_id
- state_id
- custody_status
- custody_status_date (string as shown on the page)
- latest_intake_date (ISO 8601)
- release_date (ISO 8601 or null)
- facility (from the subgrid)
- location_name (top-of-page "Location")
- location_type (e.g., Parole Office, County Jail)
- city
- state (usually "Texas")
- detail_url
- fetched_at (ISO UTC timestamp)

## Filtering and scope

- The scraper picks the maximum (most recent) Intake Date from the subgrid and compares it to `now - window_hours`.
- If no Intake Date rows exist or parsing fails, the record is skipped.

## Mongo upsert behavior

When `--mongo-upsert` is provided, each record is upserted into a collection chosen by:

- Preferred key: city (e.g., LONGVIEW) → `simple_longview`
- Fallback: facility (e.g., Texas Department of Criminal Justice) → `simple_texas_department_of_criminal_justice`
- Final fallback: `simple_tx`

You can change the prefix with `--collection-prefix`.

Upsert key: `{ source, offender_id, tdcj_id }` with `$set` of the entire record payload.

## Operational tips

- If the site layout changes, tune the selectors near the top of `scripts/tdcj_ivss_recent_intakes.py`.
- To reduce load and runtime, pass a subset of letters (e.g., `--letters abc`) and run in multiple shards.
- Add a small `--delay` (default 0.5s) between steps to be polite to the server.

## Integration

Downstream, you can point the enrichment service or dashboards at the dynamically created `simple_*` collections per city/facility to surface new recent-booking candidates.
