"""Build a self-contained HTML viewer (export/viewer.html) for exported chats.

Scans every export/*/conversation.md produced by export_chats.py and embeds
everything (text, metadata, file lists) into one HTML file with client-side
full-text search, category navigation, and a reader with inline images.
No server needed — just open export/viewer.html in a browser.

Categories (optional): copy categories.example.json to categories.json and
edit it. Format: {"Group": {"Subcategory": ["keyword", ...]}}. Each chat is
scored against every subcategory (title hits count 4x, body hits 1x) and
filed under the best match; chats matching nothing go to "Unsorted".
Without a categories.json, the viewer simply shows one flat, searchable list.

Usage:
    python build_viewer.py [--export DIR] [--title "My Archive"]

Re-run any time after editing categories — it regenerates in seconds.
"""

import argparse
import json
import re
from pathlib import Path


def load_categories(path):
    """Return (keywords, group_order, sub_order, explicit_map)."""
    if not path.exists():
        return {}, [], {}, {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    keywords, group_order, sub_order = {}, [], {}
    explicit_map = raw.get("_chat_map", {})
    for group, subs in raw.items():
        if group.startswith("_"):
            continue  # allow "_comment", "_chat_map" keys
        group_order.append(group)
        sub_order[group] = []
        for sub, words in subs.items():
            if sub.startswith("_"):
                continue
            sub_order[group].append(sub)
            keywords[(group, sub)] = [w.lower() for w in words]
    return keywords, group_order, sub_order, explicit_map


def classify(title, body, keywords):
    if not keywords:
        return ("", "")
    t = " " + title.lower() + " "
    b = " " + body[:4000].lower() + " "
    best, best_score = None, 0
    for cat, words in keywords.items():
        score = 0
        for w in words:
            if w in t:
                score += 4
            elif w in b:
                score += 1
        if score > best_score:
            best, best_score = cat, score
    return best or ("Unsorted", "Unsorted")


def parse_chat(folder, keywords, explicit_map=None):
    md_path = folder / "conversation.md"
    if not md_path.exists():
        return None
    try:
        with open(md_path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(6000)
    except Exception:
        return None

    lines = chunk.split("\n")
    title = lines[0].lstrip("# ").strip() if lines else folder.name
    url = date = ""
    for ln in lines[:8]:
        if ln.startswith("- URL: "):
            url = ln[7:].strip()
        elif ln.startswith("- create time: "):
            date = ln[15:].strip()
        elif ln.startswith("- update time: ") and not date:
            date = ln[15:].strip()

    files = []
    fdir = folder / "files"
    if fdir.is_dir():
        files = [f"{folder.name}/files/{p.name}" for p in sorted(fdir.iterdir()) if p.is_file()]

    if explicit_map and folder.name in explicit_map:
        group, sub = explicit_map[folder.name]
    else:
        group, sub = classify(title, chunk, keywords)
    # Count message headers
    try:
        full_text = md_path.read_text(encoding="utf-8", errors="replace")
        msg_count = len(re.findall(r"^## (?:User|Assistant|Tool)", full_text, re.M | re.I))
    except Exception:
        msg_count = 1

    return {
        "id": folder.name,
        "t": title,
        "d": date,
        "u": url,
        "g": group,
        "s": sub,
        "f": files,
        "n": max(1, msg_count),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--export", type=Path, default=Path("export"),
                    help="export directory created by export_chats.py")
    ap.add_argument("--categories", type=Path, default=Path("categories.json"),
                    help="category keyword file (optional)")
    ap.add_argument("--title", default="The Chat Archive",
                    help="masthead title for the viewer")
    args = ap.parse_args()

    keywords, group_order, sub_order, explicit_map = load_categories(args.categories)
    if keywords or explicit_map:
        print(f"categories loaded from {args.categories}")
    else:
        print("no categories.json — building a flat archive "
              "(copy categories.example.json to categories.json to enable)")

    chats = []
    ignored = []
    if not args.export.is_dir():
        raise SystemExit(f"export directory not found: {args.export} — run export_chats.py first")
    for folder in sorted(args.export.iterdir()):
        if folder.is_dir():
            c = parse_chat(folder, keywords, explicit_map)
            if c:
                chats.append(c)
            else:
                ignored.append(folder.name)
    if not chats:
        raise SystemExit(f"no exported chats found in {args.export}/ — "
                         "run export_chats.py first")
    print(f"found {len(chats)} completed conversations"
          + (f" ({len(ignored)} non-conversation director{'y' if len(ignored) == 1 else 'ies'} ignored)" if ignored else ""))
    chats.sort(key=lambda c: c["d"] or "0000", reverse=True)

    present = {(c["g"], c["s"]) for c in chats}
    if ("Unsorted", "Unsorted") in present and "Unsorted" not in group_order:
        group_order.append("Unsorted")
        sub_order["Unsorted"] = ["Unsorted"]

    dates = [c["d"][:10] for c in chats if c["d"]]
    meta = {
        "count": len(chats),
        "from": min(dates) if dates else "",
        "to": max(dates) if dates else "",
        "title": args.title,
        "groups": group_order,
        "subs": sub_order,
    }

    if keywords:
        dist = {}
        for c in chats:
            dist[(c["g"], c["s"])] = dist.get((c["g"], c["s"]), 0) + 1
        print(f"{len(chats)} chats classified:")
        for g in group_order:
            print(f"  {g}")
            for s in sub_order.get(g, []):
                if (g, s) in dist:
                    print(f"    {s:<24} {dist[(g, s)]}")

    index_data = {
        "meta": meta,
        "chats": chats,
    }
    index_path = args.export / "index.json"
    index_path.write_text(json.dumps(index_data, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {index_path} ({index_path.stat().st_size / 1e3:.1f} KB)")

    out = args.export / "viewer.html"
    out.write_text(TEMPLATE, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e3:.1f} KB) — open via launch_viewer.bat")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Chat Archive</title>
<!-- Web fonts removed: the viewer makes no network requests of any kind.
     Falls back to the local font stacks defined in --serif/--disp/--mono. -->
<style>
:root{
  --paper:#f4eee1; --paper2:#ece4d2; --paper3:#e3d9c3;
  --ink:#1d1811; --ink2:#5c5340; --ink3:#8d8268;
  --red:#b03a20; --red2:#8e2e18;
  --rule:#cfc4aa; --card:#f9f5ea;
  --code-bg:#26211a; --code-ink:#e8dfc9;
  --serif:"Source Serif 4",Georgia,"Times New Roman",serif;
  --disp:"Fraunces",Georgia,serif;
  --mono:"IBM Plex Mono",Consolas,monospace;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{
  font-family:var(--serif);color:var(--ink);background:var(--paper);
  background-image:
    radial-gradient(1200px 600px at 15% -10%, rgba(255,252,240,.9), transparent 60%),
    radial-gradient(900px 500px at 110% 10%, rgba(176,58,32,.05), transparent 55%),
    repeating-linear-gradient(0deg, transparent 0 2px, rgba(29,24,17,.012) 2px 4px);
  display:flex;flex-direction:column;overflow:hidden;
}
.masthead{
  border-bottom:3px double var(--ink);padding:18px 28px 14px;flex:none;
  display:flex;align-items:baseline;gap:26px;flex-wrap:wrap;
}
.masthead h1{
  font-family:var(--disp);font-weight:900;font-size:30px;letter-spacing:.01em;
  font-variation-settings:"opsz" 100;
}
.masthead h1 .amp{color:var(--red);font-style:italic;font-weight:300}
.mastmeta{font-family:var(--mono);font-size:11px;color:var(--ink2);letter-spacing:.08em;text-transform:uppercase}
.searchwrap{margin-left:auto;position:relative;min-width:320px;flex:0 1 420px}
.searchwrap input{
  width:100%;font-family:var(--mono);font-size:13px;color:var(--ink);
  background:var(--card);border:1.5px solid var(--ink);border-radius:0;
  padding:9px 34px 9px 12px;outline:none;box-shadow:3px 3px 0 var(--paper3);
}
.searchwrap input:focus{box-shadow:3px 3px 0 var(--red)}
.searchwrap .hint{position:absolute;right:10px;top:50%;transform:translateY(-50%);
  font-family:var(--mono);font-size:10px;color:var(--ink3);border:1px solid var(--rule);padding:1px 5px}
.frame{flex:1;display:grid;grid-template-columns:220px 380px 1fr;min-height:0;transition:grid-template-columns .15s ease}
.frame.rail-collapsed{grid-template-columns:0px 420px 1fr}
.frame.rail-collapsed .rail{display:none}
.frame.focus-mode{grid-template-columns:0px 0px 1fr}
.frame.focus-mode .rail, .frame.focus-mode .list{display:none}
.rail{grid-column:1;border-right:1px solid var(--rule);overflow-y:auto;padding:12px 0 30px}
.rail-top{display:flex;justify-content:space-between;align-items:center;padding:0 18px 10px;margin-bottom:6px;border-bottom:1px solid var(--rule)}
.rail-top span{font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3)}
.rail-close{background:none;border:1px solid var(--rule);color:var(--ink2);cursor:pointer;font-family:var(--mono);font-size:11px;padding:2px 6px;border-radius:2px}
.rail-close:hover{border-color:var(--red);color:var(--red)}
.rail-toggle{background:none;border:1px solid var(--rule);padding:2px 7px;font-family:var(--mono);font-size:10px;color:var(--ink2);cursor:pointer;display:inline-flex;align-items:center;gap:5px;border-radius:2px;letter-spacing:.05em}
.rail-toggle:hover{border-color:var(--red);color:var(--red)}
.reader-toolbar{
  position:sticky;top:0;background:var(--paper);border-bottom:1px solid var(--rule);
  padding:8px 24px;display:flex;justify-content:space-between;align-items:center;
  z-index:3;flex:none;
}
.reader-btn{
  background:none;border:1px solid var(--rule);padding:3px 9px;border-radius:2px;
  color:var(--ink2);cursor:pointer;font-family:var(--mono);font-size:10.5px;
  display:inline-flex;align-items:center;gap:5px;
}
.reader-btn:hover{color:var(--red);border-color:var(--red)}
.back-btn{display:none}
.rail .allbtn{margin:0 18px 14px}
.grp{margin:16px 18px 4px;font-family:var(--mono);font-size:10px;letter-spacing:.22em;
  text-transform:uppercase;color:var(--red2);border-bottom:1px solid var(--rule);padding-bottom:5px;
  display:flex;justify-content:space-between}
.grp .gc{color:var(--ink3)}
.cat{
  display:flex;justify-content:space-between;align-items:baseline;width:calc(100% - 36px);
  margin:0 18px;padding:6px 8px;border:none;background:none;cursor:pointer;text-align:left;
  font-family:var(--serif);font-size:14.5px;color:var(--ink);border-left:2px solid transparent;
}
.cat:hover{background:var(--paper2)}
.cat.on{background:var(--card);border-left:2px solid var(--red);font-weight:600}
.cat .n{font-family:var(--mono);font-size:10.5px;color:var(--ink3)}
.cat.grpbtn{font-weight:600}
.list{grid-column:2;border-right:1px solid var(--rule);overflow-y:auto;background:
  linear-gradient(rgba(29,24,17,.025),transparent 8px)}
.listhead{position:sticky;top:0;background:var(--paper);border-bottom:1px solid var(--rule);
  padding:10px 18px;font-family:var(--mono);font-size:10.5px;color:var(--ink2);
  letter-spacing:.12em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center;z-index:2;gap:10px}
.listhead select{font-family:var(--mono);font-size:10.5px;background:none;border:none;color:var(--red2);cursor:pointer;outline:none}
.listhead select option{background:var(--paper);color:var(--ink)}
.custom-date-bar{
  position:sticky;top:38px;background:var(--paper2);border-bottom:1px solid var(--rule);
  padding:6px 18px;display:flex;align-items:center;gap:8px;z-index:2;
  font-family:var(--mono);font-size:10.5px;color:var(--ink2);
}
.custom-date-bar input[type="date"]{
  font-family:var(--mono);font-size:10px;color:var(--ink);background:var(--card);
  border:1px solid var(--rule);padding:2px 5px;border-radius:2px;outline:none;
}
.custom-date-bar input[type="date"]:focus{border-color:var(--red)}
.custom-date-bar .clear-btn{
  background:none;border:1px solid var(--rule);color:var(--ink3);cursor:pointer;
  padding:2px 6px;font-family:var(--mono);font-size:10px;border-radius:2px;margin-left:auto;
}
.custom-date-bar .clear-btn:hover{color:var(--red);border-color:var(--red)}
.pager{
  position:sticky;bottom:0;display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:10px 14px;background:rgba(8,10,21,.94);backdrop-filter:blur(18px);
  border-top:1px solid var(--rule);z-index:3;font-family:var(--mono);font-size:10px;color:var(--ink3);
}
.pager-nav{display:flex;align-items:center;gap:8px}.pager button,.pager select{
  font:inherit;color:var(--ink2);background:rgba(255,255,255,.025);border:1px solid var(--rule);
  border-radius:6px;padding:5px 8px;
}
.pager button{cursor:pointer}.pager button:hover:not(:disabled){color:var(--red);border-color:rgba(114,246,218,.48)}
.pager button:disabled{opacity:.3;cursor:default}.pager select{color-scheme:dark}.pager select option{background:#101326}
.row{padding:13px 18px 12px;border-bottom:1px solid var(--rule);cursor:pointer;position:relative}
.row:hover{background:var(--paper2)}
.row.sel{background:var(--card)}
.row.sel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--red)}
.row .top{display:flex;gap:10px;align-items:baseline;margin-bottom:3px}
.row .date{font-family:var(--mono);font-size:10px;color:var(--ink3);letter-spacing:.05em;white-space:nowrap}
.row .stamp{margin-left:auto}
.row h3{font-family:var(--disp);font-weight:500;font-size:15.5px;line-height:1.25}
.row .snip{font-size:12.5px;color:var(--ink2);margin-top:5px;line-height:1.45;font-style:italic}
.row .snip mark{background:#f3d370;color:var(--ink);font-style:normal;padding:0 1px}
.row .extras{font-family:var(--mono);font-size:9.5px;color:var(--ink3);margin-top:4px;letter-spacing:.05em}
.stamp{
  font-family:var(--mono);font-size:8.5px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
  color:var(--red2);border:1px solid var(--red2);padding:1.5px 6px;white-space:nowrap;
  transform:rotate(-1.2deg);display:inline-block;opacity:.85;border-radius:1px;
}
.stamp[data-gi="0"]{color:#3f5e46;border-color:#3f5e46}
.stamp[data-gi="2"]{color:#3a5570;border-color:#3a5570}
.empty{padding:60px 30px;text-align:center;color:var(--ink3);font-style:italic}
.reader{grid-column:3;overflow-y:auto;min-width:0}
.frame.focus-mode .reader{grid-column:1/-1}
.placeholder{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--ink3)}
.placeholder .big{font-family:var(--disp);font-weight:300;font-style:italic;font-size:40px;color:var(--ink2)}
.placeholder .mono{font-family:var(--mono);font-size:11px;letter-spacing:.15em;text-transform:uppercase}
.doc{max-width:780px;margin:0 auto;padding:38px 48px 120px}
.dochead{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:8px}
.dochead .stamps{display:flex;gap:8px;margin-bottom:14px;align-items:center}
.dochead h2{font-family:var(--disp);font-weight:700;font-size:34px;line-height:1.12;letter-spacing:-.005em}
.dochead .meta{display:flex;gap:20px;flex-wrap:wrap;margin-top:12px;font-family:var(--mono);font-size:10.5px;color:var(--ink2);letter-spacing:.06em}
.dochead .meta a{color:var(--red2)}
.filestrip{display:flex;gap:10px;flex-wrap:wrap;padding:14px 0;border-bottom:1px solid var(--rule)}
.filestrip img{height:74px;border:1px solid var(--rule);background:#fff;padding:2px;cursor:zoom-in}
.filestrip a.fchip{font-family:var(--mono);font-size:10.5px;color:var(--ink);text-decoration:none;
  border:1px solid var(--rule);background:var(--card);padding:5px 9px;align-self:center}
.filestrip a.fchip:hover{border-color:var(--red)}
.msg{padding:20px 0 6px}
.msg .who{font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;margin-bottom:8px}
.msg.user .who{color:var(--red)}
.msg.assistant .who{color:var(--ink2)}
.msg.tool .who{color:var(--ink3)}
.msg.user{background:var(--paper2);border-left:3px solid var(--red);
  padding:14px 18px 8px;margin:22px 0 6px}
.msg.tool .body{font-size:12.5px;color:var(--ink2)}
.body{font-size:15.5px;line-height:1.62}
.body>*{margin-bottom:.85em}
.body h1,.body h2,.body h3,.body h4{font-family:var(--disp);line-height:1.25;margin-top:1.2em}
.body h1{font-size:22px}.body h2{font-size:19px}.body h3{font-size:16.5px}.body h4{font-size:15px}
.body a{color:var(--red2)}
.body ul,.body ol{padding-left:1.4em}
.body li{margin-bottom:.3em}
.body blockquote{border-left:2px solid var(--rule);padding:2px 0 2px 14px;color:var(--ink2);font-style:italic}
.body code{font-family:var(--mono);font-size:.82em;background:var(--paper3);padding:1px 4px;border-radius:2px}
.body pre{background:var(--code-bg);color:var(--code-ink);padding:14px 16px;overflow-x:auto;
  border-radius:3px;font-size:12.5px;line-height:1.5}
.body pre code{background:none;color:inherit;padding:0;font-size:inherit}
.body table{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;max-width:100%}
.body th,.body td{border:1px solid var(--rule);padding:5px 9px;text-align:left;vertical-align:top}
.body th{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;background:var(--paper2)}
.body hr{border:none;border-top:1px solid var(--rule);margin:1.4em 0}
.body .asset{font-family:var(--mono);font-size:10px;color:var(--ink3);border:1px dashed var(--rule);padding:1px 6px;letter-spacing:.05em}
.body mark{background:#f3d370;padding:0 1px}
.lb{position:fixed;inset:0;background:rgba(20,16,10,.88);display:none;align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
.lb.on{display:flex}
.lb img{max-width:92vw;max-height:92vh;background:#fff;padding:4px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--paper3);border:2px solid var(--paper)}
::-webkit-scrollbar-thumb:hover{background:var(--ink3)}

/* Cosmic interface ------------------------------------------------------ */
:root{
  --paper:#070811;--paper2:#0d1020;--paper3:#171b31;
  --ink:#f0f5ff;--ink2:#a8b3cc;--ink3:#68738f;
  --red:#72f6da;--red2:#a78bfa;--rule:rgba(154,174,220,.16);
  --card:rgba(17,22,42,.82);--code-bg:#03050c;--code-ink:#c9ffe9;
  --serif:Inter,"Segoe UI",system-ui,sans-serif;
  --disp:"Arial Narrow",Inter,"Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Code","SFMono-Regular",Consolas,monospace;
}
html{background:#05060c}
body{
  background:
    radial-gradient(circle at 15% 0%,rgba(96,72,255,.17),transparent 32%),
    radial-gradient(circle at 84% 14%,rgba(30,236,213,.11),transparent 28%),
    linear-gradient(145deg,#080914 0%,#05060d 55%,#090b18 100%);
  position:relative;
}
body::before{
  content:"";position:fixed;inset:0;pointer-events:none;opacity:.38;
  background-image:
    linear-gradient(rgba(116,246,218,.035) 1px,transparent 1px),
    linear-gradient(90deg,rgba(116,246,218,.035) 1px,transparent 1px),
    radial-gradient(circle,rgba(255,255,255,.55) 0 1px,transparent 1.5px);
  background-size:64px 64px,64px 64px,137px 137px;
  -webkit-mask-image:linear-gradient(to bottom,black,transparent 75%);mask-image:linear-gradient(to bottom,black,transparent 75%);
}
.masthead{
  min-height:92px;padding:16px 26px;border-bottom:1px solid var(--rule);
  background:rgba(5,7,16,.7);backdrop-filter:blur(22px);position:relative;z-index:5;
  box-shadow:0 14px 60px rgba(0,0,0,.28);
}
.brand{display:flex;flex-direction:column;gap:4px}.brand-kicker{font-family:var(--mono);font-size:9px;
  color:var(--red);letter-spacing:.2em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
.signal-dot{width:6px;height:6px;border-radius:50%;background:var(--red);box-shadow:0 0 12px var(--red);animation:pulse 2.2s infinite}
.masthead h1{font-family:var(--disp);font-size:28px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}
.masthead h1 .amp{color:var(--red);font-style:normal;text-shadow:0 0 18px rgba(114,246,218,.65)}
.mastmeta{color:var(--ink3);font-size:9px;letter-spacing:.12em}
.searchwrap input{
  color:var(--ink);background:rgba(10,13,27,.85);border:1px solid rgba(114,246,218,.28);
  border-radius:10px;padding:11px 42px 11px 15px;box-shadow:inset 0 0 24px rgba(0,0,0,.28),0 0 0 1px rgba(255,255,255,.025);
}
.searchwrap input:focus{border-color:var(--red);box-shadow:0 0 0 3px rgba(114,246,218,.09),0 0 28px rgba(114,246,218,.12)}
.searchwrap .hint{border-color:var(--rule);border-radius:4px;background:var(--paper3)}
.frame{grid-template-columns:226px 390px 1fr;background:rgba(5,7,15,.34)}
.frame.rail-collapsed{grid-template-columns:0 420px 1fr}
.rail,.list{border-color:var(--rule);scrollbar-color:#29304b transparent}
.rail{background:rgba(7,9,19,.62);padding-top:12px}
.rail-top,.listhead,.reader-toolbar{background:rgba(8,10,21,.82);backdrop-filter:blur(18px);border-color:var(--rule)}
.rail-top span,.listhead{color:var(--ink3)}
.rail-close,.rail-toggle,.reader-btn,.custom-date-bar .clear-btn{
  border:1px solid var(--rule);border-radius:7px;background:rgba(255,255,255,.025);color:var(--ink2);transition:.18s ease;
}
.rail-close:hover,.rail-toggle:hover,.reader-btn:hover,.custom-date-bar .clear-btn:hover{
  color:var(--red);border-color:rgba(114,246,218,.48);background:rgba(114,246,218,.06);box-shadow:0 0 18px rgba(114,246,218,.08)
}
.grp{color:var(--red);border-color:var(--rule);font-size:9px}.cat{color:var(--ink2);border-radius:7px;margin-top:2px;transition:.16s ease}
.cat:hover{color:var(--ink);background:rgba(167,139,250,.07)}
.cat.on{color:var(--ink);background:linear-gradient(90deg,rgba(114,246,218,.13),rgba(114,246,218,.025));border-left-color:var(--red);box-shadow:inset 0 0 18px rgba(114,246,218,.035)}
.cat .n,.grp .gc{color:var(--ink3)}
.list{background:linear-gradient(180deg,rgba(12,15,30,.74),rgba(6,8,17,.68));display:flex;flex-direction:column;overflow:hidden}
.list #rows{flex:1;min-height:0;overflow-y:auto}
.listhead{min-height:46px}.listhead select{color:var(--red);background:transparent}.listhead select option{background:#101326;color:var(--ink)}
.custom-date-bar{background:rgba(17,20,39,.95);border-color:var(--rule);color:var(--ink2)}
.custom-date-bar input[type="date"]{color:var(--ink);background:#0a0d1b;border-color:var(--rule);color-scheme:dark}
.row{padding:17px 20px;border-color:var(--rule);transition:background .16s ease,transform .16s ease}
.row:hover{background:rgba(167,139,250,.07)}
.row.sel{background:linear-gradient(110deg,rgba(167,139,250,.15),rgba(114,246,218,.055));box-shadow:inset 0 0 30px rgba(167,139,250,.04)}
.row.sel::before{background:linear-gradient(var(--red),var(--red2));box-shadow:0 0 14px var(--red)}
.row h3{font-family:var(--serif);font-size:14.5px;font-weight:600;letter-spacing:.005em}.row .date{color:var(--ink3)}
.row .snip{color:var(--ink2);font-style:normal}.row .snip mark,.body mark{background:rgba(114,246,218,.22);color:#dffff8;border-bottom:1px solid var(--red)}
.stamp{color:var(--red2);border-color:rgba(167,139,250,.5);border-radius:99px;transform:none;background:rgba(167,139,250,.07)}
.stamp[data-gi="0"]{color:#72f6da;border-color:rgba(114,246,218,.45)}.stamp[data-gi="2"]{color:#66baff;border-color:rgba(102,186,255,.45)}
.reader{background:radial-gradient(circle at 75% 5%,rgba(114,246,218,.045),transparent 32%)}
.reader-toolbar{padding:9px 18px}.doc{max-width:850px;padding:52px 58px 130px}
.reader,.doc,.msg,.body{min-width:0}.doc,.msg,.body{max-width:100%}
.body p,.body li,.body blockquote,.dochead h2{overflow-wrap:anywhere}
.body pre{max-width:100%;min-width:0}
.dochead{border-color:rgba(114,246,218,.42);position:relative;padding-bottom:24px}.dochead::after{content:"";position:absolute;bottom:-1px;left:0;width:84px;height:2px;background:var(--red);box-shadow:0 0 14px var(--red)}
.dochead h2{font-family:var(--serif);font-size:36px;font-weight:650;letter-spacing:-.025em}.dochead .meta{color:var(--ink3)}.dochead .meta a,.body a{color:var(--red)}
.msg{padding:24px 0 9px}.msg .who{color:var(--red2);font-size:9px}.msg.user{
  background:linear-gradient(115deg,rgba(167,139,250,.1),rgba(167,139,250,.025));
  border:1px solid rgba(167,139,250,.14);border-left:2px solid var(--red2);border-radius:0 12px 12px 0;padding:17px 20px 10px;
}
.msg.user .who{color:#c4b5fd}.body{font-size:15px;line-height:1.72;color:#dce4f5}.body code{background:#161b31;color:#abffe9;border:1px solid rgba(114,246,218,.09)}
.body pre{border:1px solid rgba(114,246,218,.12);box-shadow:0 14px 35px rgba(0,0,0,.3);border-radius:9px}.body blockquote{border-color:var(--red2);color:var(--ink2)}
.body th{background:#11162a}.body th,.body td{border-color:var(--rule)}
.filestrip{border-color:var(--rule)}.filestrip img,.filestrip a.fchip{background:#0c1020;border-color:var(--rule);border-radius:8px;color:var(--ink)}
.placeholder .big{font-family:var(--serif);font-size:30px;color:var(--ink);font-style:normal;font-weight:300;letter-spacing:.08em;text-transform:uppercase}
.placeholder .mono{color:var(--ink3);font-size:9px}.orbit-mark{width:84px;height:84px;border:1px solid rgba(114,246,218,.22);border-radius:50%;display:grid;place-items:center;position:relative;animation:spin 14s linear infinite}
.orbit-mark::before{content:"";position:absolute;inset:13px;border:1px solid rgba(167,139,250,.35);border-radius:50%;transform:rotate(55deg) scaleY(.42)}
.orbit-mark span{width:17px;height:17px;border-radius:50%;background:linear-gradient(135deg,var(--red),var(--red2));box-shadow:0 0 30px rgba(114,246,218,.8)}
.lb{backdrop-filter:blur(18px);background:rgba(2,3,9,.9)}.lb img{border:1px solid var(--red);background:#080b16;border-radius:8px}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:#242b46;border:2px solid transparent;background-clip:padding-box;border-radius:8px}
@keyframes pulse{50%{opacity:.4;box-shadow:0 0 3px var(--red)}}@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:768px){
  .frame{display:block}
  .rail{display:none}
  .list{height:100%}
  .reader{position:fixed;inset:0;background:var(--paper);z-index:20;display:none}
  .reader.open{display:flex;flex-direction:column}
  .back-btn{display:inline-flex}
  .masthead{padding:12px 15px;gap:9px}.brand-kicker,.mastmeta{display:none}.masthead h1{font-size:20px}.searchwrap{order:3;min-width:100%;flex-basis:100%}
  .listhead{padding:8px 10px;align-items:flex-start}.listhead>div:last-child{gap:4px!important;flex-direction:column;align-items:flex-end!important}.listhead select{font-size:9px}
  .reader-toolbar{position:sticky;width:100%;flex:none}.doc{width:100%;padding:32px 22px 100px}.dochead h2{font-size:27px}.body{font-size:14.5px}.reader-btn{font-size:9px;padding:5px 7px}
  .pager{padding:8px 10px}.pager-nav{gap:5px}.pager button{padding:5px 6px}.pager label{display:none}
}
</style>
</head>
<body>
<header class="masthead">
  <div class="brand">
    <div class="brand-kicker"><span class="signal-dot"></span> Personal memory system</div>
    <h1 id="masttitle"></h1>
  </div>
  <div class="mastmeta" id="mastmeta"></div>
  <div class="searchwrap">
    <input id="q" type="search" placeholder="search…" autocomplete="off" spellcheck="false">
    <span class="hint">/</span>
  </div>
</header>
<div class="frame" id="frame">
  <nav class="rail" id="rail"></nav>
  <main class="list" id="listwrap">
    <div class="listhead">
      <div style="display:flex;align-items:center;gap:8px;">
        <button id="toggle-rail" class="rail-toggle" title="Toggle Categories Sidebar (Ctrl+B)">
          <span id="rail-icon">◧</span> Categories
        </button>
        <span id="shown"></span>
      </div>
      <div style="display:flex;align-items:center;gap:12px;">
        <select id="date-filter" title="Filter by date">
          <option value="all">all time</option>
          <option value="today">today</option>
          <option value="7d">last 7 days</option>
          <option value="this_month">this month</option>
          <option value="prev_month">previous month</option>
          <option value="ytd">year to date</option>
          <option value="prev_year">previous year</option>
          <option value="custom">custom date…</option>
        </select>
        <select id="sort" title="Sort conversations">
          <option value="new">newest first</option>
          <option value="old">oldest first</option>
          <option value="az">a → z</option>
        </select>
      </div>
    </div>
    <div id="custom-date-bar" class="custom-date-bar" style="display:none;">
      <span>From</span>
      <input type="date" id="date-from">
      <span>To</span>
      <input type="date" id="date-to">
      <button id="date-clear" class="clear-btn" title="Reset date filter">Reset</button>
    </div>
    <div id="rows"></div>
    <div class="pager" id="pager">
      <div class="pager-nav">
        <button id="page-prev" type="button" aria-label="Previous page">← Prev</button>
        <span id="page-info"></span>
        <button id="page-next" type="button" aria-label="Next page">Next →</button>
      </div>
      <label>Show <select id="page-size" aria-label="Conversations per page">
        <option value="25">25</option><option value="50" selected>50</option><option value="100">100</option>
      </select></label>
    </div>
  </main>
  <article class="reader" id="reader">
    <div class="placeholder">
      <div class="orbit-mark"><span></span></div>
      <div class="big">Signal ready</div>
      <div class="mono">select a transmission · &uarr;&darr; to browse · / to search</div>
    </div>
  </article>
</div>
<div class="lb" id="lb" onclick="this.classList.remove('on')"><img id="lbimg" alt=""></div>

<script>
let META = {title:"The Chat Archive", count:0, groups:[], subs:{}};
let DATA = [];
const GI = {};
const docCache = new Map();

let state = {
  q:'', group:null, sub:null, sort:'new',
  dateFilter:'all', dateFrom:'', dateTo:'',
  page:1, pageSize:50, sel:-1, matches:[], shown:[]
};

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function localUrl(path){
  if(!path) return '';
  return path.split('/').map(encodeURIComponent).join('/');
}
function stamp(c,label){
  if(!label) return '';
  return '<span class="stamp" data-gi="'+(GI[c.g]??1)+'">'+esc(label)+'</span>';
}

/* ---------------- rail ---------------- */
function counts(){
  const g={}, s={};
  for(const c of DATA){
    g[c.g]=(g[c.g]||0)+1;
    s[c.g+'|'+c.s]=(s[c.g+'|'+c.s]||0)+1;
  }
  return {g,s};
}
function buildRail(){
  const {g,s}=counts();
  let h=`<div class="rail-top"><span>Categories</span><button class="rail-close" id="rail-close-btn" title="Collapse sidebar">« Hide</button></div>`;
  h+=`<button class="cat allbtn ${!state.group?'on':''}" data-g="" data-s="">All conversations <span class="n">${DATA.length}</span></button>`;
  for(const grp of META.groups){
    if(!g[grp]) continue;
    h+=`<div class="grp"><span>${esc(grp)}</span><span class="gc">${g[grp]}</span></div>`;
    const subs=(META.subs[grp]||[]).filter(x=>s[grp+'|'+x]);
    if(!(subs.length===1&&subs[0]===grp)){
      h+=`<button class="cat grpbtn ${state.group===grp&&!state.sub?'on':''}" data-g="${esc(grp)}" data-s="">All ${esc(grp.toLowerCase())} <span class="n">${g[grp]}</span></button>`;
    }
    for(const sub of subs){
      h+=`<button class="cat ${state.group===grp&&state.sub===sub?'on':''}" data-g="${esc(grp)}" data-s="${esc(sub)}">${esc(sub)} <span class="n">${s[grp+'|'+sub]}</span></button>`;
    }
  }
  document.getElementById('rail').innerHTML=h;
  const cb=document.getElementById('rail-close-btn');
  if(cb) cb.addEventListener('click',()=>toggleRail(true));
}
document.getElementById('rail').addEventListener('click',e=>{
  const b=e.target.closest('.cat'); if(!b) return;
  state.group=b.dataset.g||null; state.sub=b.dataset.s||null; state.page=1; state.sel=-1;
  buildRail(); render();
});

/* ---------------- date filter logic ---------------- */
function matchesDate(dStr, mode, dFrom, dTo){
  if(!mode || mode === 'all') return true;
  if(!dStr) return false;
  const d = dStr.slice(0,10);
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const todayStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;

  if(mode === 'today') return d === todayStr;

  if(mode === '7d'){
    const t7 = new Date(now.getTime() - 6*24*60*60*1000);
    const t7Str = `${t7.getFullYear()}-${pad(t7.getMonth()+1)}-${pad(t7.getDate())}`;
    return d >= t7Str && d <= todayStr;
  }

  if(mode === 'this_month'){
    const ym = `${now.getFullYear()}-${pad(now.getMonth()+1)}`;
    return d.startsWith(ym);
  }

  if(mode === 'prev_month'){
    let y = now.getFullYear();
    let m = now.getMonth();
    if(m === 0){ m = 12; y -= 1; }
    const prevYm = `${y}-${pad(m)}`;
    return d.startsWith(prevYm);
  }

  if(mode === 'ytd'){
    const yStart = `${now.getFullYear()}-01-01`;
    return d >= yStart && d <= todayStr;
  }

  if(mode === 'prev_year'){
    const py = `${now.getFullYear()-1}-`;
    return d.startsWith(py);
  }

  if(mode === 'custom'){
    if(dFrom && d < dFrom) return false;
    if(dTo && d > dTo) return false;
    return true;
  }

  return true;
}

/* ---------------- search / filter ---------------- */
function filter(){
  const q=state.q.trim().toLowerCase();
  let out=DATA.filter(c=>
    (!state.group||c.g===state.group)&&(!state.sub||c.s===state.sub)&&
    matchesDate(c.d, state.dateFilter, state.dateFrom, state.dateTo)&&
    (!q||c.lt.includes(q)||(c.g&&c.g.toLowerCase().includes(q))||(c.s&&c.s.toLowerCase().includes(q))));
  if(state.sort==='new') out.sort((a,b)=>(b.d||'').localeCompare(a.d||''));
  else if(state.sort==='old') out.sort((a,b)=>(a.d||'').localeCompare(b.d||''));
  else out.sort((a,b)=>a.t.localeCompare(b.t));
  state.matches=out;
}
function snippet(c,q){
  return '';
}

/* ---------------- list ---------------- */
function render(){
  filter();
  const pages=Math.max(1,Math.ceil(state.matches.length/state.pageSize));
  state.page=Math.min(Math.max(1,state.page),pages);
  const start=(state.page-1)*state.pageSize;
  state.shown=state.matches.slice(start,start+state.pageSize);
  const q=state.q.trim().toLowerCase();
  const rows=state.shown.map((c,idx)=>{
    const d=c.d?c.d.slice(0,10):'——';
    const extras=[c.n+' msgs', c.f.length?c.f.length+' files':''].filter(Boolean).join(' · ');
    return `<div class="row ${idx===state.sel?'sel':''}" data-i="${c.i}" data-idx="${idx}">
      <div class="top"><span class="date">${d}</span>${stamp(c,c.s)}</div>
      <h3>${esc(c.t)}</h3>
      ${q?`<div class="snip">${snippet(c,q)}</div>`:''}
      <div class="extras">${extras}</div>
    </div>`;}).join('');
  document.getElementById('rows').innerHTML=rows||'<div class="empty">Nothing in the archive matches.</div>';
  const lab = state.sub||state.group||'all';
  document.getElementById('shown').textContent=`${state.matches.length} filed under ${lab.toLowerCase()}`;
  document.getElementById('page-info').textContent=state.matches.length
    ? `${start+1}–${Math.min(start+state.pageSize,state.matches.length)} of ${state.matches.length}`
    : '0 results';
  document.getElementById('page-prev').disabled=state.page<=1;
  document.getElementById('page-next').disabled=state.page>=pages;
}
document.getElementById('rows').addEventListener('click',e=>{
  const r=e.target.closest('.row'); if(!r) return;
  state.sel=+r.dataset.idx;
  render(); open(DATA[+r.dataset.i]);
});
document.getElementById('sort').addEventListener('change',e=>{state.sort=e.target.value;state.page=1;state.sel=-1;render()});
document.getElementById('page-prev').addEventListener('click',()=>{
  if(state.page>1){state.page--;state.sel=-1;render();document.getElementById('rows').scrollTop=0;}
});
document.getElementById('page-next').addEventListener('click',()=>{
  if(state.page*state.pageSize<state.matches.length){state.page++;state.sel=-1;render();document.getElementById('rows').scrollTop=0;}
});
document.getElementById('page-size').addEventListener('change',e=>{
  state.pageSize=+e.target.value;state.page=1;state.sel=-1;render();
});

const dfEl=document.getElementById('date-filter');
const cdbEl=document.getElementById('custom-date-bar');
const dFromEl=document.getElementById('date-from');
const dToEl=document.getElementById('date-to');
const dClearBtn=document.getElementById('date-clear');

if(dfEl){
  dfEl.addEventListener('change',e=>{
    state.dateFilter=e.target.value;
    state.page=1;
    state.sel=-1;
    if(cdbEl) cdbEl.style.display = state.dateFilter==='custom' ? 'flex' : 'none';
    render();
  });
}
if(dFromEl){
  dFromEl.addEventListener('change',e=>{
    state.dateFrom=e.target.value;
    state.page=1;
    state.sel=-1;
    render();
  });
}
if(dToEl){
  dToEl.addEventListener('change',e=>{
    state.dateTo=e.target.value;
    state.page=1;
    state.sel=-1;
    render();
  });
}
if(dClearBtn){
  dClearBtn.addEventListener('click',()=>{
    state.dateFrom=''; state.dateTo='';
    state.page=1;
    if(dFromEl) dFromEl.value='';
    if(dToEl) dToEl.value='';
    state.sel=-1;
    render();
  });
}

/* ---------------- markdown ---------------- */
function inline(s){
  const code=[];
  s=s.replace(/`([^`\n]+)`/g,(m,x)=>{code.push(`<code>${x}</code>`);return `\x00CODE${code.length-1}\x00`;});
  s=s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,(m,a,u)=>`<span class="asset">🖼 ${a||'image'}</span>`);
  s=s.replace(/\[image: [^\]]+\]/g,'<span class="asset">🖼 image</span>');
  s=s.replace(/\[([^\]]+)\]\((https?:[^)\s"'<>`]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  return s.replace(/\x00CODE(\d+)\x00/g,(m,i)=>code[+i]);
}
function mdToHtml(md){
  const lines=md.split('\n'); let html='', i=0;
  const flushP=p=>p.length?'<p>'+inline(p.join('<br>'))+'</p>':'';
  let para=[];
  const endP=()=>{html+=flushP(para);para=[];};
  while(i<lines.length){
    const ln=lines[i];
    const fence=ln.match(/^```(\w*)/);
    if(fence){endP();let code=[];i++;
      while(i<lines.length&&!/^```/.test(lines[i])){code.push(lines[i]);i++;}
      i++;html+=`<pre><code>${esc(code.join('\n'))}</code></pre>`;continue;}
    if(/^\s*\|.*\|/.test(ln)&&i+1<lines.length&&/^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i+1])){
      endP();const hdr=ln.split('|').slice(1,-1).map(x=>x.trim());i+=2;let rows=[];
      while(i<lines.length&&/^\s*\|.*\|/.test(lines[i])){rows.push(lines[i].split('|').slice(1,-1).map(x=>x.trim()));i++;}
      html+='<table><thead><tr>'+hdr.map(h=>'<th>'+inline(esc(h))+'</th>').join('')+'</tr></thead><tbody>'+
        rows.map(r=>'<tr>'+r.map(c=>'<td>'+inline(esc(c))+'</td>').join('')+'</tr>').join('')+'</tbody></table>';continue;}
    const h=ln.match(/^(#{1,6})\s+(.*)/);
    if(h){endP();const lv=Math.min(h[1].length+1,4);html+=`<h${lv}>${inline(esc(h[2]))}</h${lv}>`;i++;continue;}
    if(/^\s*([-*]|\d+\.)\s+/.test(ln)){endP();
      const ord=/^\s*\d+\./.test(ln);const tag=ord?'ol':'ul';let items=[];
      while(i<lines.length&&/^\s*([-*]|\d+\.)\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/,''));i++;}
      html+=`<${tag}>`+items.map(x=>'<li>'+inline(esc(x))+'</li>').join('')+`</${tag}>`;continue;}
    if(/^>/.test(ln)){endP();let qs=[];
      while(i<lines.length&&/^>/.test(lines[i])){qs.push(lines[i].replace(/^>\s?/,''));i++;}
      html+='<blockquote>'+qs.map(x=>inline(esc(x))).join('<br>')+'</blockquote>';continue;}
    if(/^\s*(---+|\*\*\*+)\s*$/.test(ln)){endP();html+='<hr>';i++;continue;}
    if(/^\s*$/.test(ln)){endP();i++;continue;}
    para.push(esc(ln));i++;
  }
  endP();return html;
}

/* ---------------- reader ---------------- */
const IMG_RE=/\.(png|jpe?g|webp|gif)$/i;
async function open(c){
  const reader=document.getElementById('reader');
  reader.classList.add('open');
  reader.scrollTop=0;

  // Show loading skeleton
  reader.innerHTML=`<div class="reader-toolbar">
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="reader-btn back-btn" onclick="closeReader()" title="Back to conversation list">&larr; List</button>
      <button class="reader-btn" onclick="toggleRail()" title="Toggle Categories Sidebar (Ctrl+B)">
        <span id="reader-rail-icon">◧</span> Categories
      </button>
    </div>
  </div>
  <div class="doc"><div class="placeholder"><div class="mono">Loading conversation…</div></div></div>`;

  let body = docCache.get(c.id);
  let loadErr = '';
  if(!body){
    try{
      const res = await fetch(`${localUrl(c.id)}/conversation.md`);
      if(res.ok){
        body = await res.text();
        docCache.set(c.id, body);
      } else {
        loadErr = `HTTP ${res.status} (${res.statusText || 'Not Found'})`;
      }
    }catch(e){
      loadErr = e.message || String(e);
    }
  }

  if(!body){
    const msg = loadErr ? `Failed to load: ${loadErr}` : 'No conversation content found.';
    const doc = reader.querySelector('.doc');
    if(doc){
      doc.innerHTML = `<div class="empty" style="padding:40px;text-align:left;line-height:1.6;">
        <h3 style="color:var(--red);font-size:16px;margin-bottom:8px;">Unable to load conversation</h3>
        <p style="color:var(--ink2);margin-bottom:8px;">${esc(msg)}</p>
        <p style="font-size:12px;color:var(--ink3);margin-top:12px;">Ensure local server is running via <code>launch_viewer.bat</code></p>
      </div>`;
    }
    return;
  }

  // Parse messages from markdown body
  const lines = (body || '').split('\n');
  let bodyStart = -1;
  for(let i=0; i<lines.length; i++){
    if(i>0 && lines[i].startsWith('## ')){ bodyStart = i; break; }
  }
  const rawBody = bodyStart >= 0 ? lines.slice(bodyStart).join('\n') : body;
  const parts = (rawBody || '').split(/^## /m).filter(x => x.trim());
  const msgs = parts.map(p => {
    const nl = p.indexOf('\n');
    const head = (nl < 0 ? p : p.slice(0, nl)).trim();
    const text = nl < 0 ? '' : p.slice(nl + 1).trim();
    let role = 'assistant', who = 'ChatGPT';
    if (/^user$/i.test(head)) { role = 'user'; who = 'You'; }
    else if (/^tool/i.test(head)) { role = 'tool'; who = head; }
    return { role, who, text };
  });

  const imgs=(c.f||[]).filter(f=>IMG_RE.test(f));
  const other=(c.f||[]).filter(f=>!IMG_RE.test(f));
  const strip = (c.f||[]).length?`<div class="filestrip">`+
      imgs.map(f=>`<img loading="lazy" src="${localUrl(f)}" onclick="lightbox(this.src)" title="${esc(f.split('/').pop())}">`).join('')+
      other.map(f=>`<a class="fchip" href="${localUrl(f)}" target="_blank">📎 ${esc(f.split('/').pop())}</a>`).join('')+
    `</div>`:'';
  const f=document.getElementById('frame');
  const isFocus = f && f.classList.contains('focus-mode');
  reader.innerHTML=`<div class="reader-toolbar">
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="reader-btn back-btn" onclick="closeReader()" title="Back to conversation list">&larr; List</button>
      <button class="reader-btn" onclick="toggleRail()" title="Toggle Categories Sidebar (Ctrl+B)">
        <span id="reader-rail-icon">◧</span> Categories
      </button>
    </div>
    <div>
      <button class="reader-btn" id="focus-btn" onclick="toggleFocus()" title="Toggle wide reading view">
        ${isFocus ? '⛶ 3-Pane' : '⛶ Focus'}
      </button>
    </div>
  </div>
  <div class="doc">
    <div class="dochead">
      <div class="stamps">${stamp(c,c.g)}${c.s!==c.g?stamp(c,c.s):''}</div>
      <h2>${esc(c.t)}</h2>
      <div class="meta">
        <span>${c.d||'undated'}</span><span>${c.n} messages</span>
        ${/^https?:\/\/[^"'<>\s]+$/.test(c.u)?`<a href="${esc(c.u)}" target="_blank" rel="noopener">original ↗</a>`:''}
        <a href="${localUrl(c.id)}/" target="_blank">folder ↗</a>
      </div>
    </div>
    ${strip}
    ${msgs.length ? msgs.map(m=>`<div class="msg ${m.role}"><div class="who">${esc(m.who)}</div><div class="body">${mdToHtml(m.text)}</div></div>`).join('') : `<div class="msg"><div class="body">${mdToHtml(body || 'No conversation content found.')}</div></div>`}
  </div>`;
  reader.classList.add('open');
  reader.scrollTop=0;
  const q=state.q.trim();
  if(q) highlight(reader.querySelectorAll('.body'),q);
}
function toggleFocus(){
  const f=document.getElementById('frame');
  const reader=document.getElementById('reader');
  if(!reader.classList.contains('open')) return;
  const on = f.classList.toggle('focus-mode');
  const btn = document.getElementById('focus-btn');
  if(btn) btn.textContent = on ? '⛶ 3-Pane' : '⛶ Focus';
}
function closeReader(){
  document.getElementById('reader').classList.remove('open');
  document.getElementById('frame').classList.remove('focus-mode');
}
function highlight(nodes,q){
  const re=new RegExp(escRe(q),'gi');
  nodes.forEach(root=>{
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null);
    const targets=[];let n;
    while(n=walker.nextNode()){if(re.test(n.nodeValue))targets.push(n);re.lastIndex=0;}
    targets.forEach(node=>{
      const frag=document.createDocumentFragment();
      let last=0,m;const s=node.nodeValue;re.lastIndex=0;
      while(m=re.exec(s)){
        frag.appendChild(document.createTextNode(s.slice(last,m.index)));
        const mk=document.createElement('mark');mk.textContent=m[0];frag.appendChild(mk);
        last=m.index+m[0].length;}
      frag.appendChild(document.createTextNode(s.slice(last)));
      node.parentNode.replaceChild(frag,node);
    });
  });
}
function lightbox(src){document.getElementById('lbimg').src=src;document.getElementById('lb').classList.add('on')}

/* ---------------- sidebar toggle ---------------- */
function toggleRail(collapse){
  const f=document.getElementById('frame');
  if(f.classList.contains('focus-mode')){
    f.classList.remove('focus-mode');
    collapse=false;
  }
  const willCollapse = collapse!==undefined ? collapse : !f.classList.contains('rail-collapsed');
  f.classList.toggle('rail-collapsed', willCollapse);
  const icon=document.getElementById('rail-icon');
  if(icon) icon.textContent = willCollapse ? '◨' : '◧';
  const rIcon=document.getElementById('reader-rail-icon');
  if(rIcon) rIcon.textContent = willCollapse ? '◨' : '◧';
  try{ localStorage.setItem('rail_collapsed', willCollapse?'1':'0'); }catch(e){}
}
const trBtn=document.getElementById('toggle-rail');
if(trBtn) trBtn.addEventListener('click',()=>toggleRail());

/* ---------------- keys / search ---------------- */
const qEl=document.getElementById('q');
let deb;
qEl.addEventListener('input',()=>{clearTimeout(deb);deb=setTimeout(()=>{state.q=qEl.value;state.page=1;state.sel=-1;render();},130)});
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='b'){e.preventDefault();toggleRail();return;}
  if(e.key==='/'&&document.activeElement!==qEl){e.preventDefault();qEl.focus();qEl.select();return;}
  if(e.key==='Escape'){if(document.activeElement===qEl){qEl.blur();}else{closeReader();}return;}
  if(document.activeElement===qEl&&e.key==='Enter'){qEl.blur();if(state.shown.length){state.sel=0;render();open(state.shown[0]);}return;}
  if(document.activeElement===qEl)return;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    if(!state.shown.length)return;
    state.sel=e.key==='ArrowDown'?Math.min(state.sel+1,state.shown.length-1):Math.max(state.sel-1,0);
    render();open(state.shown[state.sel]);
    const el=document.querySelector('.row.sel');if(el)el.scrollIntoView({block:'nearest'});
  }
});

/* ---------------- init ---------------- */
document.getElementById('frame').classList.remove('focus-mode');
try{
  if(localStorage.getItem('rail_collapsed')==='1') toggleRail(true);
}catch(e){}

async function init(){
  try {
    const res = await fetch('index.json');
    if(!res.ok) throw new Error('HTTP ' + res.status);
    const index = await res.json();
    META = index.meta || {};
    DATA = index.chats || [];
    DATA.forEach((c,i)=>{
      c.i = i;
      c.lt = (c.t || '').toLowerCase();
    });
    (META.groups||[]).forEach((g,i)=>GI[g]=(i+1)%3);
    document.getElementById('masttitle').textContent = META.title || 'The Chat Archive';
    document.title = META.title || 'The Chat Archive';
    document.getElementById('mastmeta').textContent =
      `${META.count} conversations · ${META.from} — ${META.to} · exported from chatgpt`;
    qEl.placeholder = `search ${META.count} conversations…`;
    buildRail();
    render();
  } catch(err) {
    document.getElementById('rows').innerHTML = `
      <div class="empty" style="padding:40px 24px;text-align:left;line-height:1.6;">
        <h3 style="color:var(--red);font-size:18px;margin-bottom:8px;">Local Server Required</h3>
        <p style="margin-bottom:12px;color:var(--ink2);">Web browsers restrict direct <code>file:///</code> access to local JSON files.</p>
        <p style="margin-bottom:12px;">Please launch the viewer by double-clicking:</p>
        <p><code style="background:var(--card);padding:4px 8px;border:1px solid var(--rule);font-weight:600;">launch_viewer.bat</code></p>
        <p style="margin-top:14px;color:var(--ink3);font-size:12px;">Or in terminal: <code>python -m http.server 8765 --directory export</code></p>
      </div>`;
  }
}
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
