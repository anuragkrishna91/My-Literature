"""
Impact Tracker - by GrapheAI. Your publication record, live from
OpenAlex (the open scholarly database): citations over time, h-index,
paper-by-paper counts, who cites you, and an always-current track-record
paragraph for applications.

Run with:
    streamlit run impact.py --server.port 8510

Data source: the free OpenAlex API (api.openalex.org) - fully open,
no key, no scraping. A refresh makes ~15-20 small requests.

Shares with the rest of GrapheAI:
  - answers/impact/             cached author record, works, citers
  - answers/spend.json          monthly API spend + budget
  - the Claude backend          (only for the Track-record tab)
"""

import datetime
import json
import re
from pathlib import Path

import streamlit as st

ANSWERS_DIR = Path("answers")
IMPACT_DIR = ANSWERS_DIR / "impact"

MODELS = {
    "Fast (Haiku 4.5)": "claude-haiku-4-5",
    "Balanced (Sonnet 5)": "claude-sonnet-5",
    "Deep (Opus 5)": "claude-opus-5",
    "Frontier (Fable 5.1)": "claude-fable-5-1",
}

PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
}

SPEND_FILE = ANSWERS_DIR / "spend.json"


def _load_spend():
    if SPEND_FILE.exists():
        try:
            return json.loads(SPEND_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_spend(data):
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        SPEND_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _add_spend(amount):
    if amount <= 0:
        return
    data = _load_spend()
    key = datetime.date.today().strftime("%Y-%m")
    data[key] = round(float(data.get(key, 0.0)) + amount, 5)
    _write_spend(data)


def _track_usage(n_in, n_out, model=None):
    u = st.session_state.setdefault("usage",
                                    {"in": 0, "out": 0, "calls": 0,
                                     "cost": 0.0})
    u["in"] += n_in
    u["out"] += n_out
    u["calls"] += 1
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    delta = n_in / 1e6 * p_in + n_out / 1e6 * p_out
    u["cost"] = u.get("cost", 0.0) + delta
    if st.session_state.get("backend") != "max":
        _add_spend(delta)


def _claude_cli_candidates():
    """Claude Code binaries the Agent SDK could run: the copy bundled inside
    claude-agent-sdk (which the SDK prefers by default) and any installed
    `claude` on PATH or in the usual locations."""
    import os
    import shutil
    from pathlib import Path
    cands = []
    try:
        import claude_agent_sdk
        b = (Path(claude_agent_sdk.__file__).parent / "_bundled"
             / ("claude.exe" if os.name == "nt" else "claude"))
        if b.is_file():
            cands.append(("bundled in claude-agent-sdk", str(b)))
    except Exception:
        pass
    seen = {c[1] for c in cands}
    home = Path.home()
    for p in [shutil.which("claude"), home / ".claude/local/claude",
              home / ".npm-global/bin/claude", "/usr/local/bin/claude",
              "/opt/homebrew/bin/claude", home / ".local/bin/claude"]:
        p = str(p) if p else ""
        if p and Path(p).is_file() and p not in seen:
            cands.append(("installed claude", p))
            seen.add(p)
    return cands


def _claude_cli_version(path):
    import re
    import subprocess
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=20).stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
        return m.group(0) if m else ""
    except Exception:
        return ""


