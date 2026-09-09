"""
GrapheAI Hub - the front door of the platform.

One screen: every instrument with live running status and a start
button, corpus + spend + backup + nightly-job health, the next funding
deadlines, the latest PV Radar signals, and a unified search across
everything the platform has ever written into answers/.

Run with:
    streamlit run hub.py --server.port 8500

Makes NO Claude calls - it only reads the stores the other instruments
write, so it opens in a second and costs nothing.
"""

import datetime
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
ANSWERS_DIR = HERE / "answers"

THEME_FLAGS = ["--theme.base", "dark",
               "--theme.primaryColor", "#FF6B3D",
               "--theme.backgroundColor", "#12161C",
               "--theme.secondaryBackgroundColor", "#1B222B",
               "--theme.textColor", "#E6EAF0"]

INSTRUMENTS = [
    ("✍️", "Workbench", "workbench.py", 8501,
     "literature → manuscripts → proposals"),
    ("📡", "PV Radar", "pvradar.py", 8502,
     "news, patents & market intel"),
    ("🧱", "Material Library", "materials.py", 8503,
     "materials from the corpus"),
    ("🔋", "PeroDeg", "perodeg.py", 8504,
     "degradation & outdoor analytics"),
    ("📊", "Analytics", "analytics.py", 8505,
     "any-schema extraction + plots"),
    ("💶", "Funding Radar", "fundradar.py", 8506,
     "calls, deadlines & fit memos"),
    ("🎤", "Slide Studio", "slides.py", 8507,
     "decks from your outputs"),
    ("📉", "TechnoEcon", "technoecon.py", 8508,
     "LCOE & cost modelling"),
    ("🧪", "Experiment Planner", "expplan.py", 8509,
     "DoE + Bayesian suggestions"),
    ("📈", "Impact Tracker", "impact.py", 8510,
     "citations & track record"),
    ("🚀", "Venture Studio", "venture.py", 8511,
     "business case, IP & roadmap"),
]


def port_up(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def start_app(pyfile, port):
    subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(HERE / pyfile),
         "--server.port", str(port), "--server.headless", "true",
         *THEME_FLAGS],
        cwd=str(HERE), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _age_str(ts):
    if ts is None:
        return "never"
    delta = datetime.datetime.now() - ts
    h = delta.total_seconds() / 3600
    if h < 1:
        return f"{int(delta.total_seconds() / 60)} min ago"
    if h < 48:
        return f"{h:.0f} h ago"
    return f"{h / 24:.0f} days ago"


def file_age(path):
    p = Path(path)
    if not p.exists():
        return None
    return datetime.datetime.fromtimestamp(p.stat().st_mtime)


def newest_backup():
    icloud = (Path.home() / "Library" / "Mobile Documents"
              / "com~apple~CloudDocs" / "PaperRagBackups")
    local = Path.home() / "PaperRagBackups"
    zips = []
    for d in (icloud, local):
        if d.exists():
            zips += list(d.glob("paperrag_backup_*.zip"))
    if not zips:
        return None, None
    z = max(zips, key=lambda p: p.stat().st_mtime)
    return z, datetime.datetime.fromtimestamp(z.stat().st_mtime)


