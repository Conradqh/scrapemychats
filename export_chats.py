"""scrapemychats — export every conversation from a ChatGPT account to local disk.

For ChatGPT accounts where the built-in "export data" button is unavailable
(e.g. many business/Team workspaces). Drives a real, headed Chrome window via
Playwright using a persistent profile, so you log into ChatGPT once and every
later run is automatic. Your data never leaves your machine.

How it works: from inside the logged-in ChatGPT page, the script requests
each conversation's JSON from ChatGPT's backend with the same auth headers
the page itself uses (so it never needs your credentials). By default it
opens each chat's page first; with --no-page-load it fetches directly from
the page already open. It renders each chat to Markdown and downloads every
referenced file/image using the same session.

Usage:
    python export_chats.py                 # discover all chats, export them all
    python export_chats.py --limit 3      # small test run first (recommended)
    python export_chats.py --no-page-load # faster on large, throttled accounts
    python export_chats.py --csv my.csv   # export a specific list instead
    python export_chats.py --rediscover   # refresh chats.csv from the account
    python export_chats.py --rerender     # rebuild Markdown offline from saved JSON

The chat list is saved to chats.csv on first run and reused afterwards, so
folder numbering stays stable. Re-running skips chats already exported —
safe to interrupt and resume at any time.

Rate limits: ChatGPT throttles bulk access ("You're making requests too
quickly"). The script paces itself, backs off in escalating steps when
throttled, and permanently slows down each time it happens. A large archive
(500+ chats) takes a few hours. This is normal; let it run.
"""

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://chatgpt.com"