def _ver_tuple(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


MAX_CLI_MIN_VERSION = "2.1.251"      # Fable 5.1 needs at least this Claude Code


def claude_code_status(refresh=False):
    """The newest Claude Code found (path, version, origin), cached for the
    session. Max mode runs through this binary, so a stale copy bundled in
    an old claude-agent-sdk no longer blocks new models when a newer
    `claude` is installed - and vice versa."""
    key = "_claude_code_status"
    if not refresh and key in st.session_state:
        return st.session_state[key]
    best = {}
    for origin, p in _claude_cli_candidates():
        v = _claude_cli_version(p)
        if v and (not best or _ver_tuple(v) > _ver_tuple(best["version"])):
            best = {"path": p, "version": v, "origin": origin}
    st.session_state[key] = best
    return best


def _max_update_command():
    import sys
    return f"{sys.executable} -m pip install -U claude-agent-sdk"


def _max_unsupported_hint(err, model):
    """Turn Claude Code's 'does not support this model; version X or newer
    is required' into a message that says exactly what to run."""
    import re
    m = re.search(r"does not support this model;?\s*version\s*([\d.]+)\s*or newer",
                  str(err), re.I)
    if not m:
        return None
    status = claude_code_status()
    have = status.get("version", "unknown")
    return (f"Claude Code {have} ({status.get('origin', 'not found')}) is too old "
            f"for {model}: version {m.group(1)} or newer is required. Fix in "
            f"Terminal, then restart the app:\n    {_max_update_command()}\n"
            "(claude-agent-sdk ships its own Claude Code; that command updates "
            "it. `claude update` works too - GrapheAI uses the newest Claude "
            "Code it finds.) Until then choose 'Fast (Haiku 4.5)' or switch the "
            "sidebar to 'API key'.")


def call_claude_max(system, user_msg, model, max_tokens=None):
    """Run one Claude call through the Claude Agent SDK (Claude Code), so it
    counts against the user's Claude Max subscription instead of API billing.

    Needs one-time setup (see MAX_SETUP.txt): `pip install claude-agent-sdk`
    (it bundles Claude Code) and a Claude Code login with the Max account.
    Personal use of your own subscription only. max_tokens is not enforced on
    this path (the CLI manages output length itself). New models need a
    recent Claude Code: the app runs the newest copy it can find.
    """
    try:
        from claude_agent_sdk import (query, ClaudeAgentOptions,
                                      AssistantMessage, TextBlock,
                                      ResultMessage)
    except ImportError as exc:
        raise RuntimeError(
            "Claude Max mode needs the Agent SDK. One-time setup (Terminal):\n"
            f"1) {_max_update_command()}\n"
            "2) Run `claude` once and log in with your Claude (Max) account\n"
            "Or switch the sidebar back to 'API key'. See MAX_SETUP.txt."
        ) from exc
    import asyncio
    status = claude_code_status()
    extra = {"cli_path": status["path"]} if status.get("path") else {}

    async def _run():
        opts = ClaudeAgentOptions(
            system_prompt=system,
            model=model,
            max_turns=1,
            # Pure text generation - keep Claude Code's tools out of the way.
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep",
                              "WebSearch", "WebFetch", "Task", "NotebookEdit"],
            **extra,
        )
        parts = []
        usage = None
        async for message in query(prompt=user_msg, options=opts):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                usage = getattr(message, "usage", None)
                if getattr(message, "is_error", False):
                    raise RuntimeError(getattr(message, "result", None)
                                       or "Claude Code returned an error.")
        return "".join(parts), usage

    try:
        text, usage = asyncio.run(_run())
    except Exception as exc:
        hint = _max_unsupported_hint(exc, model)
        if hint:
            raise RuntimeError(hint) from exc
        raise
    if not text.strip():
        raise RuntimeError(
            "Claude Code returned no text. If this mentions usage limits, "
            "your Max plan may be at its cap - switch the sidebar to "
            "'API key' to continue, or wait for the limit to reset.")
    try:
        _track_usage(int(usage.get("input_tokens", 0)),
                     int(usage.get("output_tokens", 0)))
    except Exception:
        _track_usage(0, 0)
    return text


THINKING_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5",
                   "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")


def _effective_max_tokens(model, requested):
    mt = requested or 1500
    if any(str(model).startswith(m) for m in THINKING_MODELS):
        mt = max(mt, 32000)
    return mt


def _response_text(resp):
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("Claude declined this request.")
    parts = [b.text for b in resp.content
             if getattr(b, "type", "") == "text" and getattr(b, "text", "")]
    if not parts:
        raise RuntimeError("The model returned no text.")
    return "".join(parts)


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
STREAM_ABOVE = 8000      # stream responses above this many output tokens


def _model_family(model):
    m = str(model)
    if m.startswith(("claude-fable-5", "claude-mythos-5")):
        return "fable"          # thinking always on; depth via effort
    if m.startswith("claude-opus-5"):
        return "opus5"          # adaptive by default; effort accepted
    if m.startswith(("claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")):
        return "adaptive"       # must ask for adaptive thinking
    if m.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
        return "adaptive46"     # adaptive; effort up to 'high'/'max'
    return "classic"            # Haiku 4.5 and older: no thinking


