# gradtrack

Scrapes company careers pages, asks Claude to pull out the graduate / placement /
internship roles and their deadlines, and shows them in a sortable dashboard.

## Setup

```
pip install -r requirements.txt
python -m playwright install chromium
```

Then copy `.env.example` to `.env` and paste your key in:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored - it never leaves your machine. `.env.example` is the
committed placeholder so anyone cloning knows what to fill in.

## Run

```
python track.py
```

Writes `jobs.json` (the dashboard reads this) and `jobs.csv` (for Excel).

The spreadsheet columns are: Company name, Position, Date applied, Application
status, Extra details, Link, Location/locations, Deadline.

`Date applied` and `Application status` are yours to fill in. A run reads them
back out of `jobs.csv` before it starts, so a scrape refreshes the facts around
them and never wipes your progress. Close the file in Excel before running - if
it is locked the results go to `jobs.new.csv` instead.

## Filtering

`prefs.json` narrows what reaches the spreadsheet. Put words you want to keep
under a column name; leave a list empty to ignore that column. A role is shown
only if every filter you filled in matches it:

    "Extra details": ["summer"]

`jobs.json` always keeps every role, so widening a filter brings them straight
back without re-scraping.

## Dashboard

`index.html` fetches `jobs.json`, so it needs a server - opening the file
directly gives a CORS error and an empty table.

```
python -m http.server 8000
```

then http://localhost:8000

## Adding companies

Append the company name and any careers URL to `companies.json`. It does not
have to be the page that lists the jobs - if the one you give it only describes
the schemes, the run reads the links on it, asks the model which one leads to a
real vacancy list, and follows up to three of them until one yields roles.

Whatever worked is written back as a `"found"` line, and later runs go straight
there. Delete that line to make it search again. If a remembered page stops
producing roles it is dropped automatically and rediscovered.

`"js": true` opens a real browser, which most careers sites need; it defaults to
true. Set it to `false` for sites that reset the connection under headless
Chrome - EDF and Frazer-Nash both do.

## Weekly run

`.github/workflows/weekly.yml` runs the scrape every Monday 08:00 UTC and commits
the results. Push this folder to GitHub and add `ANTHROPIC_API_KEY` under
Settings > Secrets and variables > Actions.

CI does not use `.env` (it is gitignored, so it is never in the checkout) - the
secret arrives as a real environment variable and `load_dotenv` quietly does
nothing. Same code path, different key source.
