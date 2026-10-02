"""Local review page for the fetch_brave.py inbox.

    python3 fetch_brave.py review

Serves ~/arty/inbox on http://127.0.0.1:8765 (this Mac only) and opens it in
the browser. Each candidate gets Keep / Reject buttons and editable title and
date fields; "Accept kept" moves the kept works into ~/arty/brave.

- Keep    marks the sidecar  "review": "keep"
- Reject  moves the .jpg + .json to ~/arty/inbox/.rejected/{artist}/ (Undo
          brings them back). The search ledger already stops rejected images
          from being downloaded again, so .rejected can be emptied any time.
- Edits   save to the sidecar's "title" and "date" as soon as you leave the field.

Every request must carry a per-run token that only this page knows, so other
web pages open in the browser can't trigger actions.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image

import fetch_brave as fb

TOKEN = secrets.token_urlsafe(24)
_SAFE_NAME = re.compile(r"^[^/\\\x00]+$")
_LOCK = threading.Lock()          # one file operation at a time


# ---------------------------------------------------------------------------
# Inbox operations
# ---------------------------------------------------------------------------

def _candidate(artist: str, name: str, base: Path | None = None) -> Path:
    """Resolve an inbox image path, refusing anything outside the inbox."""
    base = base or fb.INBOX_DIR
    for part in (artist, name):
        if not part or part.startswith(".") or not _SAFE_NAME.match(part):
            raise ValueError("bad path")
    if not name.lower().endswith(".jpg"):
        raise ValueError("bad path")
    path = (base / artist / name).resolve()
    if path.parent.parent != base.resolve():
        raise ValueError("bad path")
    return path


def _write_sidecar(img_path: Path, side: dict) -> None:
    img_path.with_suffix(".json").write_text(
        json.dumps(side, indent=2, ensure_ascii=False), encoding="utf-8")


def _move_pair(src_img: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in (src_img, src_img.with_suffix(".json")):
        if src.exists():
            shutil.move(str(src), dest_dir / src.name)
    if src_img.parent.exists() and not any(src_img.parent.iterdir()):
        src_img.parent.rmdir()


def list_inbox() -> list[dict]:
    items = []
    for artist_dir in fb.inbox_artist_dirs():
        for img_path in sorted(artist_dir.glob("*.jpg")):
            side = fb.read_sidecar(img_path) or {}
            w, h = side.get("width"), side.get("height")
            if not (w and h):
                try:
                    with Image.open(img_path) as im:
                        w, h = im.size
                except OSError:
                    w = h = 0
            items.append({
                "artist_dir": artist_dir.name,
                "name":       img_path.name,
                "artist":     side.get("artist") or artist_dir.name,
                "title":      side.get("title", ""),
                "date":       side.get("date", ""),
                "domain":     side.get("domain", ""),
                "page_url":   side.get("source_page_url", ""),
                "page_title": side.get("page_title", ""),
                "width":      w,
                "height":     h,
                "low_res":    max(w or 0, h or 0) < fb.LOW_RES_PX,
                "kept":       side.get("review") == "keep",
            })
    return items


def do_action(action: str, body: dict) -> dict:
    artist, name = body.get("artist_dir", ""), body.get("name", "")
    with _LOCK:
        if action == "accept":
            moved = fb.accept_inbox(kept_only=True)
            return {"ok": True, "accepted": moved, "output": str(fb.OUTPUT_DIR)}

        if action == "restore":
            src = _candidate(artist, name, fb.REJECTED_DIR)
            if not src.exists():
                return {"ok": False, "error": "not in .rejected any more"}
            _move_pair(src, fb.INBOX_DIR / artist)
            return {"ok": True}

        img = _candidate(artist, name)
        if not img.exists():
            return {"ok": False, "error": "file not found (already moved?)"}
        side = fb.read_sidecar(img)
        if side is None:
            return {"ok": False, "error": "sidecar JSON is unreadable"}

        if action == "keep":
            if body.get("kept", True):
                side["review"] = "keep"
            else:
                side.pop("review", None)
            _write_sidecar(img, side)
            return {"ok": True}

        if action == "edit":
            for field in ("title", "date"):
                if field in body:
                    side[field] = str(body[field]).strip()[:300]
            _write_sidecar(img, side)
            return {"ok": True}

        if action == "reject":
            _move_pair(img, fb.REJECTED_DIR / artist)
            return {"ok": True}

    return {"ok": False, "error": f"unknown action {action!r}"}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "arty-review"

    def log_message(self, fmt, *args):          # keep the terminal quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            page = PAGE.replace("__TOKEN__", TOKEN).replace(
                "__ITEMS__", json.dumps(list_inbox()).replace("</", "<\\/"))
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path.startswith("/img/"):
            try:
                _, _, artist, name = path.split("/", 3)
                img = _candidate(artist, name)
            except ValueError:
                self._send(404, b"not found", "text/plain")
                return
            if img.exists():
                self._send(200, img.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"not found", "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.headers.get("X-Arty-Token") != TOKEN:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "bad token"})
            return
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, do_action(path[len("/api/"):], body))
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:                 # report, don't crash the server
            fb.log.exception("review action failed")
            self._json(500, {"ok": False, "error": str(exc)})


def serve(port: int = 8765, open_browser: bool = True) -> None:
    if not fb.INBOX_DIR.exists():
        raise SystemExit(f"No inbox at {fb.INBOX_DIR} — run a search first.")
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # port busy
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Review page: {url}\nPress Ctrl-C here when you're done.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        fb.write_review_page()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>arty review</title>
<style>
:root{--bg:#121212;--card:#1d1d1d;--line:#2c2c2c;--text:#e6e6e6;--dim:#8a8a8a;
--keep:#4caf72;--reject:#d9534f;--accent:#c9a24a;--link:#8fbfff}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;
background:var(--bg);color:var(--text)}
header{position:sticky;top:0;z-index:5;background:rgba(18,18,18,.96);
border-bottom:1px solid var(--line);padding:12px 20px;display:flex;flex-wrap:wrap;
gap:12px;align-items:center}
h1{font-size:18px;margin:0 12px 0 0;font-weight:600;letter-spacing:.02em}
.counts{color:var(--dim)} .counts b{color:var(--text)}
.filters{display:flex;gap:4px;margin-left:auto}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:#262626;
color:var(--text);padding:6px 12px;border-radius:6px}
button:hover{border-color:#555}
.filters button.on{background:#3a3a3a;border-color:#666}
#accept{background:var(--accent);color:#1a1405;border-color:var(--accent);font-weight:600}
#accept:disabled{opacity:.4;cursor:default}
main{padding:8px 20px 80px}
h2{font-size:16px;margin:28px 0 10px;display:flex;gap:12px;align-items:baseline}
h2 small{color:var(--dim);font-weight:400}
h2 button{font-size:12px;padding:3px 8px;margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.card{background:var(--card);border:2px solid transparent;border-radius:8px;
padding:8px;display:flex;flex-direction:column;gap:6px;transition:opacity .15s}
.card.kept{border-color:var(--keep)}
.card.rejected{opacity:.35}
.card.rejected .thumb,.card.rejected input{pointer-events:none}
.thumb{display:block;height:230px;background:#000;border-radius:4px;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:contain}
input{width:100%;font:inherit;background:#141414;color:var(--text);
border:1px solid var(--line);border-radius:4px;padding:4px 6px}
input:focus{outline:none;border-color:#777}
input.saved{border-color:var(--keep)}
.row{display:flex;gap:6px}
.row input.date{width:38%}
.meta{color:var(--dim);font-size:12px;word-break:break-word}
.meta a{color:var(--link);text-decoration:none}
.lowres{color:var(--accent);font-weight:600}
.actions{display:flex;gap:6px}
.actions button{flex:1}
.k.on{background:var(--keep);border-color:var(--keep);color:#06170c;font-weight:600}
.r:hover{border-color:var(--reject);color:#ffb3b0}
.hidden{display:none!important}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
background:#2b2b2b;border:1px solid #555;padding:10px 16px;border-radius:8px;
opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
#toast.show{opacity:1}
.empty{color:var(--dim);margin-top:40px}
</style></head><body>
<header>
  <h1>arty review</h1>
  <span class="counts"><b id="nUnd">0</b> to review · <b id="nKeep">0</b> kept ·
    <b id="nRej">0</b> rejected</span>
  <div class="filters">
    <button data-f="all" class="on">All</button>
    <button data-f="und">To review</button>
    <button data-f="kept">Kept</button>
  </div>
  <button id="accept" disabled>Accept kept</button>
</header>
<main id="main"></main>
<div id="toast"></div>
<script>
const TOKEN = "__TOKEN__";
let items = __ITEMS__;
let filter = "all";

function esc(s){return String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function toast(msg){const t=document.getElementById("toast");t.textContent=msg;
  t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),3500)}
async function api(action, body){
  const r = await fetch("/api/"+action,{method:"POST",
    headers:{"Content-Type":"application/json","X-Arty-Token":TOKEN},
    body:JSON.stringify(body||{})});
  const d = await r.json().catch(()=>({ok:false,error:"bad response"}));
  if(!d.ok) toast("Couldn't "+action+": "+(d.error||r.status));
  return d;
}
const key = it => it.artist_dir+"/"+it.name;

function render(){
  const main = document.getElementById("main");
  if(!items.length){main.innerHTML='<p class="empty">The inbox is empty.</p>';counts();return}
  const groups = {};
  for(const it of items)(groups[it.artist_dir] ||= []).push(it);
  main.innerHTML = Object.entries(groups).map(([dir,list])=>`
    <section data-dir="${esc(dir)}">
      <h2>${esc(list[0].artist)} <small class="gcount"></small>
        <button class="rejrest" title="Reject everything in this artist not marked Keep">
          Reject the rest</button></h2>
      <div class="grid">${list.map(card).join("")}</div>
    </section>`).join("");
  apply();
}

function card(it){
  const dims = it.width ? `${it.width}×${it.height}` : "size ?";
  return `<div class="card${it.kept?" kept":""}" data-key="${esc(key(it))}">
    <a class="thumb" href="/img/${encodeURIComponent(it.artist_dir)}/${encodeURIComponent(it.name)}"
       target="_blank" title="Open full size"><img loading="lazy"
       src="/img/${encodeURIComponent(it.artist_dir)}/${encodeURIComponent(it.name)}" alt=""></a>
    <input class="title" value="${esc(it.title)}" placeholder="Title" aria-label="Title">
    <div class="row"><input class="date" value="${esc(it.date)}" placeholder="Date" aria-label="Date">
      <span class="meta">${dims}${it.low_res?' · <span class="lowres">low-res</span>':""}</span></div>
    <div class="meta"><a href="${esc(it.page_url)}" target="_blank" rel="noreferrer"
       title="${esc(it.page_title)}">${esc(it.domain)}</a></div>
    <div class="actions">
      <button class="k${it.kept?" on":""}">${it.kept?"✓ Kept":"Keep"}</button>
      <button class="r">Reject</button>
    </div></div>`;
}

function find(el){const c=el.closest(".card");return [c, items.find(i=>key(i)===c.dataset.key)]}

function counts(){
  const live = items.filter(i=>!i.rejected);
  document.getElementById("nUnd").textContent = live.filter(i=>!i.kept).length;
  const k = live.filter(i=>i.kept).length;
  document.getElementById("nKeep").textContent = k;
  document.getElementById("nRej").textContent = items.filter(i=>i.rejected).length;
  const b = document.getElementById("accept");
  b.disabled = !k; b.textContent = k ? `Accept kept (${k})` : "Accept kept";
  document.querySelectorAll("section").forEach(s=>{
    const list = items.filter(i=>i.artist_dir===s.dataset.dir && !i.rejected);
    s.querySelector(".gcount").textContent =
      `${list.filter(i=>i.kept).length} kept · ${list.filter(i=>!i.kept).length} to review`;
  });
}

function apply(){
  document.querySelectorAll(".card").forEach(c=>{
    const it = items.find(i=>key(i)===c.dataset.key);
    const show = filter==="all" || (filter==="kept" ? it.kept && !it.rejected
                                                    : !it.kept && !it.rejected);
    c.classList.toggle("hidden", !show);
  });
  document.querySelectorAll("section").forEach(s=>
    s.classList.toggle("hidden", !s.querySelector(".card:not(.hidden)")));
  counts();
}

async function setKept(c, it, kept){
  if(!(await api("keep",{artist_dir:it.artist_dir,name:it.name,kept})).ok) return;
  it.kept = kept; c.classList.toggle("kept",kept);
  const b = c.querySelector(".k"); b.classList.toggle("on",kept);
  b.textContent = kept ? "✓ Kept" : "Keep"; apply();
}

async function reject(c, it){
  if(!(await api("reject",{artist_dir:it.artist_dir,name:it.name})).ok) return;
  it.rejected = true; c.classList.add("rejected");
  const b = c.querySelector(".r"); b.textContent = "Undo";
  c.querySelector(".k").disabled = true; apply();
}

async function restore(c, it){
  if(!(await api("restore",{artist_dir:it.artist_dir,name:it.name})).ok) return;
  it.rejected = false; c.classList.remove("rejected");
  c.querySelector(".r").textContent = "Reject";
  c.querySelector(".k").disabled = false; apply();
}

document.addEventListener("click", async e=>{
  const t = e.target;
  if(t.matches(".filters button")){
    filter = t.dataset.f;
    document.querySelectorAll(".filters button").forEach(b=>b.classList.toggle("on",b===t));
    apply(); return;
  }
  if(t.matches(".k")){const [c,it]=find(t); setKept(c,it,!it.kept); return}
  if(t.matches(".r")){const [c,it]=find(t); it.rejected ? restore(c,it) : reject(c,it); return}
  if(t.matches(".rejrest")){
    const s = t.closest("section");
    const rest = items.filter(i=>i.artist_dir===s.dataset.dir && !i.kept && !i.rejected);
    if(!rest.length || !confirm(`Reject ${rest.length} not kept in ${rest[0].artist}?`)) return;
    for(const it of rest){
      const c = s.querySelector(`.card[data-key="${CSS.escape(key(it))}"]`);
      await reject(c, it);
    }
    return;
  }
  if(t.id==="accept"){
    const k = items.filter(i=>i.kept && !i.rejected).length;
    if(!confirm(`Move ${k} kept works into the collection?`)) return;
    t.disabled = true;
    const d = await api("accept");
    if(d.ok){
      items = items.filter(i=>!(i.kept && !i.rejected));
      render();
      toast(`Accepted ${d.accepted}. Next: python3 process_collection.py --input ${d.output}`);
    } else t.disabled = false;
  }
});

document.addEventListener("change", async e=>{
  const t = e.target;
  if(!t.matches("input.title, input.date")) return;
  const [c,it] = find(t);
  const field = t.classList.contains("title") ? "title" : "date";
  if((await api("edit",{artist_dir:it.artist_dir,name:it.name,[field]:t.value})).ok){
    it[field] = t.value; t.classList.add("saved");
    setTimeout(()=>t.classList.remove("saved"),900);
  }
});
document.addEventListener("keydown", e=>{
  if(e.key==="Enter" && e.target.matches("input")) e.target.blur();
});

render();
</script>
</body></html>
"""