def _api_kwargs(model, system, user_msg, max_tokens):
    """Request parameters tuned per model family. Fable 5.1: thinking is
    always on and its depth is set with output_config.effort (the
    sidebar's 'Reasoning effort'); Opus 5 the same; Opus 4.8 / Sonnet 5
    need adaptive thinking requested explicitly; Haiku gets none."""
    fam = _model_family(model)
    kw = {"model": model,
          "max_tokens": _effective_max_tokens(model, max_tokens),
          "system": system,
          "messages": [{"role": "user", "content": user_msg}]}
    effort = st.session_state.get("effort", "xhigh")
    if effort not in EFFORT_LEVELS:
        effort = "xhigh"
    if fam in ("fable", "opus5"):
        kw["output_config"] = {"effort": effort}
    elif fam == "adaptive":
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": effort}
    elif fam == "adaptive46":
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "high" if effort == "xhigh"
                               else effort}
    return kw


def _api_create(client, kw):
    """Send the request with graceful degradation: refusal fallbacks
    (beta), then effort/adaptive thinking, then the bare request - so an
    older installed SDK or a model that rejects a parameter still works.
    Long outputs are streamed to avoid HTTP timeouts."""
    import anthropic
    stream = kw["max_tokens"] > STREAM_ABOVE
    fam = _model_family(kw["model"])
    attempts = []
    if fam in ("fable", "opus5") and not st.session_state.get("_no_fallbacks"):
        attempts.append(("beta", dict(kw, betas=["server-side-fallback-2026-07-01"],
                                      fallbacks="default")))
    attempts.append(("plain", dict(kw)))
    attempts.append(("minimal", {k: kw[k] for k in
                                 ("model", "max_tokens", "system", "messages")}))
    last = None
    for kind, params in attempts:
        try:
            api = client.beta.messages if kind == "beta" else client.messages
            if stream:
                with api.stream(**params) as s:
                    return s.get_final_message()
            return api.create(**params)
        except TypeError as e:                 # installed SDK lacks a parameter
            last = e
            if kind == "beta":
                st.session_state["_no_fallbacks"] = True
            continue
        except anthropic.BadRequestError as e:
            last = e
            low = str(e).lower()
            if kind == "beta":
                st.session_state["_no_fallbacks"] = True
                continue
            if kind == "plain" and any(w in low for w in
                                       ("output_config", "effort", "thinking",
                                        "fallback", "beta", "unexpected")):
                continue
            raise
    raise last


def call_claude(api_key, system, user_msg, model, max_tokens=None):
    """Single Claude call via the selected backend; tracks token usage.
    Backend 'api': the Anthropic API (Fable 5.1 with effort-controlled
    reasoning, streaming, refusal fallbacks). Backend 'max': the Claude
    Agent SDK, billed to the Claude Max plan."""
    if st.session_state.get("backend") == "max":
        return call_claude_max(system, user_msg, model, max_tokens)
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = _api_create(client, _api_kwargs(model, system, user_msg,
                                           max_tokens))
    _track_usage(resp.usage.input_tokens, resp.usage.output_tokens, model)
    return _response_text(resp)


# --------------------------------------------------------------------------
# OpenAlex client (pure functions, defensively parsed)
# --------------------------------------------------------------------------
OA_BASE = "https://api.openalex.org"
# OpenAlex offers a faster "polite pool" if you add &mailto=you@example.com
# to requests - add your email below if you want that; it is optional.
OA_MAILTO = ""


def _oa_get(path, params=None):
    import requests
    params = dict(params or {})
    if OA_MAILTO:
        params["mailto"] = OA_MAILTO
    r = requests.get(f"{OA_BASE}/{path.lstrip('/')}", params=params,
                     timeout=40,
                     headers={"User-Agent": "GrapheAI-ImpactTracker"})
    r.raise_for_status()
    return r.json()


