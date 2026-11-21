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
    """Return {(group, sub): [keywords]} plus display order, or empty."""
    if not path.exists():
        return {}, [], {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    keywords, group_order, sub_order = {}, [], {}
    for group, subs in raw.items():
        if group.startswith("_"):
            continue  # allow "_comment" keys
        group_order.append(group)
        sub_order[group] = []
        for sub, words in subs.items():
            if sub.startswith("_"):
                continue
            sub_order[group].append(sub)
            keywords[(group, sub)] = [w.lower() for w in words]
    return keywords, group_order, sub_order


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


def parse_chat(folder, keywords):
    md_path = folder / "conversation.md"
    if not md_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    title = lines[0].lstrip("# ").strip() if lines else folder.name
    url = date = ""
    body_start = 0
    for i, ln in enumerate(lines[:8]):
        if ln.startswith("- URL: "):
            url = ln[7:].strip()
        elif ln.startswith("- create time: "):
            date = ln[15:].strip()
        elif ln.startswith("- update time: ") and not date:
            date = ln[15:].strip()
        if ln.startswith("## "):
            body_start = i
            break
    else:
        body_start = min(6, len(lines))
    body = "\n".join(lines[body_start:]).strip()

    files = []
    fdir = folder / "files"
    if fdir.is_dir():
        # rich-answer images (genui_*) are shown inline in the text instead
        files = [f"{folder.name}/files/{p.name}" for p in sorted(fdir.iterdir())
                 if not re.fullmatch(r"genui_[0-9a-f]+\.\w+", p.name)]

    # Project / custom GPT this chat belongs to (recorded by ChatGPT in the JSON)
    proj, kind = "", ""
    try:
        d = json.loads((folder / "conversation.json").read_text(encoding="utf-8"))
        gid, gtype = d.get("gizmo_id") or "", d.get("gizmo_type") or ""
        if isinstance(gid, str) and gid:
            if gtype == "snorlax" or gid.startswith("g-p-"):
                proj, kind = gid, "project"
            elif gtype == "gpt":
                proj, kind = gid, "gpt"
    except (OSError, ValueError, AttributeError):
        pass

    group, sub = classify(title, body, keywords)
    return {
        "p": proj,
        "pk": kind,
        "id": folder.name,
        "t": title,
        "d": date,
        "u": url,
        "g": group,
        "s": sub,
        "f": files,
        "n": len(re.findall(r"^## ", body, re.M)),
        "b": body,
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

    keywords, group_order, sub_order = load_categories(args.categories)
    if keywords:
        print(f"categories loaded from {args.categories}")
    else:
        print("no categories.json — building a flat archive "
              "(copy categories.example.json to categories.json to enable)")

    chats = []
    for folder in sorted(args.export.iterdir()):
        if folder.is_dir():
            c = parse_chat(folder, keywords)
            if c:
                chats.append(c)
    if not chats:
        raise SystemExit(f"no exported chats found in {args.export}/ — "
                         "run export_chats.py first")
    chats.sort(key=lambda c: c["d"] or "0000", reverse=True)

    present = {(c["g"], c["s"]) for c in chats}
    if ("Unsorted", "Unsorted") in present and "Unsorted" not in group_order:
        group_order.append("Unsorted")
        sub_order["Unsorted"] = ["Unsorted"]

    # names for Projects / custom GPTs, saved by export_chats.py (discovery or
    # --list-projects); unnamed ones fall back to a shortened id
    try:
        names = json.loads((args.export / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        names = {}
    pcount = {}
    for c in chats:
        if c["p"]:
            pcount[(c["p"], c["pk"])] = pcount.get((c["p"], c["pk"]), 0) + 1
    projects = []
    for (pid, kind), n in pcount.items():
        table = (names.get("projects") if kind == "project" else names.get("gpts")) or {}
        label = table.get(pid) or f"{'Project' if kind == 'project' else 'GPT'} {pid[-8:]}"
        projects.append({"id": pid, "kind": kind, "name": label.strip(), "n": n})
    projects.sort(key=lambda p: (p["kind"] != "project", p["name"].lower()))
    unnamed = sum(1 for p in projects if p["id"] not in
                  ((names.get("projects") or {}) | (names.get("gpts") or {})))
    if projects:
        print(f"{len(projects)} projects/custom GPTs"
              + (f" ({unnamed} without a name — run export_chats.py --list-projects)"
                 if unnamed else ""))

    dates = [c["d"][:10] for c in chats if c["d"]]
    meta = {
        "projects": projects,
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

    data_json = json.dumps(chats, ensure_ascii=False).replace("</", "<\\/")
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")

    out = args.export / "viewer.html"
    html = TEMPLATE.replace("__META__", meta_json).replace("__DATA__", data_json)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB) — open it in a browser")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Chat Archive</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,500;9..144,700;9..144,900&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap" rel="stylesheet">
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
.frame{flex:1;display:grid;grid-template-columns:236px 400px 1fr;min-height:0}
.rail{border-right:1px solid var(--rule);overflow-y:auto;padding:18px 0 30px}
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
.list{border-right:1px solid var(--rule);overflow-y:auto;background:
  linear-gradient(rgba(29,24,17,.025),transparent 8px)}
.listhead{position:sticky;top:0;background:var(--paper);border-bottom:1px solid var(--rule);
  padding:10px 18px;font-family:var(--mono);font-size:10.5px;color:var(--ink2);
  letter-spacing:.12em;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center;z-index:2}
.listhead select{font-family:var(--mono);font-size:10.5px;background:none;border:none;color:var(--red2);cursor:pointer}
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
.stamp.pj{color:#6b4f2a;border-color:#6b4f2a;margin-left:6px}
.empty{padding:60px 30px;text-align:center;color:var(--ink3);font-style:italic}
.reader{overflow-y:auto;min-width:0}
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
.body img.inl{max-width:220px;max-height:280px;object-fit:cover;border:1px solid var(--rule);background:#fff;padding:2px;margin:4px 6px 4px 0;vertical-align:top;cursor:zoom-in}
.lb img{max-width:92vw;max-height:92vh;background:#fff;padding:4px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--paper3);border:2px solid var(--paper)}
::-webkit-scrollbar-thumb:hover{background:var(--ink3)}
@media (max-width:1100px){
  .frame{grid-template-columns:200px 1fr}
  .reader{position:fixed;inset:0;top:0;background:var(--paper);z-index:10;display:none}
  .reader.open{display:block}
  .reader .back{display:inline-block}
}
.back{display:none;font-family:var(--mono);font-size:11px;color:var(--red2);cursor:pointer;margin-bottom:14px;background:none;border:1px solid var(--red2);padding:4px 10px}
</style>
</head>
<body>
<header class="masthead">
  <h1 id="masttitle"></h1>
  <div class="mastmeta" id="mastmeta"></div>
  <div class="searchwrap">
    <input id="q" type="search" placeholder="search…" autocomplete="off" spellcheck="false">
    <span class="hint">/</span>
  </div>
</header>
<div class="frame">
  <nav class="rail" id="rail"></nav>
  <main class="list" id="listwrap">
    <div class="listhead">
      <span id="shown"></span>
      <select id="sort">
        <option value="new">newest first</option>
        <option value="old">oldest first</option>
        <option value="az">a → z</option>
      </select>
    </div>
    <div id="rows"></div>
  </main>
  <article class="reader" id="reader">
    <div class="placeholder">
      <div class="big">Ex archivo</div>
      <div class="mono">select a conversation · &uarr;&darr; to browse · / to search</div>
    </div>
  </article>
</div>
<div class="lb" id="lb" onclick="this.classList.remove('on')"><img id="lbimg" alt=""></div>

<script id="meta" type="application/json">__META__</script>
<script id="data" type="application/json">__DATA__</script>
<script>
const META = JSON.parse(document.getElementById('meta').textContent);
const DATA = JSON.parse(document.getElementById('data').textContent);
DATA.forEach((c,i)=>{c.i=i; c.lt=c.t.toLowerCase(); c.lb=c.b.toLowerCase();});
const GI={}; META.groups.forEach((g,i)=>GI[g]=(i+1)%3);

const PN={}; (META.projects||[]).forEach(p=>PN[p.id]=p.name);

let state = {q:'', group:null, sub:null, proj:null, sort:'new', sel:-1, shown:[]};

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
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
  let h=`<button class="cat allbtn ${!state.group&&!state.proj?'on':''}" data-g="" data-s="">All conversations <span class="n">${DATA.length}</span></button>`;
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
  // ChatGPT Projects and custom GPTs, from each chat's recorded gizmo id
  for(const [kind,heading] of [['project','Projects'],['gpt','Custom GPTs']]){
    const ps=(META.projects||[]).filter(p=>p.kind===kind);
    if(!ps.length) continue;
    const tot=ps.reduce((a,p)=>a+p.n,0);
    h+=`<div class="grp"><span>${heading}</span><span class="gc">${tot}</span></div>`;
    for(const p of ps){
      h+=`<button class="cat ${state.proj===p.id?'on':''}" data-p="${esc(p.id)}">${esc(p.name)} <span class="n">${p.n}</span></button>`;
    }
  }
  document.getElementById('rail').innerHTML=h;
}
document.getElementById('rail').addEventListener('click',e=>{
  const b=e.target.closest('.cat'); if(!b) return;
  if(b.dataset.p){state.proj=b.dataset.p; state.group=null; state.sub=null;}
  else{state.proj=null; state.group=b.dataset.g||null; state.sub=b.dataset.s||null;}
  state.sel=-1;
  buildRail(); render();
});

/* ---------------- search / filter ---------------- */
function filter(){
  const q=state.q.trim().toLowerCase();
  let out=DATA.filter(c=>
    (!state.group||c.g===state.group)&&(!state.sub||c.s===state.sub)&&
    (!state.proj||c.p===state.proj)&&
    (!q||c.lt.includes(q)||c.lb.includes(q)));
  if(state.sort==='new') out.sort((a,b)=>(b.d||'').localeCompare(a.d||''));
  else if(state.sort==='old') out.sort((a,b)=>(a.d||'').localeCompare(b.d||''));
  else out.sort((a,b)=>a.t.localeCompare(b.t));
  state.shown=out;
}
function snippet(c,q){
  const i=c.lb.indexOf(q);
  if(i<0) return '';
  const start=Math.max(0,i-70), end=Math.min(c.b.length,i+q.length+90);
  let s=esc((start>0?'…':'')+c.b.slice(start,end).replace(/\n+/g,' ')+(end<c.b.length?'…':''));
  return s.replace(new RegExp(escRe(q),'gi'),m=>'<mark>'+m+'</mark>');
}
function escRe(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}

/* ---------------- list ---------------- */
function render(){
  filter();
  const q=state.q.trim().toLowerCase();
  const rows=state.shown.map((c,idx)=>{
    const d=c.d?c.d.slice(0,10):'——';
    const extras=[c.n+' msgs', c.f.length?c.f.length+' files':''].filter(Boolean).join(' · ');
    return `<div class="row ${idx===state.sel?'sel':''}" data-i="${c.i}" data-idx="${idx}">
      <div class="top"><span class="date">${d}</span>${stamp(c,c.s)}${c.p&&!state.proj?`<span class="stamp pj">${esc(PN[c.p]||'project')}</span>`:''}</div>
      <h3>${esc(c.t)}</h3>
      ${q?`<div class="snip">${snippet(c,q)}</div>`:''}
      <div class="extras">${extras}</div>
    </div>`;}).join('');
  document.getElementById('rows').innerHTML=rows||'<div class="empty">Nothing in the archive matches.</div>';
  const lab = (state.proj&&PN[state.proj])||state.sub||state.group||'all';
  document.getElementById('shown').textContent=`${state.shown.length} filed under ${lab.toLowerCase()}`;
}
document.getElementById('rows').addEventListener('click',e=>{
  const r=e.target.closest('.row'); if(!r) return;
  state.sel=+r.dataset.idx;
  render(); open(DATA[+r.dataset.i]);
});
document.getElementById('sort').addEventListener('change',e=>{state.sort=e.target.value;state.sel=-1;render()});

/* ---------------- markdown ---------------- */
// encode each path segment ('#', '?', '%' in folder names must not become URL syntax)
function encPath(p){return p.split('/').map(encodeURIComponent).join('/')}
let CUR_ID='';  // folder of the chat being rendered; inline images resolve against it
function inline(s){
  // only images saved locally by the exporter (files/genui_<hash>.<ext>); remote stays blocked
  s=s.replace(/!\[([^\]]*)\]\((files\/genui_[0-9a-f]+\.(?:jpg|png|webp|gif))\)/g,(m,a,u)=>{
    const src=esc(encPath(CUR_ID+'/'+u));
    return `<img class="inl" loading="lazy" src="${src}" alt="${a}" title="${a}" onclick="lightbox(this.src)">`;});
  s=s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,(m,a,u)=>`<span class="asset">🖼 ${esc(a||'image')}</span>`);
  s=s.replace(/\[image: [^\]]+\]/g,'<span class="asset">🖼 image</span>');
  s=s.replace(/\[([^\]]+)\]\((https?:[^)\s"'<>`]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  return s;
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
      const st=ord?parseInt(ln,10):1;
      while(i<lines.length&&/^\s*([-*]|\d+\.)\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/,''));i++;}
      html+=(ord&&st>1?`<ol start="${st}">`:`<${tag}>`)+items.map(x=>'<li>'+inline(esc(x))+'</li>').join('')+`</${tag}>`;continue;}
    if(/^>/.test(ln)){endP();let qs=[];
      while(i<lines.length&&/^>/.test(lines[i])){qs.push(lines[i].replace(/^>\s?/,''));i++;}
      html+='<blockquote>'+inline(esc(qs.join('<br>')))+'</blockquote>';continue;}
    if(/^\s*(---+|\*\*\*+)\s*$/.test(ln)){endP();html+='<hr>';i++;continue;}
    if(/^\s*$/.test(ln)){endP();i++;continue;}
    para.push(esc(ln));i++;
  }
  endP();return html;
}

/* ---------------- reader ---------------- */
const IMG_RE=/\.(png|jpe?g|webp|gif)$/i;
function open(c){
  const reader=document.getElementById('reader');
  CUR_ID=c.id;
  const parts=c.b.split(/^## /m).filter(x=>x.trim());
  const msgs=parts.map(p=>{
    const nl=p.indexOf('\n');
    const head=(nl<0?p:p.slice(0,nl)).trim();
    const text=nl<0?'':p.slice(nl+1).trim();
    let role='assistant', who='ChatGPT';
    if(/^user$/i.test(head)){role='user';who='You';}
    else if(/^tool/i.test(head)){role='tool';who=head;}
    else if(!/^assistant$/i.test(head)){role='assistant';who='ChatGPT';return {role,who,text:'## '+p};}
    return {role,who,text};
  });
  const imgs=c.f.filter(f=>IMG_RE.test(f));
  const other=c.f.filter(f=>!IMG_RE.test(f));
  const strip = c.f.length?`<div class="filestrip">`+
      imgs.map(f=>`<img loading="lazy" src="${esc(encPath(f))}" onclick="lightbox(this.src)" title="${esc(f.split('/').pop())}">`).join('')+
      other.map(f=>`<a class="fchip" href="${esc(encPath(f))}" target="_blank">📎 ${esc(f.split('/').pop())}</a>`).join('')+
    `</div>`:'';
  reader.innerHTML=`<div class="doc">
    <button class="back" onclick="closeReader()">&larr; back to list</button>
    <div class="dochead">
      <div class="stamps">${stamp(c,c.g)}${c.s!==c.g?stamp(c,c.s):''}</div>
      <h2>${esc(c.t)}</h2>
      <div class="meta">
        <span>${c.d||'undated'}</span><span>${c.n} messages</span>
        ${/^https?:\/\/[^"'<>\s]+$/.test(c.u)?`<a href="${esc(c.u)}" target="_blank" rel="noopener">original ↗</a>`:''}
        <a href="${esc(encPath(c.id))}/" target="_blank">folder ↗</a>
      </div>
    </div>
    ${strip}
    ${msgs.map(m=>`<div class="msg ${m.role}"><div class="who">${esc(m.who)}</div><div class="body">${mdToHtml(m.text)}</div></div>`).join('')}
  </div>`;
  reader.classList.add('open');
  reader.scrollTop=0;
  const q=state.q.trim();
  if(q) highlight(reader.querySelectorAll('.body'),q);
}
function closeReader(){document.getElementById('reader').classList.remove('open')}
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

/* ---------------- keys / search ---------------- */
const qEl=document.getElementById('q');
let deb;
qEl.addEventListener('input',()=>{clearTimeout(deb);deb=setTimeout(()=>{state.q=qEl.value;state.sel=-1;render();},130)});
document.addEventListener('keydown',e=>{
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
document.getElementById('masttitle').innerHTML =
  /archive$/i.test(META.title)
    ? esc(META.title.slice(0,-7))+'<span class="amp">&amp;</span>'+esc(META.title.slice(-6))
    : esc(META.title);
document.title=META.title;
document.getElementById('mastmeta').textContent=
  `${META.count} conversations · ${META.from} — ${META.to} · exported from chatgpt`;
qEl.placeholder=`search ${META.count} conversations…`;
buildRail();render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
