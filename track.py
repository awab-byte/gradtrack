"""
gradtrack - finds grad/placement/internship roles and pulls out the deadlines.

Run:  python track.py
Needs: pip install -r requirements.txt  +  python -m playwright install chromium
Key:   put ANTHROPIC_API_KEY in a .env file next to this script
"""

import json, os, re, csv, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from dotenv import load_dotenv

# windows consoles default to cp1252 and blow up on job titles containing
# curly quotes or fancy dashes. print utf-8 and never crash on a stray glyph.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
COMPANIES = HERE / "companies.json"
JOBS = HERE / "jobs.json"
CSV_OUT = HERE / "jobs.csv"
PREFS = HERE / "prefs.json"
# your own columns live here and nowhere else. gitignored, so publishing
# the dashboard never publishes where you have applied.
PROGRESS = HERE / "my-progress.json"

# the spreadsheet columns, in order.
# left = the heading you see in excel, right = the name used inside jobs.json
COLUMNS = [
    ("Company name",       "company"),
    ("Position",           "title"),
    ("Date applied",       "date_applied"),
    ("Application status", "status"),
    ("Extra details",      "kind"),
    ("Link",               "link"),
    ("Location/locations", "location"),
    ("Deadline",           "deadline"),
]
FIELD_OF = dict(COLUMNS)

# the columns you fill in by hand. a scrape must never overwrite these.
YOURS = ["date_applied", "status"]

# read .env sitting next to this script. no-op in github actions,
# where the key arrives as a real env var from the repo secret.
load_dotenv(HERE / ".env")

# what counts as a role you care about
KEYWORDS = ["graduate", "intern", "internship", "placement",
            "summer", "student", "year in industry", "early careers"]

def make_client():
    """Anthropic() reads ANTHROPIC_API_KEY from the environment itself -
    load_dotenv has already put the .env value there. We only check first so
    the error is readable instead of a stack trace."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        sys.exit("no key - put ANTHROPIC_API_KEY=... in your .env file and try again")
    return Anthropic()


client = None      # built in main()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}


# ---------------------------------------------------------------- fetching

def get_html(url, js=False):
    """Fetch a page, with a real browser if the site needs one."""
    if js:
        try:
            return render(url)
        except Exception as e:
            # some sites reset the connection on headless chrome - plain http
            # occasionally still gets us something usable
            print(f"  ! browser failed ({str(e)[:60]}) - trying plain http")
            return fetch(url)
    return fetch(url)


def get_text(url, js=False, html=None):
    """Strip a page down to readable text."""
    if html is None:
        html = get_html(url, js)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg"]):
        tag.decompose()

    # keep each link's url next to its text. get_text() would otherwise throw
    # every href away, which is why the model could never give a real link.
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href.startswith("http"):
            a.append(f" [{href}]")

    text = soup.get_text("\n")
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:120000]         # job boards run to ~80k chars


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def render(url):
    """For pages that build their job list with javascript."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=HEADERS["User-Agent"],
                          viewport={"width": 1400, "height": 2000})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass      # some careers sites poll forever and never go idle
            page.wait_for_timeout(2500)
            return page.content()
        finally:
            b.close()


# a broad net - anything that could plausibly be a careers link. this only
# keeps the shortlist small enough to hand to the model, which does the judging.
LINK_WORDS = ("job", "career", "graduate", "intern", "placement", "vacanc",
              "opportunit", "student", "apprentice", "search", "early")

# you want UK roles, so never follow a link that is plainly another country.
# without this the model happily picks "jobs-in-canada" and we remember it.
OTHER_COUNTRIES = ("canada", "/fr-fr", "/es-es", "/de-de", "/pt-", "/pl-",
                   "usa", "united-states", "/us/", "india", "/asia",
                   "australia", "latin-america", "middle-east", "africa",
                   "singapore", "malaysia", "philippines", "brasil")

PICK_PROMPT = """Here are links found on a company careers page.

Which of them leads to a page that LISTS actual open vacancies for graduates,
interns, industrial placements or year-in-industry students? Prefer job search
results and vacancy listings, and prefer the UK where there is a choice.

Ignore FAQs, events, blog posts, news, benefits and profile pages, and anything
that only describes the schemes without listing jobs. Never pick a home page.

If one of the links is the same job search but with a filter already applied
(something like early_careers=true, or a graduate category), prefer it - it is
the same list narrowed down to the roles that matter.

Return ONLY a JSON array of up to 3 URLs, best first. No other text.

LINKS:
{links}
"""