CONV_ID_RE = re.compile(
    r"/c/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

DELAY_RANGE = (10.0, 16.0)  # polite delay between chats, seconds
EXTRA_DELAY_PER_429 = 5.0  # permanent slowdown added each time we get throttled
EXTRA_DELAY_CAP = 40.0
NAV_TIMEOUT_MS = 45_000
MAX_ATTEMPTS = 3
# escalating waits after "too many requests"; retried without counting as failure
RATE_LIMIT_BACKOFFS_S = [300, 600, 900, 1800, 1800]
LONG_BREAK_EVERY = 20  # chats
LONG_BREAK_S = (150, 240)


def log(msg):
    print(msg, flush=True)


def sanitize(name, max_len=60):
    """Make a string safe for a file/folder name on any OS."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().strip(". ")
    return name[:max_len].strip() or "untitled"


# ------------------------------------------------------------------ session


def session_state(page):
    """Return the /api/auth/session JSON, or None if it can't be fetched."""
    try:
        return page.evaluate(
            "() => fetch('/api/auth/session').then(r => r.json()).catch(() => null)"
        )
    except Exception:
        return None


def ensure_logged_in(page):
    page.goto(BASE_URL, wait_until="domcontentloaded")
    state = session_state(page)
    if state and state.get("user"):
        log(f"Logged in as {state['user'].get('email', '?')}")
        return
    log("")
    log("=" * 64)
    log("Not logged in. Please log into ChatGPT in the Chrome window that")
    log("just opened. If your account has multiple workspaces, switch to")
    log("the workspace whose chats you want to export. The export starts")
    log("automatically once the login completes (checked every 5 seconds).")
    log("=" * 64)
    while True:
        time.sleep(5)
        state = session_state(page)
        if state and state.get("user"):
            log(f"Login detected: {state['user'].get('email', '?')}")
            return


def auth_headers_from(request_headers):
    """Extract the auth headers ChatGPT's own frontend uses."""
    auth = {}
    if request_headers.get("authorization"):
        auth["Authorization"] = request_headers["authorization"]
    if request_headers.get("chatgpt-account-id"):
        auth["chatgpt-account-id"] = request_headers["chatgpt-account-id"]
    return auth


def fetch_with_session(page, api_url, headers):
    """fetch() inside the page so cookies + fingerprint match the session."""
    return page.evaluate(
        """async ({url, headers}) => {
            const r = await fetch(url, {headers});
            let body = null;
            try { body = await r.text(); } catch (e) {}
            return {status: r.status, body,
                    cf: r.headers.get('cf-mitigated') || '',
                    ctype: r.headers.get('content-type') || ''};
        }""",
        {"url": api_url, "headers": headers},
    )


def is_cloudflare_challenge(res):
    """A Cloudflare check answers API calls with 403 and an HTML page (and
    usually a cf-mitigated header) — not a rate limit."""
    if res.get("status") != 403:
        return False
    if (res.get("cf") or "").lower() == "challenge":
        return True
    body = (res.get("body") or "")[:3000].lower()
    return "html" in (res.get("ctype") or "").lower() and (
        "cloudflare" in body or "challenge" in body or "just a moment" in body)


# ---------------------------------------------------------------- discovery


def capture_auth(page):
    """Load the homepage and copy the auth headers the frontend itself uses."""
    with page.expect_response(
        lambda r: "/backend-api/conversations?" in r.url, timeout=NAV_TIMEOUT_MS
    ) as resp_info:
        page.goto(BASE_URL, wait_until="domcontentloaded")
    return auth_headers_from(resp_info.value.request.headers)


def api_get(page, api, headers):
    """GET with automatic 60s wait on throttling."""
    while True:
        res = fetch_with_session(page, api, headers)
        if res["status"] == 429:
            log("  throttled while listing, waiting 60s...")
            time.sleep(60)
            continue
        return res


def discover_main(page, headers):
    items, offset, total = [], 0, None
    while total is None or offset < total:
        api = (
            f"{BASE_URL}/backend-api/conversations"
            f"?offset={offset}&limit=100&order=updated"
        )
        res = api_get(page, api, headers)
        if res["status"] != 200:
            raise RuntimeError(
                f"conversation list request failed (HTTP {res['status']}). "
                "You can build a chats.csv by hand instead — see README."
            )
        data = json.loads(res["body"])
        batch = data.get("items") or []
        items.extend(batch)
        total = data.get("total", len(items))
        offset += 100
        log(f"  found {len(items)}/{total}")
        if not batch:
            break
        time.sleep(random.uniform(2, 4))
    return [
        (f"{BASE_URL}/c/{it['id']}", it["id"], it.get("title") or "untitled")
        for it in items if it.get("id")
    ]


def list_projects(page, headers):
    """[(project id, name)] for every Project in the account, or None if the
    listing failed."""
    projects, cursor = [], None
    while True:
        api = f"{BASE_URL}/backend-api/gizmos/snorlax/sidebar"
        if cursor:
            api += f"?cursor={cursor}"
        res = api_get(page, api, headers)
        if res["status"] != 200:
            log(f"  ! project listing failed (HTTP {res['status']}), skipping projects")
            return None
        data = json.loads(res["body"])
        for it in data.get("items") or []:
            g = it.get("gizmo") or {}
            g = g.get("gizmo") or g  # shape varies
            gid = g.get("id")
            name = ((g.get("display") or {}).get("name")
                    or g.get("short_url") or gid or "project")
            if gid:
                projects.append((gid, name))
        cursor = data.get("cursor")
        if not cursor or not data.get("items"):
            break
        time.sleep(random.uniform(1, 2))
    return projects


def discover_projects(page, headers):
    """Enumerate Projects and every conversation inside each of them.
    Returns (chats, projects)."""
    projects = list_projects(page, headers)
    if projects is None:
        return [], None
    chats = []
    for gid, name in projects:
        got, page_cursor = 0, 0
        while True:
            api = (f"{BASE_URL}/backend-api/gizmos/{gid}/conversations"
                   f"?cursor={page_cursor}&limit=50&owned_only=false")
            res = api_get(page, api, headers)
            if res["status"] != 200:
                log(f"  ! listing project {name!r} failed (HTTP {res['status']})")
                break
            data = json.loads(res["body"])
            batch = data.get("items") or []
            for it in batch:
                if it.get("id"):
                    chats.append((
                        f"{BASE_URL}/g/{gid}/c/{it['id']}",
                        it["id"],
                        it.get("title") or "untitled",
                    ))
            got += len(batch)
            nxt = data.get("cursor")
            if not batch or nxt in (None, page_cursor):
                break
            page_cursor = nxt
            time.sleep(random.uniform(1, 2))
        log(f"  project {name!r}: {got} conversations")
    return chats, projects


def save_names(out_dir, projects=None, gpts=None):
    """Merge {id: name} maps into out_dir/projects.json, which the viewer
    uses to group chats by Project / custom GPT."""
    path = out_dir / "projects.json"
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    for key, new in (("projects", projects), ("gpts", gpts)):
        if new:
            cur.setdefault(key, {}).update(new)
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def exported_gpt_ids(out_dir):
    """Custom-GPT ids referenced by already-exported chats."""
    ids = set()
    for p in out_dir.glob("*/conversation.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("gizmo_type") == "gpt" and d.get("gizmo_id"):
            ids.add(d["gizmo_id"])
    return ids


def list_gpt_names(page, headers, gids):
    """{gpt id: name}, best effort (unknown or deleted GPTs are skipped)."""
    names = {}
    for gid in sorted(gids):
        try:
            res = api_get(page, f"{BASE_URL}/backend-api/gizmos/{gid}", headers)
            if res["status"] == 200:
                d = json.loads(res["body"])
                g = d.get("gizmo") or d
                name = (g.get("display") or {}).get("name")
                if name:
                    names[gid] = name
        except Exception:
            pass
        time.sleep(random.uniform(1, 2))
    return names


def discover_chats(page, csv_path, out_dir=None):
    """Discover all conversations (main sidebar + every project) and merge
    them into csv_path, preserving existing rows and their order so that
    export folder numbering stays stable across runs."""
    log("Discovering conversations in this account...")
    headers = capture_auth(page)
    found = discover_main(page, headers)
    log("Discovering project conversations...")
    pchats, projects = discover_projects(page, headers)
    found += pchats
    if projects and out_dir:
        save_names(out_dir, projects=dict(projects))

    existing_rows, existing_ids = [], set()
    if csv_path.exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].strip().startswith("http"):
                    m = CONV_ID_RE.search(row[0])
                    if m:
                        existing_rows.append([row[0].strip(),
                                              row[1].strip() if len(row) > 1 else "untitled"])
                        existing_ids.add(m.group(1))

    new_rows = []
    for url, cid, title in found:
        if cid not in existing_ids:
            existing_ids.add(cid)
            new_rows.append([url, title])
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "title"])
        w.writerows(existing_rows + new_rows)
    log(f"{csv_path}: kept {len(existing_rows)} existing, added {len(new_rows)} new")
    return headers


def read_chat_list(csv_path):
    """Return [(url, conversation_id, title)] deduped by conversation id.

    Accepts any CSV whose first column is a chatgpt.com conversation URL;
    the second column (optional) is the title. Header rows are ignored.
    """
    chats, seen = [], set()
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip().startswith("http"):
                continue
            url = row[0].strip()
            title = (row[1].strip() if len(row) > 1 else "") or "untitled"
            m = CONV_ID_RE.search(url)
            if not m:
                log(f"  ! no conversation id in URL, skipping: {url}")
                continue
            cid = m.group(1)
            if cid in seen:
                continue
            seen.add(cid)
            chats.append((url, cid, title))
    return chats


# ------------------------------------------------- conversation -> markdown


def ordered_messages(data):
    """Walk the mapping tree from current_node to the root, oldest first."""
    mapping = data.get("mapping") or {}
    chain, node_id = [], data.get("current_node")
    while node_id:
        node = mapping.get(node_id) or {}
        if node.get("message"):
            chain.append(node["message"])
        node_id = node.get("parent")
    return list(reversed(chain))


