# Jefferson PDF recent bonds (72h)

This utility extracts inmates from Jefferson County's CURRENTINMATES.PDF who were booked within the last 72 hours and have a positive bond amount.

Source PDF

- https://jeffersoncountytx.gov/Sheriff/content/documents/inmate/CURRENTINMATES.PDF
- The file appears to refresh at least daily (often twice).

## What it does

- Downloads or reads a local CURRENTINMATES.PDF
- Parses names, optional booking datetime, DOB, and bond amount from text
- Filters to records with booking datetime within N hours (default 72) and bond_total > 0
- Prints JSONL to stdout and can upsert into Mongo `simple_jefferson`

## Requirements

Add to your virtualenv:

- pdfminer.six
- python-dateutil
- pymongo (only if using `--mongo`)
- python-dotenv (for `.env`-based MONGO settings)

These are already listed in `requirements.txt` (pdfminer.six and python-dateutil). Install with:

```
pip install -r requirements.txt
```

Ensure your `.env` at repo root has:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=warrantdb
```

## Usage

Run against the live URL:

```
python -m scripts.jefferson_pdf_recent_bonds --pdf https://jeffersoncountytx.gov/Sheriff/content/documents/inmate/CURRENTINMATES.PDF --hours 72 --output-jsonl debug/jefferson/recent_bonds.jsonl
```

Optionally write to Mongo:

```
python -m scripts.jefferson_pdf_recent_bonds --pdf https://jeffersoncountytx.gov/Sheriff/content/documents/inmate/CURRENTINMATES.PDF --hours 72 --mongo
```

Read from a saved local copy:

```
python -m scripts.jefferson_pdf_recent_bonds --pdf rosters/jefferson-county/CURRENTINMATES_2025-11-02.pdf
```

## Output

- Each JSON line contains: `first`, `last`, `full_name`, `booking_dt` (ISO8601 UTC if present), `bond_total`, `dob` (YYYY-MM-DD if present), and the original `line`.
- If `booking_dt` isn’t present in a row, it’s skipped when enforcing the recency window.

## Caveats and tuning

- PDF layouts can drift. If the county changes the header order or labels, adjust the regexes in `scripts/jefferson_pdf_recent_bonds.py`:
  - `NAME_LINE_RE` for name recognition
  - `BOOKING_RE` for booking date/time
  - `BOND_RE` / `MONEY_RE` for bond amounts
- Time zone: booking times are assumed local; we normalize to UTC. If you observe consistent offsets, add explicit Central Time handling.
- Duplicates: we de-duplicate lines by `(first, last, booking_dt)`.

## Automating

Add a cron or scheduled job that runs hourly and writes JSONL + optional Mongo upserts. Since the PDF refreshes daily/bi-daily, an hourly poll is sufficient and ensures we catch the last 72h window reliably.