def candidate_links(url, html, limit=3):
    """Ask the model which links lead to the real job list.

    Lets you paste any careers page into companies.json without checking
    first whether it happens to list jobs itself.
    """
    host = urlparse(url).netloc.lower().removeprefix("www.")
    found = {}
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if not href.startswith("http") or host not in urlparse(href).netloc.lower():
            continue                       # stay on the company own site
        if href.rstrip("/") == url.rstrip("/"):
            continue                       # that is the page we just read
        label = " ".join(a.get_text(" ").split())[:70]
        low = href.lower()
        if any(bad in low for bad in OTHER_COUNTRIES):
            continue
        if any(w in (low + " " + label.lower()) for w in LINK_WORDS):
            found.setdefault(href, label)
    if not found:
        return []

    listing = chr(10).join(f"{h}  [{t}]" for h, t in list(found.items())[:60])
    try:
        r = client.messages.create(
            model="claude-haiku-4-5", max_tokens=500,
            messages=[{"role": "user",
                       "content": PICK_PROMPT.format(links=listing)}],
        )
        raw = "".join(b.text for b in r.content if b.type == "text")
        m = re.search(r"\[.*\]", raw, re.S)
        picks = json.loads(m.group(0)) if m else []
    except Exception as e:
        print(f"   ! could not rank links ({type(e).__name__}) - trying the first few")
        picks = []

    # only follow links that really were on the page
    ordered = [u for u in picks if isinstance(u, str) and u in found]
    ordered += [u for u in found if u not in ordered]
    return ordered[:limit]


# ---------------------------------------------------------------- extraction

PROMPT = """Here is the text of a company careers page. A link's URL is shown
in square brackets straight after the text it belongs to.

Pull out every open role that is a GRADUATE SCHEME, INTERNSHIP, INDUSTRIAL
PLACEMENT, SUMMER INTERNSHIP or YEAR IN INDUSTRY. Ignore experienced-hire
and contractor jobs.

Return ONLY a JSON array, no markdown, no preamble. Each object:
{{
  "employer": "",                // the company offering it, if the page lists several
  "title": "",
  "location": "",                // every location it is offered in, comma separated
  "discipline": "",              // e.g. Chemical, Mechanical, Software, Business
  "kind": "",                    // exactly one of: summer, graduate, placement, internship
  "opens": "YYYY-MM-DD or null",
  "deadline": "YYYY-MM-DD or 'rolling' or null",
  "stages": "",                  // e.g. "CV > OA > video > AC", "" if unknown
  "link": "the URL in square brackets beside this role, or null"
}}

For "link" use the URL that sits next to that specific role, so it opens the
role itself - not the listing page you are reading. If no matching roles,
return [].

PAGE TEXT:
{text}
"""