def part_to_text(part):
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        ct = part.get("content_type", "")
        if "asset_pointer" in part:
            return f"[image: {part.get('asset_pointer')}]"
        if ct == "audio_transcription":
            return part.get("text", "")
        if "text" in part:
            return part.get("text") or ""
        return f"[{ct or 'unsupported content'}]"
    return ""


# ----------------------------------------------- rich ("genui") answers
#
# Newer ChatGPT answers with photo cards, place names or maps store layout
# code (JSX-like markup) as the message text, plus metadata.model_dil_v2:
#   fallbackMarkdown  - ChatGPT's own plain-Markdown rendering, with inline
#                       tokens  \ue200TYPE\ue202ARG\ue202ARG...\ue201
#   appData.opGenui.componentResults - resolved image searches (AsyncImage)
# We render fallbackMarkdown, expand its tokens, and save the images locally.

GENUI_TOKEN_RE = re.compile("\ue200([A-Za-z0-9_]+)\ue202(.*?)\ue201", re.S)
INVISIBLE_TOKENS = ("cite", "filecite")  # citation markers: dropped
GENUI_IMG_PREFIX = "genui_"
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png",
             "image/webp": ".webp", "image/gif": ".gif"}


def _dil(msg):
    d = (msg.get("metadata") or {}).get("model_dil_v2")
    return d if isinstance(d, dict) else None


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_list(v):
    return v if isinstance(v, list) else []


def message_genui_images(msg):
    """{image search query: [image dict, ...]} for one message's resolved
    AsyncImage results (deduplicated within each query only)."""
    dil = _dil(msg)
    if not dil:
        return {}
    results = _as_dict(_as_dict(_as_dict(dil.get("appData")).get("opGenui"))
                       .get("componentResults"))
    idx = {}
    for r in results.values():
        r = _as_dict(r)
        if r.get("componentName") != "AsyncImage":
            continue
        for im in _as_list(_as_dict(r.get("state")).get("images")):
            if not isinstance(im, dict):
                continue
            q, stem = im.get("image_search_query"), genui_image_stem(im)
            if isinstance(q, str) and q and stem:
                lst = idx.setdefault(q, [])
                if all(genui_image_stem(x) != stem for x in lst):
                    lst.append(im)
    return idx


def genui_images(data):
    """Every resolved image in the chat, one entry per distinct image."""
    out, seen = [], set()
    for node in _as_dict(data.get("mapping")).values():
        msg = _as_dict(_as_dict(node).get("message"))
        for imgs in message_genui_images(msg).values():
            for im in imgs:
                stem = genui_image_stem(im)
                if stem not in seen:
                    seen.add(stem)
                    out.append(im)
    return out


def genui_image_stem(im):
    """Stable local file name (without extension) for a resolved image."""
    key = (im.get("content_url") or im.get("thumbnail_url")
           or im.get("original_content_url") or "")
    if not isinstance(key, str) or not key:
        return ""
    return GENUI_IMG_PREFIX + hashlib.sha1(key.encode()).hexdigest()[:12]


def local_genui_files(files_dir):
    """{stem: file name} of genui images already saved in files_dir."""
    if not files_dir or not files_dir.is_dir():
        return {}
    return {f.stem: f.name for f in files_dir.iterdir()
            if f.name.startswith(GENUI_IMG_PREFIX)}


def download_genui_images(fetch, data, files_dir, err):
    """Save every resolved image of the chat into files_dir.
    fetch(url) -> (status, content_type, body_bytes)."""
    have = local_genui_files(files_dir)
    ok = failed = 0
    for im in genui_images(data):
        stem = genui_image_stem(im)
        if stem in have:
            continue
        saved = False
        for u in (im.get("content_url"), im.get("thumbnail_url"),
                  im.get("original_content_url")):
            if not isinstance(u, str) or not u.startswith("https://"):
                continue
            try:
                status, ctype, body = fetch(u)
            except Exception as e:
                err(f"image {u[:80]}: {type(e).__name__}: {e}")
                continue
            ctype = (ctype or "").split(";")[0].strip().lower()
            if status == 200 and ctype in IMAGE_EXT and body:
                files_dir.mkdir(parents=True, exist_ok=True)
                (files_dir / (stem + IMAGE_EXT[ctype])).write_bytes(body)
                saved = True
                break
        if saved:
            ok += 1
        else:
            failed += 1
            err(f"image for '{im.get('image_search_query', '')}': no source downloadable")
    return ok, failed


def playwright_fetcher(context):
    def fetch(url):
        r = context.request.get(url, timeout=30_000)
        return r.status, r.headers.get("content-type", ""), r.body()
    return fetch


def urllib_fetch(url):
    """Download without a browser. Falls back to the system curl when
    Python can't verify HTTPS certificates (common on python.org builds for
    macOS that never ran 'Install Certificates.command')."""
    import ssl
    from urllib.error import URLError
    from urllib.request import Request, urlopen
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=30, context=ctx) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        return curl_fetch(url)


def curl_fetch(url):
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "body"
        r = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "30", "-A", "Mozilla/5.0",
             "-o", str(out), "-w", "%{http_code} %{content_type}", url],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"curl failed: {r.stderr.strip()[:200]}")
        code, _, ctype = r.stdout.strip().partition(" ")
        return int(code or 0), ctype, out.read_bytes() if out.exists() else b""


# One pass over the text: protected spans (fenced code, genui tokens, inline
# code, 4-space/tab-indented lines) are copied unchanged; elsewhere '#'/'##'
# headings are demoted and harmless backslash escapes removed.
_FALLBACK_RE = re.compile(
    r"(?P<keep>"
    r"^[ \t]*(?P<f>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*(?P=f)[ \t]*$|\Z)"
    "|\ue200.*?\ue201"
    r"|`[^`\n]*`"
    r"|^(?: {4}|\t)[^\n]*"
    r")"
    r"|(?P<h>^#{1,2} )"
    r"|\\(?P<e>[.:()!])",
    re.M | re.S,
)