def oa_search_authors(name):
    data = _oa_get("authors", {"search": name, "per-page": "10"})
    out = []
    for a in data.get("results", []):
        inst = ""
        lki = a.get("last_known_institutions") or []
        if lki:
            inst = lki[0].get("display_name", "")
        stats = a.get("summary_stats") or {}
        out.append({
            "id": a.get("id", ""),
            "name": a.get("display_name", "?"),
            "institution": inst,
            "works": a.get("works_count", 0),
            "citations": a.get("cited_by_count", 0),
            "h_index": stats.get("h_index"),
            "orcid": a.get("orcid") or "",
        })
    return out


def oa_get_author(author_id):
    aid = author_id.rsplit("/", 1)[-1]
    return _oa_get(f"authors/{aid}")


def oa_get_works(author_id, max_pages=6):
    """All works for the author (cursor-paged, 200/page)."""
    aid = author_id.rsplit("/", 1)[-1]
    works, cursor = [], "*"
    fields = ("id,display_name,publication_year,cited_by_count,doi,"
              "type,primary_location,counts_by_year,authorships")
    for _ in range(max_pages):
        data = _oa_get("works", {
            "filter": f"authorships.author.id:{aid}",
            "per-page": "200", "cursor": cursor, "select": fields,
            "sort": "cited_by_count:desc"})
        for w in data.get("results", []):
            loc = w.get("primary_location") or {}
            src = loc.get("source") or {}
            works.append({
                "id": w.get("id", ""),
                "title": w.get("display_name", "?"),
                "year": w.get("publication_year"),
                "citations": w.get("cited_by_count", 0),
                "doi": (w.get("doi") or "").replace(
                    "https://doi.org/", ""),
                "type": w.get("type", ""),
                "venue": src.get("display_name", ""),
                "counts_by_year": w.get("counts_by_year", []),
                "n_authors": len(w.get("authorships") or []),
            })
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return works


def oa_who_cites(works, top_n=12):
    """Aggregate citing institutions + citing authors over the top-N
    cited works (one group_by request each per work)."""
    insts, authors = {}, {}
    top = sorted(works, key=lambda w: -w["citations"])[:top_n]
    errors = 0
    for w in top:
        wid = w["id"].rsplit("/", 1)[-1]
        if not wid:
            continue
        try:
            gi = _oa_get("works", {
                "filter": f"cites:{wid}",
                "group_by": "authorships.institutions.lineage",
                "per-page": "25"})
            for g in gi.get("group_by", []):
                k = g.get("key_display_name") or "?"
                insts[k] = insts.get(k, 0) + int(g.get("count", 0))
            ga = _oa_get("works", {
                "filter": f"cites:{wid}",
                "group_by": "authorships.author.id",
                "per-page": "25"})
            for g in ga.get("group_by", []):
                k = g.get("key_display_name") or "?"
                authors[k] = authors.get(k, 0) + int(g.get("count", 0))
        except Exception:
            errors += 1
    return insts, authors, len(top), errors


def _store(name):
    return IMPACT_DIR / f"{name}.json"