def launchd_status():
    """{label: 'ok'|'error <code>'} for the grapheai jobs (macOS)."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return {}
    jobs = {}
    for line in out.splitlines():
        if "grapheai" in line:
            parts = line.split()
            if len(parts) >= 3:
                code = parts[1]
                jobs[parts[2]] = ("ok" if code in ("0", "-")
                                  else f"exit {code}")
    return jobs


def days_left(deadline):
    try:
        d = datetime.date.fromisoformat(str(deadline)[:10])
        return (d - datetime.date.today()).days
    except Exception:
        return None


# --------------------------------------------------------------------------
# Maintenance helpers (no AI calls)
# --------------------------------------------------------------------------
GOLDEN_LAST = ANSWERS_DIR / "golden_runs_last.json"
MAX_CLI_MIN_VERSION = "2.1.251"

WHATS_NEW = [
    ("Workbench › Review › 🧬 Rewrite", "Library grounding: passages from your corpus cited as [Ln], "
     "positioning map, referee novelty check, 📚 Library tab with reference export"),
    ("Workbench › Review › 🧬 Rewrite", "CSV/XLSX data tables (and PeroDeg/Analytics exports) become "
     "file-backed data figures; figure-plan checkpoint; redraw any figure with feedback"),
    ("Workbench › Review › 🧬 Rewrite", "PV reporting checklist, submission packet (cover letter, "
     "reviewer profiles, limits check), Word Track Changes vs the original"),
    ("Workbench › Review › 📨 Respond to reviewers", "Reviews → approved point list → grounded responses → "
     "exact changes applied → before/after diff → Word bundle with Track Changes"),
    ("Workbench › every result", "💬 Follow-up conversation: say what is wrong, add facts, get exact "
     "edits applied and the document rebuilt"),
    ("Workbench › 🏆 Proposal", "ESR-calibrated mock evaluation (your past ESRs), consistency audit "
     "(objectives × WPs, dates, effort), Gantt & WP figures + tables"),
    ("Workbench › 📬 Watch", "New papers checked against manuscripts, reviews and proposals in progress"),
    ("Workbench › 🖼️ Figures › Figure Studio", "PNG + SVG + PDF, journal panel styles, visual QA on the "
     "API backend and through Claude Code on the Max backend"),
    ("Venture Studio › Roadmap", "Grant-to-startup roadmap with decision gates; milestones join the Gantt"),
    ("All apps", "Newest Claude Code found is used for Max mode; version shown in the sidebar"),
    ("Here (Hub)", "Golden runs self-test, index freshness, re-index, backup now, jobs in progress"),
]


def run_golden():
    """Run golden_runs.py, parse PASS/FAIL lines, persist the result."""
    script = HERE / "golden_runs.py"
    if not script.exists():
        return {"error": "golden_runs.py not found next to hub.py"}
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(HERE),
                              capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "golden runs exceeded 15 minutes"}
    rows = []
    for ln in (proc.stdout or "").splitlines():
        m = re.match(r"\s*(PASS|FAIL)\s+(.+?)\s{2,}(\S.*)$", ln)
        if m:
            rows.append({"status": m.group(1), "check": m.group(2).strip(),
                         "info": m.group(3).strip()})
    rec = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
           "rows": rows, "exit": proc.returncode,
           "passed": sum(1 for r in rows if r["status"] == "PASS"), "total": len(rows),
           "tail": (proc.stdout or "")[-1500:] + ("\n" + proc.stderr[-1500:] if proc.stderr else "")}
    try:
        ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_LAST.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    except Exception:
        pass
    return rec


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def claude_code_versions():
    """[(origin, path, version)] for the Claude Code binaries Max mode can use."""
    import shutil
    out = []
    cands = []
    try:
        import claude_agent_sdk
        b = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if b.is_file():
            cands.append(("bundled in claude-agent-sdk", str(b)))
    except Exception:
        pass
    home = Path.home()
    for p in [shutil.which("claude"), home / ".claude/local/claude", home / ".npm-global/bin/claude",
              "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
        p = str(p) if p else ""
        if p and Path(p).is_file() and p not in {c[1] for c in cands}:
            cands.append(("installed claude", p))
    for origin, p in cands:
        try:
            v = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
            out.append((origin, p, m.group(0) if m else "?"))
        except Exception:
            out.append((origin, p, "?"))
    return out


def package_versions():
    vers = {}
    for name in ("anthropic", "claude_agent_sdk", "openai", "streamlit", "chromadb", "matplotlib", "openpyxl"):
        try:
            mod = __import__(name)
            vers[name] = getattr(mod, "__version__", "installed")
        except Exception:
            vers[name] = "not installed"
    return vers


def index_freshness():
    """PDF count on disk, files newer than the index, index age."""
    try:
        import config
        pdf_dir = Path(config.PDF_DIR)
        db_dir = Path(config.DB_DIR)
    except Exception:
        return None
    files = [p for p in pdf_dir.rglob("*") if p.is_file()
             and p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")] if pdf_dir.exists() else []
    idx_ts = None
    if db_dir.exists():
        try:
            idx_ts = max((p.stat().st_mtime for p in db_dir.rglob("*") if p.is_file()), default=None)
        except Exception:
            idx_ts = None
    newer = [p for p in files if idx_ts and p.stat().st_mtime > idx_ts]
    return {"n_files": len(files), "index_time": datetime.datetime.fromtimestamp(idx_ts) if idx_ts else None,
            "newer": newer[:20], "n_newer": len(newer), "pdf_dir": pdf_dir}


def jobs_in_progress():
    rows = []
    for sub, kind in (("rewrite_jobs", "rewrite"), ("review_jobs", "review / career doc"),
                      ("revision_jobs", "response to reviewers")):
        d = ANSWERS_DIR / sub
        if not d.exists():
            continue
        for p in sorted(d.glob("*/state.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:30]:
            s = _read_json(p, {})
            if not s:
                continue
            inp, opts = s.get("inputs", {}) or {}, s.get("opts", {}) or {}
            name = (inp.get("ms_name") or opts.get("synopsis") or opts.get("brief") or p.parent.name)
            rows.append({"kind": kind, "document": str(name)[:70], "status": s.get("status", "?"),
                         "updated": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                         "figures": len((s.get("stages", {}) or {}).get("figures", {}) or {}),
                         "job": p.parent.name})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


def dir_size_mb(path):
    try:
        return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file()) / 1e6
    except Exception:
        return 0.0


def start_background(pyfile, logname):
    """Run a maintenance script detached, logging to answers/<logname>."""
    log = ANSWERS_DIR / logname
    ANSWERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        fh.write(f"\n=== started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n".encode())
        subprocess.Popen([sys.executable, str(HERE / pyfile)], cwd=str(HERE),
                         stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    return log


# --------------------------------------------------------------------------
# Unified search over answers/
# --------------------------------------------------------------------------
SEARCH_EXT = (".md", ".txt", ".csv", ".docx")
MAX_FILE_BYTES = 2_000_000


def _answers_signature():
    if not ANSWERS_DIR.exists():
        return 0
    sig = 0
    for p in ANSWERS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in SEARCH_EXT:
            sig ^= hash((str(p), p.stat().st_mtime_ns))
    return sig


@st.cache_data(show_spinner=False)
def load_search_corpus(signature):
    """[(relpath, mtime, text)] for every searchable file in answers/."""
    del signature  # cache key only
    out = []
    if not ANSWERS_DIR.exists():
        return out
    for p in sorted(ANSWERS_DIR.rglob("*"),
                    key=lambda q: q.stat().st_mtime if q.is_file()
                    else 0, reverse=True):
        if (not p.is_file() or p.suffix.lower() not in SEARCH_EXT
                or p.stat().st_size > MAX_FILE_BYTES
                or p.name.startswith("~$")):
            continue
        try:
            if p.suffix.lower() == ".docx":
                from docx import Document
                doc = Document(p)
                text = "\n".join(q.text for q in doc.paragraphs
                                 if q.text.strip())
            else:
                text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        out.append((str(p.relative_to(ANSWERS_DIR)),
                    p.stat().st_mtime, text))
        if len(out) >= 3000:
            break
    return out


def search_answers(query, corpus, limit=40):
    ql = query.lower()
    hits = []
    for rel, mtime, text in corpus:
        low = text.lower()
        idx = low.find(ql)
        in_name = ql in rel.lower()
        if idx == -1 and not in_name:
            continue
        if idx == -1:
            snippet = text[:220]
        else:
            a = max(0, idx - 110)
            snippet = ("…" if a else "") + text[a:idx + 160] + "…"
        snippet = re.sub(r"\s+", " ", snippet).strip()
        hits.append({"file": rel, "mtime": mtime, "snippet": snippet,
                     "count": low.count(ql) + (1 if in_name else 0)})
        if len(hits) >= limit:
            break
    return hits


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
st.set_page_config(page_title="GrapheAI Hub", page_icon="🏠",
                   layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');
html, body, [class*="css"], .stMarkdown, p, li {
    font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
    font-size: 16.5px; color: #E6EAF0;
}
.stApp { background: #12161C; }
.an-header { border-bottom: 3px solid #FF6B3D; padding-bottom: 12px;
             margin-bottom: 10px; }
.an-title  { font-family: 'IBM Plex Serif', Georgia, serif;
             font-size: 2.6rem; font-weight: 600; color: #F4F6F9;
             margin: 0; letter-spacing: -0.5px; }
.an-sub    { font-size: 0.82rem; color: #8E99A8; margin-top: 6px;
             text-transform: uppercase; letter-spacing: 2px; }
.an-sub b  { color: #FF8A5C; font-weight: 600; }
h2 { font-family: 'IBM Plex Serif', Georgia, serif; font-weight: 600;
     color: #DFE5EC; }
h3 { color: #C3CBD6; }
.tool-card { background: #1A212B; border: 1px solid #263140;
    border-radius: 14px; padding: 14px 16px 10px 16px;
    margin-bottom: 10px; }
.tool-name { font-size: 1.12rem; font-weight: 600; color: #F4F6F9; }
.tool-desc { font-size: .85rem; color: #8E99A8; margin: 2px 0 8px 0; }
.dot-on  { color: #4ADE80; } .dot-off { color: #5A6675; }
[data-testid="stMetric"] { background: #1A212B; border: 1px solid #263140;
    border-radius: 12px; padding: .7rem .95rem; }
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace;
                                color: #FF8A5C; font-size: 1.35rem; }
[data-testid="stMetricLabel"] { color: #8E99A8; text-transform: uppercase;
    letter-spacing: 1px; font-size: .78rem; }
.stButton>button { font-size: .92rem; font-weight: 600;
    border-radius: 10px; padding: 0.3rem 0.9rem; }
details { border: 1px solid #263140; border-radius: 12px;
          background: #1A212B; }
[data-testid="stDataFrame"] { border: 1px solid #263140;
                              border-radius: 12px; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='an-header'>"
    "<p class='an-title'>🏠 GrapheAI</p>"
    "<p class='an-sub'>research platform hub · developed by "
    "<b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

# ------------------------------ health row --------------------------------
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    try:
        import chromadb
        import config
        client = chromadb.PersistentClient(path=str(config.DB_DIR))
        n_chunks = client.get_collection(config.COLLECTION_NAME).count()
        st.metric("Corpus chunks", f"{n_chunks:,}")
    except Exception:
        st.metric("Corpus chunks", "—")

with c2:
    spend = _read_json(ANSWERS_DIR / "spend.json", {})
    month = datetime.date.today().strftime("%Y-%m")
    st.metric("API spend " + month,
              f"${float(spend.get(month, 0.0)):.2f}")

with c3:
    zpath, zts = newest_backup()
    st.metric("Last backup", _age_str(zts))

with c4:
    st.metric("Radar refresh",
              _age_str(file_age(ANSWERS_DIR / "radar_refresh.log")))

with c5:
    st.metric("Analytics refresh",
              _age_str(file_age(ANSWERS_DIR / "analytics_refresh.log")))

jobs = launchd_status()
if jobs:
    bad = {k: v for k, v in jobs.items() if v != "ok"}
    if bad:
        st.error("launchd job problem: "
                 + ", ".join(f"{k} ({v})" for k, v in bad.items()))
    else:
        st.caption("launchd: " + " · ".join(f"{k} ✓" for k in jobs))
else:
    st.caption("launchd status unavailable (not macOS, or launchctl "
               "not readable).")

st.markdown("---")

left, right = st.columns([3, 2], gap="large")

# ------------------------------ instruments -------------------------------
with left:
    st.markdown("## Instruments")
    if st.button("🔄 Refresh statuses", key="hub_refresh"):
        st.rerun()
    for row_start in range(0, len(INSTRUMENTS), 2):
        cols = st.columns(2)
        for col, inst in zip(cols,
                             INSTRUMENTS[row_start:row_start + 2]):
            emoji, name, pyfile, port, desc = inst
            exists = (HERE / pyfile).exists()
            up = port_up(port) if exists else False
            with col:
                dot = ("<span class='dot-on'>●</span>" if up
                       else "<span class='dot-off'>●</span>")
                st.markdown(
                    f"<div class='tool-card'>"
                    f"<div class='tool-name'>{dot} {emoji} {name} "
                    f"<span style='color:#5A6675;font-size:.8rem'>"
                    f":{port}</span></div>"
                    f"<div class='tool-desc'>{desc}</div></div>",
                    unsafe_allow_html=True)
                b1, b2 = st.columns(2)
                with b1:
                    if up:
                        st.link_button("↗ Open",
                                       f"http://localhost:{port}",
                                       use_container_width=True)
                    elif exists:
                        if st.button("▶ Start", key=f"start_{port}",
                                     use_container_width=True):
                            start_app(pyfile, port)
                            st.toast(f"Starting {name} — give it a few "
                                     "seconds, then Refresh statuses.")
                    else:
                        st.button("missing", key=f"miss_{port}",
                                  disabled=True,
                                  use_container_width=True)

# ------------------------------ right column ------------------------------
with right:
    st.markdown("## Next deadlines")
    board = _read_json(ANSWERS_DIR / "funding" / "board.json",
                       {}).get("board", [])
    upcoming = []
    for c in board:
        if c.get("status") == "Dropped":
            continue
        dl = days_left(c.get("deadline"))
        if dl is not None and dl >= 0:
            upcoming.append((dl, c))
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        st.caption("No upcoming deadlines on the Funding Radar board.")
    for dl, c in upcoming[:4]:
        urgent = "🔴" if dl <= 30 else ("🟠" if dl <= 90 else "🟢")
        st.markdown(
            f"{urgent} **{dl} days** — "
            f"{(c.get('identifier') or '')} "
            f"{str(c.get('title', ''))[:70]}")

    st.markdown("## Latest radar signals")
    news = _read_json(ANSWERS_DIR / "pv_radar.json", [])
    if isinstance(news, list) and news:
        def _dt(item):
            return str(item.get("date", ""))
        for item in sorted(news, key=_dt, reverse=True)[:5]:
            st.markdown(
                f"- **{str(item.get('category', '?'))}** · "
                f"{str(item.get('title', ''))[:90]} "
                f"<span style='color:#5A6675;font-size:.8rem'>"
                f"{str(item.get('date', ''))[:10]}</span>",
                unsafe_allow_html=True)
    else:
        st.caption("No PV Radar items yet.")

st.markdown("---")

# ------------------------------ maintenance & health ----------------------
st.markdown("## 🧪 Maintenance & health")
m1, m2, m3 = st.columns([2, 2, 2], gap="large")

with m1:
    st.markdown("### Golden runs")
    st.caption("Nine deterministic checks of the Workbench machinery (sandbox, audits, "
               "figure stage, checklist, consistency, edits, diff, library, Track "
               "Changes). No AI calls; ~20 s. Run after every update.")
    last = _read_json(GOLDEN_LAST, None)
    if st.button("▶ Run golden runs now", key="golden_go", use_container_width=True):
        with st.spinner("Running golden runs..."):
            last = run_golden()
    if last:
        if last.get("error"):
            st.error(last["error"])
        else:
            ok = last.get("passed", 0) == last.get("total", 0) and last.get("total", 0) > 0
            (st.success if ok else st.error)(
                f"{last.get('passed', 0)}/{last.get('total', 0)} passed · "
                f"{str(last.get('time', ''))[:16].replace('T', ' ')}")
            if last.get("rows"):
                st.dataframe(last["rows"], use_container_width=True, hide_index=True)
            if not ok:
                with st.expander("Output"):
                    st.code(last.get("tail", ""), language=None)
    else:
        st.caption("Never run.")

with m2:
    st.markdown("### Library index")
    fr = index_freshness()
    if fr:
        st.metric("Documents in papers/", f"{fr['n_files']:,}")
        st.caption(f"Index built {_age_str(fr['index_time'])}"
                   + (f" · **{fr['n_newer']} file(s) newer than the index**" if fr["n_newer"] else
                      " · nothing newer than the index"))
        if fr["n_newer"]:
            with st.expander("Newer files"):
                for p in fr["newer"]:
                    st.caption(str(p.relative_to(fr["pdf_dir"])))
    else:
        st.caption("config.py not importable - index status unavailable.")
    if st.button("🔁 Re-index the library (background)", key="reindex_go",
                 use_container_width=True, disabled=not (HERE / "ingest.py").exists()):
        log = start_background("ingest.py", "ingest_hub.log")
        st.toast(f"Indexing started - log: {log.name}")
    if st.button("💾 Back up now (background)", key="backup_go", use_container_width=True,
                 disabled=not (HERE / "backup.py").exists()):
        log = start_background("backup.py", "backup_hub.log")
        st.toast(f"Backup started - log: {log.name}")
    for logname in ("ingest_hub.log", "backup_hub.log"):
        lp = ANSWERS_DIR / logname
        if lp.exists():
            with st.expander(f"{logname} ({_age_str(file_age(lp))})"):
                try:
                    st.code(lp.read_text(encoding="utf-8", errors="replace")[-2500:], language=None)
                except Exception:
                    pass

with m3:
    st.markdown("### Claude access & versions")
    ccs = claude_code_versions()
    if ccs:
        best = max(ccs, key=lambda c: _ver_tuple(c[2]))
        ok = _ver_tuple(best[2]) >= _ver_tuple(MAX_CLI_MIN_VERSION)
        (st.success if ok else st.warning)(
            f"Claude Code {best[2]} ({best[0]}) - "
            + ("fine for Fable 5.1" if ok else
               f"older than {MAX_CLI_MIN_VERSION}: run "
               f"`{sys.executable} -m pip install -U claude-agent-sdk`"))
        if len(ccs) > 1:
            st.caption(" · ".join(f"{o}: {v}" for o, _p, v in ccs))
    else:
        st.caption("No Claude Code found (Max mode needs `pip install claude-agent-sdk`).")
    pv = package_versions()
    st.caption(" · ".join(f"{k} {v}" for k, v in pv.items()))
    st.caption(f"answers/ {dir_size_mb(ANSWERS_DIR):,.0f} MB")
    try:
        import config as _cfg
        st.caption(f"chroma_db/ {dir_size_mb(_cfg.DB_DIR):,.0f} MB")
    except Exception:
        pass

jobs_rows = jobs_in_progress()
if jobs_rows:
    open_rows = [r for r in jobs_rows if r["status"] != "complete"]
    st.markdown(f"### Jobs ({len(open_rows)} in progress, {len(jobs_rows) - len(open_rows)} "
                "completed recently)")
    st.caption("Resume an unfinished job from its panel in the Workbench (Review › Rewrite / "
               "Respond, Draft › Review writer, Career).")
    st.dataframe(jobs_rows[:20], use_container_width=True, hide_index=True)

with st.expander("✨ What's new in this build"):
    for where, what in WHATS_NEW:
        st.markdown(f"- **{where}** - {what}")
    guide = ANSWERS_DIR / "SYSTEM_GUIDE.md"
    if guide.exists():
        st.caption("Full guide: answers/SYSTEM_GUIDE.md")

st.markdown("---")

# ------------------------------ unified search ----------------------------
st.markdown("## 🔍 Search everything GrapheAI has written")
st.caption("Full-text search across every report, memo, dataset, note "
           "and answer in answers/ — Markdown, text, CSV and Word.")
q = st.text_input("Search", key="hub_q",
                  placeholder="e.g. buried interface, LAPERITIVO, "
                              "T80, NiO ...")
if q and len(q.strip()) >= 2:
    with st.spinner("Searching..."):
        corpus = load_search_corpus(_answers_signature())
        hits = search_answers(q.strip(), corpus)
    st.caption(f"{len(hits)} file(s) matched"
               + (" (showing first 40)" if len(hits) >= 40 else ""))
    for h in hits:
        ts = datetime.datetime.fromtimestamp(h["mtime"])
        with st.expander(f"📄 {h['file']} — {h['count']} match(es) · "
                         f"{ts:%Y-%m-%d}"):
            st.markdown(h["snippet"])
            st.code(str(ANSWERS_DIR / h["file"]), language=None)
elif q:
    st.caption("Type at least 2 characters.")

st.markdown("---")
st.caption("GrapheAI Hub makes no AI calls - it only reads what the "
           "other instruments store. Guide: answers/SYSTEM_GUIDE.md")
