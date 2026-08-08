"""Build a local HTML page for a human to confirm, deny or correct the findings.

Three of this project's ground-truth labels have already turned out to be wrong,
each time caught by a model reading pixels that the labelling process -- which
searched stored OCR text -- could not see. The fix for that is not more careful
searching. It is showing a person the pixels.

The page this writes is deliberately offline and local: it references PNGs on
disk rather than embedding them, keeps answers in localStorage so a half-finished
review survives a reload, and exports a single JSON file to hand back. Nothing is
uploaded anywhere.

Item ids are stable strings, so a second run after adding items keeps the answers
already given.

Run:
    uv run python make_review.py
    xdg-open out/review/index.html
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from pathlib import Path

from lab.groundtruth import GAMES, OTHER
from lab.spipe import open_archive

OUT = Path(__file__).resolve().parent / "out" / "review"
PNG = OUT / "png"
CARD_WIDTH = 1000
ZOOM_WIDTH = 1600

# Tokens that only appear in Age of Empires IV. "elo" is deliberately excluded --
# it matches "develop", which put a dozen terminal sessions in the candidate list
# on the first pass.
STRONG = (
    "age of empires",
    "quick match",
    "landmark",
    "feudal age",
    "imperial age",
    "castle age",
    "dark age",
    "villager",
    "town center",
    "wonder",
    "sacred site",
    "ranked",
    "matchmaking",
)
WEAK = ("victory", "defeat", "surrender", "multiplayer", "aoe")


def export(arc, frame_id: int, width: int = CARD_WIDTH) -> str | None:
    """Export one frame, returning its filename or None if it has no pixels."""
    try:
        src = arc.frame_png(frame_id, max_width=width)
    except Exception as exc:  # noqa: BLE001
        print(f"  frame {frame_id}: {type(exc).__name__} {exc}")
        return None
    dest = PNG / f"f{frame_id}_w{width}.png"
    if not dest.exists():
        dest.write_bytes(src.read_bytes())
    return dest.name


def stretches(arc):
    """Contiguous runs of frames reporting no app name, i.e. fullscreen.

    On KDE Wayland the app name is empty for every fullscreen window, so this
    catches the game *and* a fullscreen terminal. That ambiguity is the whole
    reason a human is being asked: an agent session printing "VICTORY" and an
    actual victory screen are indistinguishable in the stored text.
    """
    lo, hi = arc.span()
    frames = arc.frames(lo.isoformat(), (hi + timedelta(seconds=1)).isoformat())
    runs: list[list] = []
    for f in frames:
        if not f.app_name:
            if runs and f.timestamp - runs[-1][-1].timestamp <= timedelta(minutes=3):
                runs[-1].append(f)
            else:
                runs.append([f])
    out = []
    for r in runs:
        mins = (r[-1].timestamp - r[0].timestamp).total_seconds() / 60
        if mins < 1.5:
            continue
        blob = " ".join((f.text or "").lower() for f in r)
        strong = sorted({k for k in STRONG if k in blob})
        weak = sorted({k for k in WEAK if k in blob})
        out.append(
            {
                "start": r[0],
                "end": r[-1],
                "mins": mins,
                "n": len(r),
                "mid": r[len(r) // 2],
                "strong": strong,
                "weak": weak,
            }
        )
    return sorted(out, key=lambda s: (-len(s["strong"]), -len(s["weak"])))


def token_clusters(arc, gap_minutes: int = 4):
    """Group the frames that individually contain game-only vocabulary.

    Clustering the *matching frames* rather than the surrounding stretch is the
    fix for the aggregation bug described at the call site: a card can only ever
    show frames that are themselves the reason for the label.
    """
    lo, hi = arc.span()
    frames = arc.frames(lo.isoformat(), (hi + timedelta(seconds=1)).isoformat())
    hits = []
    for f in frames:
        if f.app_name:
            continue
        low = (f.text or "").lower()
        found = {k for k in STRONG if k in low}
        if found:
            hits.append((f, found))

    clusters: list[dict] = []
    for f, found in hits:
        if clusters and f.timestamp - clusters[-1]["frames"][-1].timestamp <= timedelta(
            minutes=gap_minutes
        ):
            clusters[-1]["frames"].append(f)
            clusters[-1]["tokens"] |= found
        else:
            clusters.append({"frames": [f], "tokens": set(found)})
    for c in clusters:
        c["start"] = c["frames"][0].local_time
        c["end"] = c["frames"][-1].local_time
    return clusters


# --------------------------------------------------------------------- items


def build_items(arc) -> list[dict]:
    items: list[dict] = []

    # ---- A. did the frames I flagged actually show a game? -----------------
    #
    # The first version of this section aggregated vocabulary over a whole
    # fullscreen stretch -- up to 86 minutes and 806 frames -- and then showed
    # the stretch's midpoint frame. Measured afterwards: the frame shown
    # contained the matched words in 1 of 10 cases. One stretch was labelled off
    # a single frame out of thirty, and the screenshot was a different screen.
    #
    # So the unit here is the matching frame itself. Every card shows frames that
    # actually contain the words, names the words, and quotes that frame's own
    # stored text. A label and its evidence now come from the same pixels.
    A1 = "A1. I flagged these as AoE4 — from these frames, was I right?"
    A2 = "A2. I flagged these as NOT a game — did I miss anything?"

    for cl in token_clusters(arc):
        toks = ", ".join(sorted(cl["tokens"])[:6])
        snippet = " ".join((cl["frames"][0].text or "").split())[:260]
        items.append(
            {
                "id": f"hit_{cl['frames'][0].id}",
                "section": A1,
                "priority": True,
                "title": (
                    f"{cl['start']:%a %d %b %H:%M}–{cl['end']:%H:%M} · "
                    f"{len(cl['frames'])} matching frames"
                ),
                "claim": f"matched on: {toks}",
                "detail": (
                    f"Frames shown are ones that actually contain those words "
                    f"(ids {', '.join(str(f.id) for f in cl['frames'][:3])}). "
                    f"Stored text of the first: “{snippet}”"
                ),
                "frames": [f.id for f in cl["frames"][:3]],
                "kind": "choice",
                "options": [
                    "AoE4 — I was playing",
                    "AoE4 on screen but NOT playing (agent or terminal text about it)",
                    "Different app entirely",
                    "Not sure",
                ],
            }
        )

    for s in stretches(arc):
        if s["strong"] or s["mins"] < 5:
            continue
        items.append(
            {
                "id": f"nogame_{s['mid'].id}",
                "section": A2,
                "priority": False,
                "title": (
                    f"{s['start'].local_time:%a %d %b %H:%M}–"
                    f"{s['end'].local_time:%H:%M} · {s['mins']:.0f} min"
                ),
                "claim": "I think this was NOT a game",
                "detail": (
                    "No game-only vocabulary anywhere in this stretch. "
                    f"Midpoint frame shown (id {s['mid'].id}). Flag it if I am "
                    "wrong — a missed game matters more than a false alarm."
                ),
                "frames": [s["mid"].id],
                "kind": "choice",
                "options": [
                    "Correct, not a game",
                    "Wrong — I was playing AoE4 here",
                    "Not sure",
                ],
            }
        )

    # ---- B. the four matches ----------------------------------------------
    for t in GAMES:
        if t.key == "session_record":
            continue
        a = t.attributes
        items.append(
            {
                "id": f"match_{t.key}",
                "section": "B. The four matches — are these details right?",
                "priority": True,
                "title": f"{t.key.upper()}: {t.title}",
                "claim": (
                    f"opponent={a.get('opponent')} · map={a.get('map')} · "
                    f"outcome={a.get('outcome')} · duration={a.get('duration')}"
                ),
                "detail": t.notes.split("\n")[0][:400] if t.notes else "",
                "frames": t.evidence[:5],
                "kind": "confirm",
                "fields": ["opponent", "map", "outcome", "duration"],
                "prefill": {
                    k: str(a.get(k, ""))
                    for k in ("opponent", "map", "outcome", "duration")
                },
            }
        )

    items.append(
        {
            "id": "match_count",
            "section": "B. The four matches — are these details right?",
            "priority": True,
            "title": "Was four the complete set for that session?",
            "claim": "Four 1v1 matches on the evening of 24 Jul, ending 2–2.",
            "detail": (
                "Every recall score in report 04 divides by four. If there was a "
                "fifth match I never found, all of them are wrong. The window "
                "labelled is 24 Jul 19:30 IST → 25 Jul 06:30 IST."
            ),
            "frames": [],
            "kind": "confirm",
        }
    )

    # ---- C. specific facts -------------------------------------------------
    facts = [
        (
            "fact_gamertag",
            "Your gamertag is 'KillerBreadMan'",
            "E2B read 'KillerBread/Man' once and the 12B 'KillerFreddMan'. "
            "The lobby frame below shows both players.",
            [7159],
            ZOOM_WIDTH,
        ),
        (
            "fact_opponent_spelling",
            "m2's opponent is 'xRaptoR72'",
            "SETTLED -- kept here for the record. OCR stored 'XRaptoR72' (wrong "
            "case); both gemma4 models read 'xRaptorR72' (one letter too many); "
            "the reviewer said 'xRaptoR72' and the pixels at 1900px agree. I had "
            "wrongly treated the two models' agreement as corroboration.",
            [7159],
            ZOOM_WIDTH,
        ),
        (
            "fact_m2_duration",
            "m2 lasted about 50 seconds and you surrendered",
            "A lobby frame shows ELAPSED TIME 00:45; the DEFEAT banner is at "
            "20:59:49 IST. Chat reads 'ill surrender to save time'.",
            [7171, 7173],
            CARD_WIDTH,
        ),
        (
            "fact_m2_result_screen",
            "m2 DID have a result screen — frame 7173, a full-screen DEFEAT banner",
            "This is the most important frame in the project. My label said m2 had "
            "no result screen. ScreenPipe stored ZERO OCR characters for it, so "
            "every text-based pipeline was blind to it. Please confirm it is real "
            "and belongs to the Mountain Clearing match.",
            [7173],
            ZOOM_WIDTH,
        ),
        (
            "fact_ladder",
            "Your ladder record was 97W/93L before this session",
            "Read off a menu frame. Before or after the four matches?",
            [6876],
            CARD_WIDTH,
        ),
        (
            "fact_m1_result",
            "m1 ended with the opponent (WLD6116) surrendering at 11:40",
            "SETTLED. I claimed I had never found a VICTORY banner for m1; that was "
            "wrong and the ground truth already listed the frames. 7063 reads "
            "'WLD6116 surrendered / has been eliminated' and 7064 reads '1 VICTORY'.",
            [7063, 7064, 7066],
            CARD_WIDTH,
        ),
    ]
    for fid, title, detail, frames, width in facts:
        items.append(
            {
                "id": fid,
                "section": "C. Specific facts",
                "priority": True,
                "title": title,
                "claim": "",
                "detail": detail,
                "frames": frames,
                "width": width,
                "kind": "confirm",
            }
        )

    # ---- D. what belongs in a day summary ---------------------------------
    for t in OTHER:
        items.append(
            {
                "id": f"keep_{t.key}",
                "section": "D. Would you want this in a summary of your day?",
                "priority": False,
                "title": t.title,
                "claim": f"currently tiered '{t.tier}'",
                "detail": (t.notes or "").split("\n")[0][:300],
                "frames": t.evidence[:3],
                "kind": "keep",
            }
        )
    extras = [
        (
            "keep_forestry",
            "An in-game research upgrade completing ('Forestry')",
            "gemma4 flagged this as a completed event. I assumed it is noise you "
            "would never want. Right?",
            [8753],
        ),
        (
            "keep_delivered",
            "Something reading 'Delivered' at 05:12",
            "gemma4 flagged it as a result. I could not tell what it was.",
            [9154],
        ),
        (
            "keep_audio_error",
            "An audio-recording error at 01:32",
            "gemma4 flagged it as a result.",
            [8230],
        ),
        (
            "keep_fma_granularity",
            "Seven FMA:B episodes — one event or seven?",
            "83 separate visits were counted across 17 episode pages. A summary "
            "could say 'watched 7 episodes of FMA:B' or list each.",
            [],
        ),
    ]
    for fid, title, detail, frames in extras:
        items.append(
            {
                "id": fid,
                "section": "D. Would you want this in a summary of your day?",
                "priority": False,
                "title": title,
                "claim": "",
                "detail": detail,
                "frames": frames,
                "kind": "keep",
            }
        )

    # ---- E. attention vs tab-open ----------------------------------------
    dash = None
    for f in arc.grep("Friend-Lite Dashboard", limit=400):
        if f.chunk_path:
            dash = f.id
            break
    items.append(
        {
            "id": "attention_dashboard",
            "section": "E. Attention",
            "priority": True,
            "title": "Friend-Lite Dashboard: 56 visits, 707 minutes of 'dwell'",
            "claim": "I suspect this is a parked tab, not 11.8 hours of attention.",
            "detail": (
                "Dwell here means contiguous capture runs where the window title "
                "matched. Browser frames come from the accessibility tree, which is "
                "read whether or not the window is visible. If this is a background "
                "tab, every dwell number in the site table is fiction."
            ),
            "frames": [dash] if dash else [],
            "kind": "confirm",
        }
    )

    # ---- G. the sessions played after the labelled day --------------------
    #
    # Fresh, never-tuned-against data. The four labelled matches are the day I
    # have been fitting to all along, so anything confirmed here is worth more
    # than another measurement on 24 Jul.
    G = "G. Last night's sessions (new data, never analysed)"
    items.append(
        {
            "id": "new_match_wyzvok",
            "section": G,
            "priority": True,
            "title": "25 Jul ~22:24–22:38 — you beat Wyzvok [FR], about 14:48",
            "claim": "outcome=victory · opponent=Wyzvok [FR] · duration ~14:48",
            "detail": (
                "Read from stored text: frame 10692 'Wyzvok [FR] has been eliminated "
                "/ has left the match', then a post-match summary titled Victory "
                "listing 'PLAYERS Wyzvok [FR] — Defeat' with a 00:00–14:48 timeline. "
                "Correct the fields if any of it is wrong."
            ),
            "frames": [10692, 10697, 10699],
            "kind": "confirm",
            "fields": ["opponent", "outcome", "duration", "map"],
            "prefill": {
                "opponent": "Wyzvok [FR]",
                "outcome": "victory",
                "duration": "~14:48",
                "map": "",
            },
        }
    )
    items.append(
        {
            "id": "new_session_count",
            "section": G,
            "priority": True,
            "title": "How many matches did you play on the night of 25 Jul?",
            "claim": "I can only confirm one (the Wyzvok win).",
            "detail": (
                "Fullscreen game stretches run 22:01–23:03 and on past midnight, "
                "which is long enough for two or three. I found exactly one result "
                "screen. If there were more, this is a clean test of whether my "
                "methods miss them — so the count matters more than the details."
            ),
            "frames": [],
            "kind": "text",
        }
    )
    items.append(
        {
            "id": "wyzvok_profile_attribution",
            "section": G,
            "priority": True,
            "title": "Frame 10210 is Wyzvok's profile, not yours — confirm?",
            "claim": "A match-history panel showing 4 results that are NOT yours.",
            "detail": (
                "It reads 'Wyzvok [FR], LEVEL: 60' with LATEST MATCHES: VICTORY!, "
                "DEFEAT, DEFEAT, VICTORY! — all 7/25/2026. Your own level shows as "
                "170 in the bar underneath, so I read this as you opening the "
                "opponent's profile. If that is right it is a new trap: the same "
                "screen type serves self and others, told apart only by a name in "
                "the corner. An extractor reading it naively would credit you with "
                "four matches that belong to someone else."
            ),
            "frames": [10210],
            "width": ZOOM_WIDTH,
            "kind": "confirm",
        }
    )

    # ---- F. configuration -------------------------------------------------
    cfg = [
        (
            "cfg_browser_url",
            "browser_url is empty on all 9,972 frames",
            "ScreenPipe has a browser-pairing flow (/connections/browser/pair). Did "
            "you ever set that up for Zen? If it can be enabled, site-visit counting "
            "gets real URLs and the ten title regexes I wrote become unnecessary.",
        ),
        (
            "cfg_ui_events",
            "ui_events has 0 rows",
            "Explained as three things failing at once: evdev permission (is your "
            "user in the 'input' group?), the --disable-clipboard-capture / "
            "--disable-keyboard-capture flags, and no KWin IPC on KDE Wayland. Did "
            "you pass those disable flags deliberately?",
        ),
        (
            "cfg_retention",
            "Retention is 90 days, mode 'media'",
            "Deliberate or default? It matters because mode 'lean' would delete the "
            "elements table that the salience signal depends on.",
        ),
        (
            "cfg_pipes",
            "No pipe has ever run (pipe_executions is empty)",
            "All eight are schedule: manual, template: true. Did you ever click run "
            "on one? If you did and it left no row, that is a ScreenPipe bug.",
        ),
        (
            "cfg_capture_gaps",
            "Capture has gaps: a 600s grid found frames at only 42 of 66 points",
            "Machine off, locked, asleep — or is ScreenPipe dropping capture? These "
            "have very different implications for whether a clock-based sampler can "
            "be trusted.",
        ),
        (
            "cfg_aoe4world",
            "Can you pull the authoritative match history?",
            "In-game history or an aoe4world.com profile would replace my hand-read "
            "labels entirely, for every session in the archive rather than one day. "
            "Paste anything you can get and I will re-score all six pipelines "
            "against it.",
        ),
    ]
    for fid, title, detail in cfg:
        items.append(
            {
                "id": fid,
                "section": "F. Configuration and data quality",
                "priority": True,
                "title": title,
                "claim": "",
                "detail": detail,
                "frames": [],
                "kind": "text",
            }
        )
    return items


# ---------------------------------------------------------------------- html

CSS = """
:root{--bg:#0f1115;--card:#181c23;--line:#2a3140;--fg:#e6e9ef;--dim:#9aa4b6;
--ok:#3fb950;--no:#f85149;--maybe:#d29922;--acc:#58a6ff}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--card:#fff;--line:#dfe3ea;
--fg:#1c2128;--dim:#57606a;--acc:#0969da}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:20;background:var(--card);
border-bottom:1px solid var(--line);padding:10px 18px;display:flex;
gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
.grow{flex:1}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;
padding:8px 14px;font-size:14px;cursor:pointer;font-weight:500}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
main{max-width:1080px;margin:0 auto;padding:20px 18px 120px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);
margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:14px}
.card.done{border-color:var(--ok)}
.t{font-weight:600;margin-bottom:4px}
.claim{color:var(--acc);font-size:14px;margin-bottom:6px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.detail{color:var(--dim);font-size:13.5px;margin-bottom:10px}
.shots{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;margin-bottom:10px}
.shots img{height:150px;border-radius:6px;border:1px solid var(--line);cursor:zoom-in}
.opts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.opts label{border:1px solid var(--line);border-radius:20px;padding:5px 12px;
font-size:13.5px;cursor:pointer;user-select:none}
.opts input{margin-right:6px}
.opts label:has(input:checked){border-color:var(--acc);background:rgba(88,166,255,.12)}
textarea{width:100%;background:transparent;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:8px;font:inherit;font-size:13.5px;min-height:42px;resize:vertical}
.fields{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.fields label{font-size:12px;color:var(--dim);display:flex;flex-direction:column;gap:3px}
.fields input{background:transparent;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;font:inherit;font-size:13.5px;min-width:150px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.94);display:none;z-index:50;
align-items:center;justify-content:center;cursor:zoom-out}
#lb img{max-width:98vw;max-height:98vh}
.pill{font-size:11.5px;color:var(--dim);border:1px solid var(--line);
border-radius:10px;padding:1px 7px}
.nofr{font-size:12.5px;color:var(--maybe);margin-bottom:8px}
"""

JS = """
const KEY='screenmem_review_v1';
// SEED is answers.json as it stood when this page was generated. Without it a
// regenerated page starts blank, which is exactly how 40 answers were lost once:
// a shadowed save() meant localStorage was never written, so a reload showed an
// empty form and the review was redone from scratch. localStorage wins per key,
// so nothing typed since generation is overwritten.
let A=Object.assign({}, SEED, JSON.parse(localStorage.getItem(KEY)||'{}'));
function touch(id){
  const c=document.getElementById('c_'+id); if(!c) return;
  const a=A[id]||{}; const has=a.verdict||a.comment||(a.fields&&Object.values(a.fields).some(v=>v));
  c.classList.toggle('done',!!has);
}
// Named cache(), not save(): an earlier version called this save() as well as
// the server-upload function, so the second declaration shadowed the first.
// Every keystroke then POSTed to the server and localStorage was never written,
// which meant a page reload would have silently discarded the whole review.
function cache(){localStorage.setItem(KEY,JSON.stringify(A));count();}
function set(id,k,v){A[id]=A[id]||{};A[id][k]=v;cache();touch(id);}
function setf(id,k,v){A[id]=A[id]||{};A[id].fields=A[id].fields||{};A[id].fields[k]=v;cache();touch(id);}
function count(){
  const n=Object.keys(A).filter(k=>{const a=A[k];
    return a.verdict||a.comment||(a.fields&&Object.values(a.fields).some(v=>v));}).length;
  document.getElementById('n').textContent=n+' / '+TOTAL+' answered';
}
function payload(){return JSON.stringify({saved:new Date().toISOString(),answers:A},null,1);}
function dl(){
  const blob=new Blob([payload()],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='screen-memory-review.json';a.click();
}
function status(msg,bad){const s=document.getElementById('st');
  s.textContent=msg;s.style.color=bad?'var(--no)':'var(--ok)';}
// Writes to out/review/answers.json via serve_review.py. Opened over file://
// there is no server to POST to, so fall back to a browser download rather than
// failing silently -- losing a completed review would be the worst outcome here.
async function save(){
  status('saving…');
  try{
    const r=await fetch('save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:payload()});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const j=await r.json();
    status('saved '+j.answered+' answers to answers.json');
  }catch(e){
    status('no server — downloading instead ('+e.message+')',true);
    dl();
  }
}
function reset(){if(confirm('Clear all answers?')){A={};cache();location.reload();}}
function onlyPri(b){document.querySelectorAll('.card').forEach(c=>{
  if(c.dataset.pri==='0') c.style.display=b?'none':'';});}
document.addEventListener('DOMContentLoaded',()=>{
  for(const [id,a] of Object.entries(A)){
    if(a.verdict){document.querySelectorAll(`input[name="v_${id}"]`)
      .forEach(x=>{if(x.value===a.verdict)x.checked=true;});}
    if(a.comment){const t=document.getElementById('t_'+id);if(t)t.value=a.comment;}
    if(a.fields)for(const [k,v] of Object.entries(a.fields)){
      const i=document.getElementById('f_'+id+'_'+k);if(i)i.value=v;}
    touch(id);
  }
  count();
  document.querySelectorAll('.shots img').forEach(im=>im.onclick=()=>{
    document.getElementById('lbi').src=im.dataset.full||im.src;
    document.getElementById('lb').style.display='flex';});
  document.getElementById('lb').onclick=()=>document.getElementById('lb').style.display='none';
});
"""


def card(it: dict) -> str:
    i = it["id"]
    opts = (
        it.get("options")
        or {
            "confirm": ["Correct", "Wrong", "Partly right", "Not sure"],
            "keep": ["Keep it", "Drop it", "Only if grouped", "Not sure"],
            "choice": it.get("options", []),
            "text": [],
        }[it["kind"]]
    )

    shots = ""
    if it.get("frames"):
        imgs = []
        for fn in it["frames"]:
            if fn:
                imgs.append(
                    f'<img src="png/{html.escape(fn)}" data-full="png/{html.escape(fn)}" loading="lazy">'
                )
        if imgs:
            shots = f'<div class="shots">{"".join(imgs)}</div>'
    elif it["kind"] != "text":
        shots = '<div class="nofr">No screenshot available for this one.</div>'

    radios = ""
    if opts:
        radios = (
            '<div class="opts">'
            + "".join(
                f'<label><input type="radio" name="v_{i}" value="{html.escape(o)}" '
                f"onchange=\"set('{i}','verdict',this.value)\">{html.escape(o)}</label>"
                for o in opts
            )
            + "</div>"
        )

    fields = ""
    if it.get("fields"):
        fields = (
            '<div class="fields">'
            + "".join(
                f'<label>{html.escape(f)}<input id="f_{i}_{f}" '
                f'value="{html.escape(it.get("prefill",{}).get(f,""))}" '
                f"oninput=\"setf('{i}','{f}',this.value)\"></label>"
                for f in it["fields"]
            )
            + "</div>"
        )

    claim = (
        f'<div class="claim">{html.escape(it["claim"])}</div>'
        if it.get("claim")
        else ""
    )
    detail = (
        f'<div class="detail">{html.escape(it["detail"])}</div>'
        if it.get("detail")
        else ""
    )
    ph = "Anything to add, correct, or explain"
    return f"""<div class="card" id="c_{i}" data-pri="{1 if it.get('priority') else 0}">
<div class="t">{html.escape(it["title"])} <span class="pill">{i}</span></div>
{claim}{detail}{shots}{fields}{radios}
<textarea id="t_{i}" placeholder="{ph}" oninput="set('{i}','comment',this.value)"></textarea>
</div>"""


def main() -> None:
    PNG.mkdir(parents=True, exist_ok=True)
    arc = open_archive()
    items = build_items(arc)

    print(f"{len(items)} items; exporting frames...")
    for it in items:
        w = it.get("width", CARD_WIDTH)
        it["frames"] = [export(arc, fid, w) for fid in (it.get("frames") or [])]

    sections: dict[str, list[dict]] = {}
    for it in items:
        sections.setdefault(it["section"], []).append(it)

    body = "".join(
        f"<h2>{html.escape(name)}</h2>" + "".join(card(i) for i in group)
        for name, group in sections.items()
    )

    # Carry any answers already on disk into the page, so regenerating never
    # presents a blank form over a completed review.
    seed: dict = {}
    answers_file = OUT / "answers.json"
    if answers_file.exists():
        try:
            seed = json.loads(answers_file.read_text()).get("answers") or {}
        except json.JSONDecodeError as exc:
            print(f"  answers.json unreadable, not seeding: {exc}")

    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Screen memory — review</title><style>{CSS}</style></head><body>
<header><h1>Screen memory · review</h1>
<span class="pill" id="n">0 answered</span>
<label class="pill"><input type="checkbox" onchange="onlyPri(this.checked)"> priority only</label>
<span class="pill" id="st"></span>
<span class="grow"></span>
<button onclick="save()">Save</button>
<button class="ghost" onclick="dl()">Download copy</button>
<button class="ghost" onclick="reset()">Clear</button></header>
<main>
<p style="color:var(--dim);font-size:13.5px;max-width:70ch">
Answers autosave to this browser as you go. Click any screenshot to enlarge.
Press <b>Save</b> whenever you like — partway is fine — and it writes to
<code>out/review/answers.json</code> where I can read it directly. Blank items
count as "no answer", not as agreement.<br>
<b>Section A1 is the one to start with.</b> My first pass over-called AoE4 badly:
it matched vocabulary across a whole fullscreen stretch and then showed you the
stretch's middle frame, which contained the matched words in only 1 of 10 cases.
Every card below now shows frames that are themselves the reason for the label,
with the matching words and that frame's own stored text quoted.</p>
{body}</main>
<div id="lb"><img id="lbi"></div>
<script>const TOTAL={len(items)};const SEED={json.dumps(seed)};{JS}</script></body></html>"""

    (OUT / "index.html").write_text(doc)
    seeded = len(seed)
    if seeded:
        print(f"seeded {seeded} existing answers from answers.json into the page")
    n_png = len(list(PNG.glob("*.png")))
    mb = sum(p.stat().st_size for p in PNG.glob("*.png")) / 1e6
    print(f"wrote {OUT/'index.html'}  ({len(items)} items, {n_png} pngs, {mb:.0f} MB)")


if __name__ == "__main__":
    main()