def clean_fallback(md):
    """Adapt ChatGPT's fallback Markdown to conversation.md conventions:
    demote '#'/'##' headings (a line starting '## ' marks a new message in
    conversation.md and the viewer) and drop backslash escapes of harmless
    punctuation ('1\\.' -> '1.'). Code and tokens are left alone."""
    def repl(m):
        if m.group("keep") is not None:
            return m.group("keep")
        if m.group("h") is not None:
            return "### "
        return m.group("e")
    return _FALLBACK_RE.sub(repl, md)


def _md_text(s):
    """Make a string safe as Markdown link/alt text."""
    return re.sub(r"[\[\]\n\r]+", " ", s or "").strip()


def _map_coords(msg):
    """{place name: (lat, lon)} from the message's resolved map widgets."""
    coords = {}
    for ref in (msg.get("metadata") or {}).get("content_references") or []:
        if isinstance(ref, dict) and ref.get("category") == "map":
            for p in (ref.get("data") or {}).get("points") or []:
                if p.get("name") and p.get("latitude") is not None:
                    coords[p["name"]] = (p["latitude"], p.get("longitude"))
    return coords


def expand_genui_tokens(text, msg, images, local):
    """Replace \\ue200...\\ue201 tokens with plain Markdown."""
    from urllib.parse import quote

    def image_md(im, alt):
        name = local.get(genui_image_stem(im))
        page = im.get("url") or ""
        src = f" [source]({page})" if page.startswith("http") else ""
        if name:
            return f"![{alt}](files/{name}){src}"
        if page.startswith("http"):
            return f"[🖼 {alt}]({page})"
        return f"[image: {alt}]"

    coords = None

    def readable(kind, args):
        """Fallback for unknown or unparsable tokens: keep the first argument
        that looks like visible text rather than silently dropping it."""
        for a in args:
            a = a.strip()
            if a and not a.startswith(("{", "[")) and not re.fullmatch(
                    r"turn\d+[a-z_]+\d+", a):
                return a
        return f"[{kind}]"

    def repl(m):
        nonlocal coords
        kind, args = m.group(1), m.group(2).split("\ue202")
        try:
            if kind in INVISIBLE_TOKENS:
                return ""
            if kind == "url":
                if len(args) >= 2 and args[-1].startswith("http"):
                    return f"[{_md_text(args[0])}]({args[-1]})"
                return args[0]
            if kind == "entity":
                v = json.loads(args[0])
                if isinstance(v, list):
                    return str(v[1] if len(v) > 1 else v[0])
                return str(v)
            if kind == "image_group":
                spec = json.loads(args[0])
                queries = spec.get("query") or []
                if isinstance(queries, str):
                    queries = [queries]
                n = int(spec.get("num_per_query") or 1)
                out = []
                for q in queries:
                    found = images.get(q) or []
                    out += [image_md(im, _md_text(q)) for im in found[:n]]
                    if not found:
                        out.append(f"[image: {_md_text(q)}]")
                return " ".join(out)
            if kind == "genui":
                spec = json.loads(args[0])
                mw = spec.get("map_widget_v2") if isinstance(spec, dict) else None
                if isinstance(mw, dict):
                    if coords is None:
                        coords = _map_coords(msg)
                    lines = []
                    for p in mw.get("points") or []:
                        name, addr = p.get("name") or "", p.get("address") or ""
                        ll = coords.get(name)
                        if ll and ll[1] is not None:
                            link = (f"https://www.openstreetmap.org/?mlat={ll[0]}"
                                    f"&mlon={ll[1]}#map=17/{ll[0]}/{ll[1]}")
                        else:
                            link = ("https://www.openstreetmap.org/search?query="
                                    + quote(addr or name))
                        label = f"**{_md_text(name)}**" + (f" — {addr}" if addr else "")
                        lines.append(f"- {label} ([map]({link}))")
                    return "\n\n" + "\n".join(lines) + "\n\n"
                names = ", ".join(spec) if isinstance(spec, dict) else "widget"
                return f"[interactive widget: {names}]"
        except Exception:
            pass
        return readable(kind, args)

    return GENUI_TOKEN_RE.sub(repl, text)


def message_to_markdown(msg, local=None):
    """local: {stem: file name} of rich-answer images saved in files/."""
    meta = msg.get("metadata") or {}
    if meta.get("is_visually_hidden_from_conversation"):
        return None
    role = (msg.get("author") or {}).get("role", "?")
    name = (msg.get("author") or {}).get("name")
    content = msg.get("content") or {}
    ct = content.get("content_type")

    if role == "system" or ct in ("user_editable_context", "model_editable_context"):
        return None
    # messages addressed to a tool (e.g. internal search queries) are hidden in the UI
    if msg.get("recipient") not in (None, "all"):
        return None

    if ct in ("text", "multimodal_text"):
        dil = _dil(msg)
        fallback = dil.get("fallbackMarkdown") if dil else None
        if isinstance(fallback, str) and fallback.strip():
            # rich answer: parts hold layout code; use ChatGPT's own Markdown
            body = clean_fallback(fallback)
        else:
            body = "\n\n".join(
                t for t in (part_to_text(p) for p in content.get("parts") or []) if t
            )
        if "\ue200" in body:
            try:
                body = expand_genui_tokens(
                    body, msg, message_genui_images(msg), local or {})
            except Exception:  # keep the text rather than lose the chat
                body = GENUI_TOKEN_RE.sub(" ", body)
    elif ct == "code":
        lang = content.get("language") or ""
        body = f"```{lang}\n{content.get('text', '')}\n```"
    elif ct == "execution_output":
        body = f"```\n{content.get('text', '')}\n```"
    elif ct == "thoughts":
        body = "\n\n".join(
            f"> {t.get('summary', '')}: {t.get('content', '')}"
            for t in content.get("thoughts") or []
        )
    elif ct == "tether_quote":
        body = f"> {content.get('title', '')}\n> {content.get('text', '')}"
    else:
        body = content.get("text") or content.get("result") or f"[{ct}]"

    if not (body or "").strip():
        return None

    attachments = meta.get("attachments") or []
    if attachments:
        att = "\n".join(f"- attached: `{a.get('name', a.get('id'))}`" for a in attachments)
        body = att + "\n\n" + body

    header = role.capitalize() if role != "tool" else f"Tool ({name or 'tool'})"
    return f"## {header}\n\n{body}"