def extract(company, text, board=False):
    r = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=16000,      # a busy job board page runs to ~9k output tokens
        messages=[{"role": "user", "content": PROMPT.format(text=text)}],
    )
    if r.stop_reason == "max_tokens":
        print(f"  ! {company}: reply was cut off - some roles will be missing")
    raw = "".join(b.text for b in r.content if b.type == "text")
    # the model sometimes fences the array, and sometimes adds a sentence of
    # explanation after it. take the outermost [...] and ignore everything else.
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        roles = json.loads(m.group(0))
    except json.JSONDecodeError:
        print(f"  ! couldn't parse model output for {company}")
        return []

    for role in roles:
        # on a job board the employer differs per role. on a company careers
        # page ignore it, so the name stays exactly as you wrote it.
        employer = str(role.pop("employer", "") or "").strip()
        role["company"] = employer if (board and employer) else company
        role["seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return roles


def relevant(role):
    blob = f"{role.get('title','')} {role.get('discipline','')} {role.get('kind','')}".lower()
    return any(k in blob for k in KEYWORDS)


def find_roles(company, url, js, html=None, board=False):
    """Roles on one page, already narrowed to the ones you care about.

    Discovery uses this too, so a page only counts as "the job list" if it
    yields roles you would actually want - not just any vacancy.
    """
    return [r for r in extract(company, get_text(url, js, html), board) if relevant(r)]


# ---------------------------------------------------------------- storage

def key(role):
    return f"{role['company']}::{role.get('title','').strip().lower()}"


def merge_locations(roles):
    """Job boards list the same role once per city. Keep it as one row."""
    out = {}
    for r in roles:
        k = key(r)
        if k not in out:
            out[k] = r
            continue
        places = []
        for chunk in (out[k].get("location") or "", r.get("location") or ""):
            for place in chunk.split(","):
                place = place.strip()
                if place and place not in places:
                    places.append(place)
        out[k]["location"] = ", ".join(places)
    return list(out.values())


def load_your_edits(existing):
    """Read back whatever you have typed into the spreadsheet.

    You edit jobs.csv, not jobs.json, so for your own columns the spreadsheet
    is the truth. Without this a run would quietly wipe your progress.
    """
    if not CSV_OUT.exists():
        return 0
    found = 0
    with open(CSV_OUT, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            company = (row.get("Company name") or "").strip()
            title = (row.get("Position") or "").strip().lower()
            k = f"{company}::{title}"
            if k not in existing:
                continue
            for header, field in COLUMNS:
                if field in YOURS:
                    existing[k][field] = (row.get(header) or "").strip()
            found += 1
    return found


def load_prefs():
    """Your filters, one per spreadsheet column - see prefs.json."""
    if not PREFS.exists():
        return {}
    raw = json.loads(PREFS.read_text(encoding="utf-8"))
    return {h: [str(w).lower() for w in words]
            for h, words in raw.items()
            if not h.startswith("_") and words}


def wanted(role, prefs):
    """Keep a role only if every filter you actually set matches it."""
    for header, words in prefs.items():
        field = FIELD_OF.get(header)
        if not field:
            continue
        cell = str(role.get(field) or "").lower()
        if not any(w in cell for w in words):
            return False
    return True


def main():
    global client
    client = make_client()

    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))
    existing = json.loads(JOBS.read_text(encoding="utf-8")) if JOBS.exists() else {}

    if PROGRESS.exists():
        for k, mine in json.loads(PROGRESS.read_text(encoding="utf-8")).items():
            if k in existing:
                existing[k].update(mine)

    edited = load_your_edits(existing)
    if edited:
        print(f"read your columns back from {CSV_OUT.name} ({edited} rows)")
    prefs = load_prefs()

    new_roles = []
    discovered = False

    for c in companies:
        print(f"-> {c['name']}")
        js = c.get("js", True)
        board = c.get("board", False)     # a job board, many employers
        base = c["url"]
        start = c.get("found") or base      # a job list we found last time
        try:
            html = get_html(start, js)
        except Exception as e:
            print(f"  ! fetch failed: {e}")
            continue

        roles = find_roles(c["name"], start, js, html, board)

        # the url you gave us may only describe the schemes rather than list
        # them. follow the links that look like a real vacancy list until one
        # of them actually has jobs on it, then remember which worked.
        if not roles:
            if c.pop("found", None):
                discovered = True           # the page we remembered went stale
            if base != start:
                try:
                    html = get_html(base, js)
                except Exception as e:
                    print(f"  ! fetch failed: {e}")
                    html = None
            for cand in (candidate_links(base, html) if html else []):
                print(f"   no jobs on that page - looking at {cand}")
                try:
                    roles = find_roles(c["name"], cand, js, board=board)
                except Exception as e:
                    print(f"   ! {type(e).__name__}")
                    continue
                if roles:
                    c["found"] = cand
                    discovered = True
                    print("   that is the job list - remembering it")
                    break

        merged = merge_locations(roles)
        for role in merged:
            k = key(role)
            if k not in existing:
                new_roles.append(role)
                role["status"] = "not applied"      # you edit these by hand
                role["date_applied"] = ""
                existing[k] = role
            else:
                # refresh the facts, keep the columns you fill in yourself
                role["status"] = existing[k].get("status", "not applied")
                role["date_applied"] = existing[k].get("date_applied", "")
                existing[k] = role

        # a job board files roles under each employer, not under the source name
        print(f"   {len(merged)} roles")

    if discovered:
        COMPANIES.write_text(json.dumps(companies, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        print("saved the job lists it found into companies.json")

    # jobs.json is the published copy the dashboard reads - facts only.
    public = {k: {f: v for f, v in r.items() if f not in YOURS}
              for k, r in existing.items()}
    JOBS.write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")

    # everything you filled in yourself, kept back from the published copy
    mine = {k: {f: r.get(f, "") for f in YOURS} for k, r in existing.items()
            if r.get("date_applied") or r.get("status", "not applied") != "not applied"}
    PROGRESS.write_text(json.dumps(mine, indent=2, ensure_ascii=False), encoding="utf-8")

    # the spreadsheet. jobs.json always keeps every role - the filters in
    # prefs.json only decide how much of it reaches excel.
    rows = [r for r in sorted(existing.values(),
                              key=lambda r: (r.get("deadline") or "zzz"))
            if wanted(r, prefs)]
    if prefs:
        print(f"filters on {list(prefs)}: showing {len(rows)} of {len(existing)}")

    def write_csv(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([h for h, _ in COLUMNS])
            for r in rows:
                w.writerow(["" if r.get(fld) is None else r.get(fld)
                            for _, fld in COLUMNS])

    try:
        write_csv(CSV_OUT)
    except PermissionError:
        # nearly always jobs.csv still open in excel. the scrape already
        # succeeded - don't throw it away over a locked file.
        alt = CSV_OUT.with_name("jobs.new.csv")
        write_csv(alt)
        print()
        print(f"! {CSV_OUT.name} is open in another program (excel?) - "
              f"wrote {alt.name} instead. close it and re-run to refresh the real one.")

    if new_roles:
        print(f"\n{len(new_roles)} NEW:")
        for r in new_roles:
            print(f"  {r['company']} - {r['title']} (deadline {r.get('deadline')})")
    else:
        print("\nnothing new")


if __name__ == "__main__":
    main()