def load_cached(name, default):
    p = _store(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_cached(name, data):
    IMPACT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _store(name).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(_store(name))


TRACK_SYSTEM = """\
You write track-record text for a researcher's grant application or
job application, from verified bibliometric data. You get their author
record, per-year citation counts, and top papers. Write:

1. A 4-6 sentence "track record" paragraph in the first person,
   suitable for pasting into an application - factual, confident, no
   hype adjectives.
2. A 3-bullet "at a glance" list (publications, citations, h-index,
   trajectory).

Use ONLY the numbers provided - never round up, never estimate, never
add achievements that are not in the data. Note the data source as
OpenAlex where numbers are cited."""


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Impact Tracker - by GrapheAI",
                   page_icon="📈", layout="wide")

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
.stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #242D38; }
.stTabs [data-baseweb="tab"] { font-size: 1.05rem; font-weight: 500;
    padding: 12px 18px; color: #8E99A8; }
.stTabs [aria-selected="true"] { color: #FF8A5C; font-weight: 600; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #FF6B3D;
                                         height: 3px; }
section[data-testid="stSidebar"] { background: #171D25;
    border-right: 1px solid #242D38; }
section[data-testid="stSidebar"] h1 { font-family: 'IBM Plex Serif', serif;
    font-size: 1.4rem; color: #F4F6F9; }
h2 { font-family: 'IBM Plex Serif', Georgia, serif; font-weight: 600;
     color: #DFE5EC; }
h3 { color: #C3CBD6; }
details { border: 1px solid #263140; border-radius: 12px;
          background: #1A212B; }
[data-testid="stDataFrame"] { border: 1px solid #263140;
                              border-radius: 12px; }
[data-testid="stMetric"] { background: #1A212B; border: 1px solid #263140;
    border-radius: 12px; padding: .7rem .95rem; }
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace;
                                color: #FF8A5C; }
[data-testid="stMetricLabel"] { color: #8E99A8; text-transform: uppercase;
    letter-spacing: 1px; font-size: .78rem; }
.stButton>button, .stDownloadButton>button {
    font-size: 1.0rem; font-weight: 600; border-radius: 10px;
    padding: 0.5rem 1.25rem; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='an-header'>"
    "<p class='an-title'>📈 Impact Tracker</p>"
    "<p class='an-sub'>citations & track record · by <b>GrapheAI</b> · "
    "developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("📈 Impact Tracker")
    st.caption("by GrapheAI · Dr. Anurag Krishna")
    backend_label = st.radio(
        "Claude access", ["API key (pay per use)",
                          "Claude Max subscription (needs Claude Code)"])
    st.session_state["backend"] = ("max" if "Max" in backend_label
                                   else "api")
    if st.session_state["backend"] == "api":
        api_key = st.text_input("Anthropic API key", type="password")
    else:
        api_key = "claude-max-subscription"
        _cc = claude_code_status()
        if not _cc:
            st.warning("Claude Code not found for Max mode - run "
                       f"`{_max_update_command()}` and log in with `claude`.")
        elif _ver_tuple(_cc["version"]) < _ver_tuple(MAX_CLI_MIN_VERSION):
            st.warning(f"Claude Code {_cc['version']} ({_cc['origin']}) is older "
                       f"than {MAX_CLI_MIN_VERSION}, which Fable 5.1 needs. Run in "
                       f"Terminal, then restart: `{_max_update_command()}`. Until "
                       "then choose Haiku 4.5 or the API-key backend.")
        else:
            st.caption(f"Claude Code {_cc['version']} ({_cc['origin']})")
    st.session_state["effort"] = st.select_slider(
        "Reasoning effort", options=list(EFFORT_LEVELS),
        value=st.session_state.get("effort", "xhigh"),
        help="Thinking depth for Fable 5.1 / Opus 5 / Sonnet 5 on the API "
             "backend. xhigh = professor-grade default; max = when "
             "correctness matters more than time; low = quick lookups.")
    st.markdown("---")
    author = load_cached("author", None)
    if author:
        st.success(f"Tracking: {author.get('display_name', '?')}")
        fetched = load_cached("meta", {}).get("fetched")
        if fetched:
            st.caption(f"Last refresh: {fetched[:16]}")
    else:
        st.info("No author linked yet - use ⚙️ Setup.")
    st.caption("Data: OpenAlex (open, no key). Claude is used only "
               "on the Track record tab.")
    u = st.session_state.get("usage")
    if u:
        st.caption(f"Session: {u['calls']} calls - "
                   f"{u['in']:,}/{u['out']:,} tokens")

try:
    import pandas as pd
except Exception:
    st.error("pandas is required.")
    st.stop()
try:
    import altair as alt
except Exception:
    alt = None

tab_over, tab_papers, tab_cite, tab_track, tab_setup = st.tabs(
    ["📊 Overview", "📄 Papers", "🌍 Who cites you", "✍️ Track record",
     "⚙️ Setup"])

author = load_cached("author", None)
works = load_cached("works", [])

# ------------------------------ SETUP -------------------------------------
with tab_setup:
    st.markdown("**1 · Find yourself on OpenAlex** (once).")
    q = st.text_input("Author name", value="Anurag Krishna",
                      key="su_name")
    if st.button("🔎 Search OpenAlex", key="su_go"):
        try:
            st.session_state["su_hits"] = oa_search_authors(q.strip())
        except Exception as e:
            st.error(f"OpenAlex error: {e}")
    hits = st.session_state.get("su_hits")
    if hits is not None:
        if not hits:
            st.warning("No authors found - try adding an initial or "
                       "an alternative spelling.")
        for i, h in enumerate(hits):
            cols = st.columns([4, 1])
            with cols[0]:
                st.markdown(
                    f"**{h['name']}** — {h['institution'] or '?'} · "
                    f"{h['works']} works · {h['citations']} citations"
                    + (f" · h={h['h_index']}" if h['h_index'] else "")
                    + (f" · [ORCID]({h['orcid']})" if h['orcid']
                       else ""))
            with cols[1]:
                if st.button("This is me", key=f"su_pick{i}"):
                    try:
                        full = oa_get_author(h["id"])
                        save_cached("author", full)
                        st.session_state.pop("su_hits", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"OpenAlex error: {e}")
    st.markdown("---")
    st.markdown("**2 · Refresh the data** (any time - ~15 small "
                "requests; publication data lags reality by days to "
                "weeks).")
    if author is None:
        st.caption("Link an author first.")
    elif st.button("🔄 Refresh works + citers from OpenAlex",
                   type="primary", key="su_refresh"):
        try:
            with st.spinner("Author record..."):
                full = oa_get_author(author["id"])
                save_cached("author", full)
            with st.spinner("All works..."):
                w = oa_get_works(author["id"])
                save_cached("works", w)
            with st.spinner("Citing institutions & authors "
                            "(top papers)..."):
                insts, cauthors, n_top, errs = oa_who_cites(w)
                save_cached("citers", {"institutions": insts,
                                       "authors": cauthors,
                                       "n_top": n_top,
                                       "errors": errs})
            save_cached("meta", {"fetched":
                                 datetime.datetime.now().isoformat()})
            st.success(f"Refreshed: {len(w)} works.")
            st.rerun()
        except Exception as e:
            st.error(f"Refresh failed: {e}")

# ------------------------------ OVERVIEW ----------------------------------
with tab_over:
    if author is None or not works:
        st.info("Set up and refresh in ⚙️ Setup first.")
    else:
        stats = author.get("summary_stats") or {}
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Publications", author.get("works_count", len(works)))
        m2.metric("Citations", f"{author.get('cited_by_count', 0):,}")
        m3.metric("h-index", stats.get("h_index", "—"))
        m4.metric("i10-index", stats.get("i10_index", "—"))
        cby = author.get("counts_by_year") or []
        if cby:
            dfy = pd.DataFrame(cby).sort_values("year")
            dfy = dfy[dfy["year"] >= dfy["year"].max() - 9]
            if alt is not None:
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**Citations per year**")
                    st.altair_chart(
                        alt.Chart(dfy).mark_bar(color="#FF6B3D")
                        .encode(x=alt.X("year:O", title=None),
                                y=alt.Y("cited_by_count:Q",
                                        title="citations"),
                                tooltip=["year", "cited_by_count"]),
                        use_container_width=True)
                with c2:
                    st.markdown("**New works per year**")
                    st.altair_chart(
                        alt.Chart(dfy).mark_bar(color="#FF8A5C")
                        .encode(x=alt.X("year:O", title=None),
                                y=alt.Y("works_count:Q",
                                        title="works"),
                                tooltip=["year", "works_count"]),
                        use_container_width=True)
        top5 = sorted(works, key=lambda w: -w["citations"])[:5]
        st.markdown("**Most cited papers**")
        for w in top5:
            doi = (f" · [doi](https://doi.org/{w['doi']})"
                   if w["doi"] else "")
            st.markdown(f"- **{w['citations']}** — "
                        f"{w['title'][:100]} ({w['year']}, "
                        f"{w['venue'] or '?'}){doi}")

# ------------------------------ PAPERS ------------------------------------
with tab_papers:
    if not works:
        st.info("Refresh in ⚙️ Setup first.")
    else:
        dfw = pd.DataFrame([{
            "Year": w["year"], "Citations": w["citations"],
            "Title": w["title"], "Venue": w["venue"],
            "Type": w["type"],
            "DOI": (f"https://doi.org/{w['doi']}" if w["doi"]
                    else "")} for w in works])
        f1, f2 = st.columns(2)
        with f1:
            sort_by = st.selectbox("Sort by",
                                   ["Citations", "Year"], key="pp_sort")
        with f2:
            only_articles = st.checkbox("Articles only", value=False,
                                        key="pp_art")
        view = dfw.copy()
        if only_articles:
            view = view[view["Type"] == "article"]
        view = view.sort_values(sort_by, ascending=False)
        st.caption(f"{len(view)} works")
        st.dataframe(view, use_container_width=True, hide_index=True,
                     column_config={
                         "DOI": st.column_config.LinkColumn(),
                         "Title": st.column_config.TextColumn(
                             width="large")})
        st.download_button("⬇️ CSV", view.to_csv(index=False),
                           file_name="publications_openalex.csv",
                           key="pp_csv")

# ------------------------------ WHO CITES ---------------------------------
with tab_cite:
    citers = load_cached("citers", None)
    if not citers:
        st.info("Refresh in ⚙️ Setup first.")
    else:
        me = (author or {}).get("display_name", "").lower()
        st.caption(f"Aggregated over your {citers.get('n_top', 0)} "
                   "most-cited papers (citing works grouped by "
                   "institution and author on OpenAlex). Recurring "
                   "names are potential collaborators, competitors - "
                   "or customers.")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Citing institutions**")
            insts = sorted(citers.get("institutions", {}).items(),
                           key=lambda kv: -kv[1])[:20]
            dfi = pd.DataFrame(insts, columns=["Institution",
                                               "Citing works"])
            st.dataframe(dfi, use_container_width=True,
                         hide_index=True)
        with c2:
            st.markdown("**Citing authors**")
            auths = [(k, v) for k, v in
                     sorted(citers.get("authors", {}).items(),
                            key=lambda kv: -kv[1])
                     if k.lower() != me][:20]
            dfa = pd.DataFrame(auths, columns=["Author",
                                               "Citing works"])
            st.dataframe(dfa, use_container_width=True,
                         hide_index=True)
        if citers.get("errors"):
            st.caption(f"({citers['errors']} paper(s) could not be "
                       "aggregated - rerun the refresh if needed.)")

# ------------------------------ TRACK RECORD ------------------------------
with tab_track:
    if author is None or not works:
        st.info("Refresh in ⚙️ Setup first.")
    else:
        tr_model = st.selectbox("Model", list(MODELS), index=3,
                                key="tr_model")
        focus = st.text_input(
            "Optional focus", key="tr_focus",
            placeholder="e.g. for a stability-focused fellowship; "
                        "emphasise recent trajectory")
        if st.button("✍️ Write track-record text", type="primary",
                     key="tr_go"):
            stats = author.get("summary_stats") or {}
            top = sorted(works, key=lambda w: -w["citations"])[:10]
            top_txt = "\n".join(
                f"- {w['citations']} citations: {w['title'][:110]} "
                f"({w['year']}, {w['venue']})" for w in top)
            cby = sorted(author.get("counts_by_year") or [],
                         key=lambda x: x["year"])
            cby_txt = "\n".join(
                f"{x['year']}: {x['works_count']} works, "
                f"{x['cited_by_count']} citations" for x in cby)
            umsg = (f"AUTHOR: {author.get('display_name')}\n"
                    f"TOTALS: {author.get('works_count')} works, "
                    f"{author.get('cited_by_count')} citations, "
                    f"h-index {stats.get('h_index')}, "
                    f"i10 {stats.get('i10_index')}\n"
                    f"PER YEAR:\n{cby_txt}\n\n"
                    f"TOP PAPERS:\n{top_txt}\n"
                    + (f"\nFOCUS: {focus}" if focus.strip() else "")
                    + f"\nDATE: {datetime.date.today().isoformat()}")
            try:
                with st.spinner("Writing..."):
                    txt = call_claude(api_key, TRACK_SYSTEM, umsg,
                                      MODELS[tr_model],
                                      max_tokens=1500)
                st.session_state["tr_out"] = txt
            except Exception as e:
                st.error(f"Claude error: {e}")
        txt = st.session_state.get("tr_out")
        if txt:
            st.markdown("---")
            st.markdown(txt)
            st.download_button("⬇️ Markdown", txt,
                               file_name="track_record.md",
                               key="tr_dl")
            st.caption("Check the numbers once against your Google "
                       "Scholar / Scopus profile - OpenAlex counts "
                       "can differ slightly from other databases.")