def render_markdown(data, url, title, files_dir=None):
    """files_dir: the chat's files/ folder, used to link saved images."""
    local = local_genui_files(files_dir)
    lines = [f"# {data.get('title') or title}", "", f"- URL: {url}"]
    for key in ("create_time", "update_time"):
        ts = data.get(key)
        if ts:
            lines.append(
                f"- {key.replace('_', ' ')}: "
                + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            )
    lines.append("")
    for msg in ordered_messages(data):
        md = message_to_markdown(msg, local)
        if md:
            lines += [md, ""]
    return "\n".join(lines)


# ------------------------------------------------------------ file download


def collect_file_refs(data):
    """Return {file_id: suggested_name} for every file referenced in the chat."""
    refs = {}
    for node in (data.get("mapping") or {}).values():
        msg = node.get("message")
        if not msg:
            continue
        for att in (msg.get("metadata") or {}).get("attachments") or []:
            fid = att.get("id")
            if fid:
                refs[fid] = att.get("name") or fid
        content = msg.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict):
                ap = part.get("asset_pointer") or ""
                m = re.search(r"//([A-Za-z0-9_\-]+)", ap)
                if m:
                    refs.setdefault(m.group(1), m.group(1))
    return refs


SANDBOX_RE = re.compile(r"sandbox:(/mnt/data/[^)\"'\s\\]+)")


def collect_sandbox_refs(data):
    """Return [(message_id, sandbox_path)] for files generated by the code tool
    (linked as sandbox:/mnt/data/... in message text)."""
    refs, seen = [], set()
    for node in (data.get("mapping") or {}).values():
        msg = node.get("message")
        if not msg or not msg.get("id"):
            continue
        blob = json.dumps(msg.get("content") or {})
        for m in SANDBOX_RE.finditer(blob):
            path = m.group(1)
            if path not in seen:
                seen.add(path)
                refs.append((msg["id"], path))
    return refs


def save_download_url(context, dl_url, files_dir, name, err, label):
    resp = context.request.get(dl_url)
    if resp.status != 200:
        err(f"{label}: download HTTP {resp.status}")
        return False
    files_dir.mkdir(exist_ok=True)
    target = files_dir / sanitize(name, max_len=100)
    if target.exists():
        target = files_dir / f"{abs(hash(dl_url)) % 99999}_{target.name}"
    target.write_bytes(resp.body())
    return True


def download_sandbox_files(page, context, cid, refs, files_dir, auth_headers, err):
    """Download code-tool output files via the interpreter/download route."""
    from urllib.parse import quote
    ok = failed = 0
    for mid, path in refs:
        name = path.rsplit("/", 1)[-1]
        if (files_dir / sanitize(name, max_len=100)).exists():
            continue
        try:
            api = (f"{BASE_URL}/backend-api/conversation/{cid}/interpreter/download"
                   f"?message_id={mid}&sandbox_path={quote(path, safe='')}")
            res = api_get(page, api, auth_headers)
            info = json.loads(res["body"]) if res["status"] == 200 and res["body"] else {}
            dl_url = info.get("download_url")
            if not dl_url:
                err(f"sandbox {path} (msg {mid}): no download_url "
                    f"(HTTP {res['status']}, likely expired)")
                failed += 1
                continue
            if save_download_url(context, dl_url, files_dir, name, err,
                                 f"sandbox {path}"):
                ok += 1
            else:
                failed += 1
        except Exception as e:
            err(f"sandbox {path}: {e}")
            failed += 1
        time.sleep(random.uniform(1.5, 3.0))
    return ok, failed


def fetch_library(page, auth_headers):
    """Page through the account file Library: returns
    [{file_id, file_name, origination_thread_id}, ...]."""
    items, cursor = [], None
    while True:
        body = {"cursor": cursor} if cursor else {}
        res = page.evaluate(
            """async ({headers, body}) => {
                const r = await fetch('/backend-api/files/library', {
                    method: 'POST',
                    headers: {...headers, 'Content-Type': 'application/json'},
                    body: JSON.stringify(body)});
                return {status: r.status, body: await r.text()};
            }""",
            {"headers": auth_headers, "body": body},
        )
        if res["status"] != 200:
            log(f"  ! library listing failed (HTTP {res['status']})")
            break
        data = json.loads(res["body"])
        batch = data.get("items") or []
        items.extend(batch)
        cursor = data.get("cursor")
        if not batch or not cursor:
            break
        time.sleep(random.uniform(1, 2))
    return items


