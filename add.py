"""
add.py - add a company to the tracker by name.

    python add.py "BP"

Also what the "Add a company" button in the Actions tab runs. All the fetching
and extracting lives in track.py - this only works out which URL to use.
"""

import json, re, sys
from pathlib import Path

import track

HERE = Path(__file__).parent
COMPANIES = HERE / "companies.json"

GUESS_PROMPT = """Give the pages most likely to LIST current vacancies for
graduates, interns and industrial placements at the UK arm of the employer
below.

Prefer their job search or vacancy listing page, and their early careers or
graduate pages. Include the careers home page as a fallback, since we follow
links from whatever we can open. Only URLs you are reasonably confident exist.

Return ONLY a JSON array of up to 4 full URLs, best first. No other text.

EMPLOYER: {name}
"""


def guess_urls(name):
    """Ask the model where this employer advertises its early careers roles."""
    r = track.client.messages.create(
        model="claude-haiku-4-5", max_tokens=300,
        messages=[{"role": "user", "content": GUESS_PROMPT.format(name=name)}],
    )
    raw = "".join(b.text for b in r.content if b.type == "text")
    m = re.search(r"\[.*\]", raw, re.S)
    urls = json.loads(m.group(0)) if m else []
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]


def try_url(name, url):
    """Find a page that really lists roles.

    Returns (roles, js, working_url), or (None, None, None). Tries plain http
    first and a real browser second, so the js flag is measured, not guessed.
    """
    html = None
    for js in (False, True):
        try:
            html = track.get_html(url, js)
        except Exception as e:
            print(f"    cannot open (js={js}): {type(e).__name__}")
            continue
        roles = track.find_roles(name, url, js, html)
        if roles:
            return roles, js, url

    if html is None:
        return None, None, None

    # the page may only describe the schemes - follow its links, same as a run
    for cand in track.candidate_links(url, html):
        print(f"    no roles there - trying {cand}")
        for js in (False, True):
            try:
                roles = track.find_roles(name, cand, js)
            except Exception:
                continue
            if roles:
                return roles, js, cand
    return None, None, None


def main():
    if len(sys.argv) < 2 or not " ".join(sys.argv[1:]).strip():
        sys.exit('usage: python add.py "Company Name"')
    name = " ".join(sys.argv[1:]).strip()

    track.client = track.make_client()
    companies = json.loads(COMPANIES.read_text(encoding="utf-8"))

    if any(c["name"].strip().lower() == name.lower() for c in companies):
        sys.exit(f"{name} is already being tracked - nothing to do")

    print(f"looking for {name}")
    urls = guess_urls(name)
    if not urls:
        sys.exit(f"could not work out a careers page for {name} - nothing added")

    roles = js = working = None
    for u in urls:
        print(f"  trying {u}")
        roles, js, working = try_url(name, u)
        if roles:
            break

    if not roles:
        sys.exit(f"no page for {name} listed any graduate, placement or "
                 f"internship roles - nothing added")

    companies.append({"name": name, "url": working, "js": js})
    COMPANIES.write_text(json.dumps(companies, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print()
    print(f"added {name}")
    print(f"   page : {working}")
    print(f"   js   : {js}")
    print(f"   found {len(roles)} roles, for example:")
    for r in roles[:5]:
        print(f"      {r.get('title')}  [{r.get('kind')}]")

    print()
    print("now running the normal scrape so it lands in jobs.json")
    track.main()


if __name__ == "__main__":
    main()
