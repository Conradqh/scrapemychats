"""scrapemychats — export every conversation from a ChatGPT account to local disk.

For ChatGPT accounts where the built-in "export data" button is unavailable
(e.g. many business/Team workspaces). Drives a real, headed Chrome window via
Playwright using a persistent profile, so you log into ChatGPT once and every
later run is automatic. Your data never leaves your machine.

How it works: when the browser opens a chat, the ChatGPT page itself requests
the full conversation JSON from its backend. This script captures that
response (so it never needs to touch your credentials), renders it to
Markdown, and downloads every referenced file/image using the same session.

Usage:
    python export_chats.py                 # discover all chats, export them all
    python export_chats.py --limit 3      # small test run first (recommended)
    python export_chats.py --csv my.csv   # export a specific list instead
    python export_chats.py --rediscover   # refresh chats.csv from the account

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
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

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
            return {status: r.status, body};
        }""",
        {"url": api_url, "headers": headers},
    )


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


def discover_projects(page, headers):
    """Enumerate Projects and every conversation inside each of them."""
    projects, cursor = [], None
    while True:
        api = f"{BASE_URL}/backend-api/gizmos/snorlax/sidebar"
        if cursor:
            api += f"?cursor={cursor}"
        res = api_get(page, api, headers)
        if res["status"] != 200:
            log(f"  ! project listing failed (HTTP {res['status']}), skipping projects")
            return []
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
    return chats


def discover_chats(page, csv_path):
    """Discover all conversations (main sidebar + every project) and merge
    them into csv_path, preserving existing rows and their order so that
    export folder numbering stays stable across runs."""
    log("Discovering conversations in this account...")
    headers = capture_auth(page)
    found = discover_main(page, headers)
    log("Discovering project conversations...")
    found += discover_projects(page, headers)

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


def message_to_markdown(msg):
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
        body = "\n\n".join(
            t for t in (part_to_text(p) for p in content.get("parts") or []) if t
        )
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


def render_markdown(data, url, title):
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
        md = message_to_markdown(msg)
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


def library_sweep(page, context, out_dir, auth_headers, err):
    """Download every Library file into the folder of the chat it came from
    (or export/_library/ if the chat isn't exported). Uploaded files keep
    working under fresh Library ids even when their original chat-attachment
    ids have gone stale."""
    log("Sweeping the account file Library...")
    items = fetch_library(page, auth_headers)
    log(f"  {len(items)} files in library")

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


# --------------------------------------------------------------- main loop


def valid_conversation(data):
    """Reject status responses and incomplete conversation payloads."""
    return (isinstance(data, dict)
            and isinstance(data.get("mapping"), dict)
            and bool(data["mapping"]))


def already_exported(path):
    """Only skip files containing a real conversation, not status JSON."""
    try:
        return valid_conversation(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def capture_conversation(page, url, cid):
    """Accept conversation payloads across endpoint variants, never status JSON."""
    captured = []
    observed = []

    def receive(response):
        path = urlsplit(response.url).path
        if "/backend-api/" not in path or f"/conversation/{cid}" not in path:
            return
        observed.append(f"{response.request.method} {path}: HTTP {response.status}")
        if response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        if valid_conversation(data):
            captured.append((data, auth_headers_from(response.request.headers), 200))

    page.on("response", receive)
    try:
        page.goto(url, wait_until="domcontentloaded")
        deadline = time.monotonic() + NAV_TIMEOUT_MS / 1000
        while not captured and time.monotonic() < deadline:
            page.wait_for_timeout(250)
        if captured:
            return captured[0]
    finally:
        page.remove_listener("response", receive)

    # The frontend may load chats differently; try the authenticated JSON API.
    log("    No conversation payload captured; trying authenticated API...")
    headers = capture_auth(page)
    result = fetch_with_session(page, f"{BASE_URL}/backend-api/conversation/{cid}", headers)
    if result["status"] != 200:
        return None, None, result["status"]
    try:
        data = json.loads(result["body"])
    except (ValueError, TypeError):
        data = None
    if valid_conversation(data):
        return data, headers, 200
    details = "; ".join(observed[-8:]) or "no matching responses"
    raise ValueError(f"No conversation mapping received. Observed: {details}")


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

        if args.fix_files:
            auth = capture_auth(page)
            fix_files(page, context, args.out, auth, ef)
            library_sweep(page, context, args.out, auth, gerr)
            context.close()
            log("fix-files pass complete.")
            return

        if args.rediscover or not args.csv.exists():
            discover_chats(page, args.csv)
        chats = read_chat_list(args.csv)
        if args.limit:
            chats = chats[: args.limit]
        log(f"{len(chats)} chats to process")
        last_auth = None

        for i, (url, cid, title) in enumerate(chats, 1):
            folder = args.out / f"{i:03d}_{sanitize(title)}_{cid[:8]}"
            if already_exported(folder / "conversation.json"):
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
                    data, auth, status = capture_conversation(page, url, cid)
                    if status == 200:
                        break
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
                    err(f"timeout waiting for conversation JSON (attempt {attempt})")
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
            (folder / "conversation.md").write_text(
                render_markdown(data, url, title), encoding="utf-8"
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
                      last_auth or capture_auth(page), gerr)
        context.close()

    log("")
    log(f"Done. exported={exported} skipped(already done)={skipped} failed={failed_n}")
    if failed_n:
        log(f"Failures listed in {errors_path} — re-run to retry just those chats.")
    log("Next: python build_viewer.py  (creates export/viewer.html)")


if __name__ == "__main__":
    main()