def library_sweep(page, context, out_dir, auth_headers, err, limit_files=False):
    """Download every Library file into the folder of the chat it came from
    (or export/_library/ if the chat isn't exported). Uploaded files keep
    working under fresh Library ids even when their original chat-attachment
    ids have gone stale. With limit_files, only files whose originating chat
    has been exported are downloaded; nothing goes to export/_library/."""
    # map full conversation id -> exported folder via manifest.csv
    folder_by_cid = {}
    manifest = out_dir / "manifest.csv"
    if manifest.exists():
        with open(manifest, encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 3 and row[2]:
                    m = CONV_ID_RE.search(row[0])
                    if m:
                        folder_by_cid[m.group(1)] = row[2]

    if limit_files and not folder_by_cid:
        log("Library sweep skipped: no exported chats yet (--limit-files)")
        return

    log("Sweeping the account file Library...")
    items = fetch_library(page, auth_headers)
    if limit_files:
        total = len(items)
        items = [it for it in items
                 if it.get("origination_thread_id") in folder_by_cid]
        log(f"  {total} files in library; {len(items)} belong to exported chats "
            f"({total - len(items)} skipped, --limit-files)")
    else:
        log(f"  {len(items)} files in library")

    ok = failed = skipped = 0
    for it in items:
        fid, name = it.get("file_id"), it.get("file_name") or it.get("file_id")
        tid = it.get("origination_thread_id")
        if not fid:
            continue
        folder = out_dir / folder_by_cid[tid] if tid in folder_by_cid else out_dir / "_library"
        files_dir = folder / "files" if tid in folder_by_cid else folder
        if (files_dir / sanitize(name, max_len=100)).exists():
            skipped += 1
            continue
        try:
            res = api_get(
                page, f"{BASE_URL}/backend-api/files/{fid}/download", auth_headers)
            info = json.loads(res["body"]) if res["status"] == 200 and res["body"] else {}
            dl_url = info.get("download_url")
            if not dl_url:
                err(f"library {fid} ({name}): no download_url (HTTP {res['status']})")
                failed += 1
                continue
            files_dir.mkdir(parents=True, exist_ok=True)
            if save_download_url(context, dl_url, files_dir, name, err,
                                 f"library {fid} ({name})"):
                ok += 1
            else:
                failed += 1
        except Exception as e:
            err(f"library {fid} ({name}): {e}")
            failed += 1
        time.sleep(random.uniform(1.5, 3.0))
    log(f"  library sweep: {ok} downloaded, {skipped} already present, {failed} failed")


def already_have(files_dir, name, fid=""):
    """True if a file for this ref is already on disk (allowing for the
    extension we may have appended and collision-renamed copies)."""
    if not files_dir.is_dir():
        return False
    base = sanitize(name, max_len=100)
    tail = fid[-8:] if fid else "\x00"
    for f in files_dir.iterdir():
        if f.name == base or f.name.startswith(base + ".") or tail in f.name:
            return True
    return False


def download_files(page, context, refs, files_dir, auth_headers, err):
    ok = failed = 0
    for fid, name in refs.items():
        if already_have(files_dir, name, fid):
            continue
        try:
            info = res = None
            for api in (
                f"{BASE_URL}/backend-api/files/{fid}/download",
                f"{BASE_URL}/backend-api/files/download/{fid}",
            ):
                res = api_get(page, api, auth_headers)
                if res["status"] == 200 and res["body"]:
                    info = json.loads(res["body"])
                    break
            dl_url = (info or {}).get("download_url")
            if not dl_url:
                # very common: OpenAI deletes old file content server-side.
                # The conversation text is still captured; only the file is gone.
                status = res["status"] if res else "?"
                snippet = (res["body"] or "")[:200] if res else ""
                err(
                    f"file {fid} ({name}): no download_url "
                    f"(HTTP {status}, likely expired) body={snippet!r}"
                )
                failed += 1
                continue

            resp = context.request.get(dl_url)
            if resp.status != 200:
                err(f"file {fid} ({name}): download HTTP {resp.status}")
                failed += 1
                continue
            body = resp.body()

            fname = sanitize(name, max_len=100)
            if "." not in fname:
                ctype = resp.headers.get("content-type", "")
                for mime, ext in (
                    ("image/png", ".png"), ("image/jpeg", ".jpg"),
                    ("image/webp", ".webp"), ("application/pdf", ".pdf"),
                ):
                    if mime in ctype:
                        fname += ext
                        break
            files_dir.mkdir(exist_ok=True)
            target = files_dir / fname
            if target.exists():
                target = files_dir / f"{fid[-8:]}_{fname}"
            target.write_bytes(body)
            ok += 1
        except Exception as e:
            err(f"file {fid} ({name}): {e}")
            failed += 1
        time.sleep(random.uniform(1.5, 3.0))
    return ok, failed


def fix_files(page, context, out_dir, auth, ef):
    """Revisit every exported chat folder and download whatever files are
    still missing: stale-id attachments (retried), code-tool sandbox files,
    then the account Library."""
    folders = [f for f in sorted(out_dir.iterdir())
               if f.is_dir() and (f / "conversation.json").exists()]
    log(f"fix-files: checking {len(folders)} exported chats for missing files")
    tot_ok = tot_fail = 0
    for i, folder in enumerate(folders, 1):
        try:
            data = json.loads((folder / "conversation.json").read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  ! unreadable conversation.json in {folder.name}: {e}")
            continue
        cid = data.get("conversation_id") or ""
        if not cid:
            m = CONV_ID_RE.search((folder / "conversation.md").read_text(encoding="utf-8")[:500])
            cid = m.group(1) if m else ""

        def err(msg, _f=folder.name):
            ef.write(f"[fix-files {_f}]\n    {msg}\n")
            ef.flush()

        refs = {fid: name for fid, name in collect_file_refs(data).items()
                if not already_have(folder / "files", name, fid)}
        srefs = [(mid, path) for mid, path in collect_sandbox_refs(data)
                 if not already_have(folder / "files", path.rsplit("/", 1)[-1])]
        if not refs and not srefs:
            continue
        log(f"[{i}/{len(folders)}] {folder.name}: "
            f"{len(refs)} attachment(s) + {len(srefs)} generated file(s) missing")
        ok, fail = download_files(page, context, refs, folder / "files", auth, err)
        tot_ok += ok
        tot_fail += fail
        if cid and srefs:
            ok, fail = download_sandbox_files(
                page, context, cid, srefs, folder / "files", auth, err)
            tot_ok += ok
            tot_fail += fail
    log(f"fix-files: recovered {tot_ok} files, {tot_fail} still unavailable")


def rerender(out_dir, ef):
    """Rebuild conversation.md of every exported chat from its saved
    conversation.json, downloading images of rich answers. No browser needed."""
    folders = [f for f in sorted(out_dir.iterdir())
               if f.is_dir() and (f / "conversation.json").exists()]
    log(f"rerender: rebuilding {len(folders)} exported chats")
    n_ok = n_bad = img_ok = img_fail = 0
    for folder in folders:
        def err(msg, _f=folder.name):
            ef.write(f"[rerender {_f}]\n    {msg}\n")
            ef.flush()
        try:
            data = json.loads((folder / "conversation.json").read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  ! unreadable conversation.json in {folder.name}: {e}")
            continue
        url = ""
        md_path = folder / "conversation.md"
        if md_path.exists():
            for ln in md_path.read_text(encoding="utf-8").split("\n")[:8]:
                if ln.startswith("- URL: "):
                    url = ln[7:].strip()
        if not url and data.get("conversation_id"):
            url = f"{BASE_URL}/c/{data['conversation_id']}"
        try:
            if genui_images(data):
                ok, fail = download_genui_images(urllib_fetch, data, folder / "files", err)
                img_ok += ok
                img_fail += fail
                if ok or fail:
                    log(f"  {folder.name}: {ok} image(s) saved, {fail} unavailable")
        except Exception as e:  # images are best-effort; still rebuild the text
            err(f"rich-answer images: {type(e).__name__}: {e}")
        try:
            text = render_markdown(data, url, data.get("title") or folder.name,
                                   folder / "files")
            md_path.write_text(text, encoding="utf-8")
            n_ok += 1
        except Exception as e:
            n_bad += 1
            err(f"rerender failed: {type(e).__name__}: {e}")
            log(f"  ! could not rebuild {folder.name}: {e}")
    log(f"rerender: rebuilt {n_ok} chats"
        + (f" ({n_bad} failed, see errors.log)" if n_bad else "")
        + f"; images saved {img_ok}, unavailable {img_fail}")
    if img_fail:
        log("  (unavailable images are listed in errors.log)")


# --------------------------------------------------------------- main loop


def capture_conversation(page, url, cid, auth, load_page=True):
    """Open the chat, then fetch its conversation JSON from inside the page.

    ChatGPT's frontend no longer reliably issues GET
    /backend-api/conversation/{cid} when a chat page loads (the content can
    arrive embedded in the page instead), so waiting passively for that
    request times out. We navigate (so the session looks normal) and then
    request the JSON ourselves with the frontend's auth headers.
    Returns (data, auth_headers, status); status is "challenge" when
    Cloudflare is showing a check that the user must solve in the window.
    With load_page=False the chat page isn't opened: the JSON is fetched from
    whatever chatgpt.com page is already open (fewer requests per chat)."""
    if not auth:
        auth = capture_auth(page)
    if load_page or not page.url.startswith(BASE_URL):
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    api = f"{BASE_URL}/backend-api/conversation/{cid}"
    res = fetch_with_session(page, api, auth)
    if res["status"] == 401:
        # access token expired: re-capture from the homepage and retry once
        auth = capture_auth(page)
        res = fetch_with_session(page, api, auth)
    if is_cloudflare_challenge(res):
        return None, auth, "challenge"
    if res["status"] != 200:
        return None, auth, res["status"]
    try:
        return json.loads(res["body"]), auth, 200
    except (TypeError, ValueError):
        return None, auth, "non-JSON response"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--csv", type=Path, default=Path("chats.csv"),
                    help="chat list CSV (auto-created by discovery if missing)")
    ap.add_argument("--out", type=Path, default=Path("export"),
                    help="output directory (default: ./export)")
    ap.add_argument("--profile", type=Path, default=Path("browser_profile"),
                    help="Chrome profile directory for the saved login")
    ap.add_argument("--limit", type=int, default=0, help="export at most N chats")
    ap.add_argument("--rediscover", action="store_true",
                    help="refresh chats.csv from the account before exporting "
                         "(new chats are appended; existing rows keep their order)")
    ap.add_argument("--fix-files", action="store_true",
                    help="don't re-export conversations; instead revisit every "
                         "already-exported chat and fetch any missing attachments, "
                         "generated (code-tool) files, and Library files")
    ap.add_argument("--rerender", action="store_true",
                    help="don't contact ChatGPT; rebuild conversation.md of every "
                         "already-exported chat from its conversation.json and "
                         "download images shown in rich answers")
    ap.add_argument("--list-projects", action="store_true",
                    help="only fetch the names of your Projects and of custom "
                         "GPTs used in exported chats (saved to "
                         "export/projects.json for the viewer), then exit")
    ap.add_argument("--no-page-load", action="store_true",
                    help="don't open each chat's page in the browser; fetch its "
                         "JSON directly from the open ChatGPT tab (fewer requests "
                         "per chat, may reduce throttling)")
    ap.add_argument("--limit-files", action="store_true",
                    help="in the Library sweep, download only files that came "
                         "from exported chats (default: the whole Library, "
                         "with unmatched files saved to export/_library/)")
    ap.add_argument("--browser-channel", default="chrome",
                    choices=["chrome", "msedge"],
                    help="installed browser to drive (default: chrome)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.csv"
    errors_path = args.out / "errors.log"
    new_manifest = not manifest_path.exists()

    if args.rerender:
        with open(errors_path, "a", encoding="utf-8") as ef:
            rerender(args.out, ef)
        log("Next: python build_viewer.py  (creates export/viewer.html)")
        return

    exported = skipped = failed_n = 0
    extra_delay = 0.0  # grows every time the server throttles us

    with sync_playwright() as p, open(
        manifest_path, "a", encoding="utf-8", newline=""
    ) as mf, open(errors_path, "a", encoding="utf-8") as ef:
        manifest = csv.writer(mf)
        if new_manifest:
            manifest.writerow(
                ["url", "title", "folder", "status", "messages", "files_ok", "files_failed"]
            )

        context = p.chromium.launch_persistent_context(
            user_data_dir=str(args.profile),
            channel=args.browser_channel,
            headless=False,  # ChatGPT blocks headless browsers
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        ensure_logged_in(page)

        def gerr(msg):
            ef.write(f"{msg}\n")
            ef.flush()

        if args.list_projects:
            auth = capture_auth(page)
            projects = list_projects(page, auth) or []
            gpts = list_gpt_names(page, auth, exported_gpt_ids(args.out))
            path = save_names(args.out, projects=dict(projects), gpts=gpts)
            context.close()
            log(f"{len(projects)} project name(s), {len(gpts)} custom GPT name(s) "
                f"saved to {path}")
            log("Next: python build_viewer.py")
            return

        if args.fix_files:
            auth = capture_auth(page)
            fix_files(page, context, args.out, auth, ef)
            library_sweep(page, context, args.out, auth, gerr,
                          limit_files=args.limit_files)
            context.close()
            log("fix-files pass complete.")
            return

        # auth headers for conversation fetches: reuse discovery's; otherwise
        # captured lazily inside the retry loop (a Cloudflare check or slow
        # homepage then retries instead of aborting the run)
        conv_auth = None
        if args.rediscover or not args.csv.exists():
            conv_auth = discover_chats(page, args.csv, args.out)
        chats = read_chat_list(args.csv)
        if args.limit:
            chats = chats[: args.limit]
        log(f"{len(chats)} chats to process")
        last_auth = None

        for i, (url, cid, title) in enumerate(chats, 1):
            folder = args.out / f"{i:03d}_{sanitize(title)}_{cid[:8]}"
            if (folder / "conversation.json").exists():
                skipped += 1
                continue

            def err(msg, _t=title, _u=url):
                ef.write(f"[{_t}] {_u}\n    {msg}\n")
                ef.flush()

            log(f"[{i}/{len(chats)}] {title}")
            data = auth = None
            attempt = rl_hits = 0
            while attempt < MAX_ATTEMPTS and rl_hits < len(RATE_LIMIT_BACKOFFS_S) + 1:
                try:
                    data, auth, status = capture_conversation(
                        page, url, cid, conv_auth, load_page=not args.no_page_load)
                    conv_auth = auth
                    if status == 200:
                        break
                    if status == "challenge":
                        attempt += 1
                        err(f"Cloudflare check on chat page (attempt {attempt})")
                        if args.no_page_load:
                            # show the check in the window so it can be solved
                            try:
                                page.goto(url, wait_until="domcontentloaded",
                                          timeout=NAV_TIMEOUT_MS)
                            except PWTimeout:
                                pass
                        log("    Cloudflare check: solve it in the Chrome window; retrying in 30s")
                        time.sleep(30)
                        continue
                    if status in (429, 403):
                        # throttled: cool down with escalating waits, don't
                        # count against normal retry attempts
                        wait = RATE_LIMIT_BACKOFFS_S[
                            min(rl_hits, len(RATE_LIMIT_BACKOFFS_S) - 1)
                        ]
                        rl_hits += 1
                        log(f"    rate limited (HTTP {status}), cooling down {wait // 60} min...")
                        time.sleep(wait)
                        continue
                    attempt += 1
                    err(f"conversation HTTP {status} (attempt {attempt})")
                    time.sleep(10)
                except PWTimeout:
                    attempt += 1
                    err(f"timeout loading chat page (attempt {attempt})")
                    log("    timed out (Cloudflare check? solve it in the window if shown)")
                    time.sleep(15)
                except Exception as e:
                    attempt += 1
                    err(f"{type(e).__name__}: {e} (attempt {attempt})")
                    time.sleep(10)

            if not data:
                failed_n += 1
                manifest.writerow([url, title, "", "failed", 0, 0, 0])
                mf.flush()
                continue

            folder.mkdir(parents=True, exist_ok=True)
            try:
                if genui_images(data):
                    g_ok, g_fail = download_genui_images(
                        playwright_fetcher(context), data, folder / "files", err)
                    log(f"    {g_ok} image(s) from rich answers saved"
                        + (f", {g_fail} unavailable" if g_fail else ""))
            except Exception as e:  # never let images abort the export
                err(f"rich-answer images: {type(e).__name__}: {e}")
            (folder / "conversation.md").write_text(
                render_markdown(data, url, title, folder / "files"), encoding="utf-8"
            )

            refs = collect_file_refs(data)
            srefs = collect_sandbox_refs(data)
            f_ok = f_fail = 0
            if refs or srefs:
                log(f"    {len(refs)} file(s) + {len(srefs)} generated file(s), downloading...")
                f_ok, f_fail = download_files(
                    page, context, refs, folder / "files", auth, err
                )
                s_ok, s_fail = download_sandbox_files(
                    page, context, cid, srefs, folder / "files", auth, err
                )
                f_ok += s_ok
                f_fail += s_fail

            last_auth = auth
            n_msgs = len(ordered_messages(data))
            # written last: acts as the completion marker for resume
            (folder / "conversation.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            manifest.writerow(
                [url, title, folder.name, "ok", n_msgs, f_ok, f_fail]
            )
            mf.flush()
            exported += 1
            if rl_hits:
                extra_delay = min(extra_delay + EXTRA_DELAY_PER_429 * rl_hits, EXTRA_DELAY_CAP)
                log(f"    pace slowed: +{extra_delay:.0f}s per chat from now on")
            if exported % LONG_BREAK_EVERY == 0:
                pause = random.uniform(*LONG_BREAK_S)
                log(f"    taking a {int(pause)}s breather after {exported} chats...")
                time.sleep(pause)
            time.sleep(random.uniform(*DELAY_RANGE) + extra_delay)

        library_sweep(page, context, args.out,
                      last_auth or capture_auth(page), gerr,
                      limit_files=args.limit_files)
        context.close()

    log("")
    log(f"Done. exported={exported} skipped(already done)={skipped} failed={failed_n}")
    if failed_n:
        log(f"Failures listed in {errors_path} — re-run to retry just those chats.")
    log("Next: python build_viewer.py  (creates export/viewer.html)")


if __name__ == "__main__":
    main()
