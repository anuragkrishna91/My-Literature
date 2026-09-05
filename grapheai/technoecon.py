"""
TechnoEcon - by GrapheAI. Techno-economic modelling for PV product
lines: EUR/Wp, LCOE, sensitivity tornado, and an efficiency-vs-
degradation parity map against the c-Si baseline.

Run with:
    streamlit run technoecon.py --server.port 8508

Model (documented, deliberately transparent):
  P_STC per m2       = 1000 W/m2 x efficiency
  module EUR/Wp      = module cost [EUR/m2] / P_STC [W/m2]
  CAPEX  EUR/Wp      = module EUR/Wp + BOS power [EUR/Wp]
                       + BOS area [EUR/m2] / P_STC
  Energy in year t   = irradiance [kWh/m2/yr] x PR
                       x (1 - degradation)^(t-1)   per kWp
  LCOE [EUR/kWh]     = (CAPEX*1000 + sum_t OM/(1+r)^t)
                       / sum_t E_t/(1+r)^t          per kWp

Shares with the rest of GrapheAI:
  - answers/technoecon/         saved scenario sets
  - answers/figures_out/        publication figures
  - answers/spend.json          monthly API spend + budget
  - the Claude backend          (only for the optional report narrative)
"""

import datetime
import json
import re
from pathlib import Path

import streamlit as st

ANSWERS_DIR = Path("answers")
TE_DIR = ANSWERS_DIR / "technoecon"
SCEN_FILE = TE_DIR / "scenarios.json"

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
# Word export
# --------------------------------------------------------------------------
def _md_runs(par, text):
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`", text):
        if m.start() > pos:
            par.add_run(text[pos:m.start()])
        if m.group(1) is not None:
            par.add_run(m.group(1)).bold = True
        elif m.group(2) is not None:
            par.add_run(m.group(2)).italic = True
        else:
            r = par.add_run(m.group(3))
            r.font.name = "Consolas"
        pos = m.end()
    if pos < len(text):
        par.add_run(text[pos:])


def md_to_docx(doc, md):
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            h = doc.add_heading("", level=min(len(m.group(1)), 4))
            _md_runs(h, m.group(2))
            continue
        m = re.match(r"^[-*+]\s+(.*)", s)
        if m:
            _md_runs(doc.add_paragraph(style="List Bullet"), m.group(1))
            continue
        _md_runs(doc.add_paragraph(), s)


def save_pub_fig(fig, name):
    out = ANSWERS_DIR / "figures_out"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60]
    png = out / f"{safe}_{stamp}.png"
    fig.savefig(png, dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(out / f"{safe}_{stamp}.svg", bbox_inches="tight",
                facecolor="white")
    return png


# --------------------------------------------------------------------------
# The economics engine
# --------------------------------------------------------------------------
PARAM_COLS = {
    "Efficiency (%)": "eff",
    "Module cost (EUR/m2)": "mod_cost",
    "BOS area (EUR/m2)": "bos_area",
    "BOS power (EUR/Wp)": "bos_power",
    "O&M (EUR/kWp/yr)": "om",
    "Degradation (%/yr)": "deg",
    "Lifetime (yr)": "life",
    "Discount rate (%)": "rate",
    "Irradiance (kWh/m2/yr)": "irr",
    "Performance ratio": "pr",
}

DEFAULT_SCENARIOS = [
    {"name": "c-Si baseline", "eff": 21.5, "mod_cost": 45.0,
     "bos_area": 55.0, "bos_power": 0.25, "om": 15.0, "deg": 0.4,
     "life": 30, "rate": 5.0, "irr": 1700.0, "pr": 0.85},
    {"name": "Perovskite SJ", "eff": 18.0, "mod_cost": 30.0,
     "bos_area": 55.0, "bos_power": 0.25, "om": 15.0, "deg": 1.5,
     "life": 20, "rate": 5.0, "irr": 1700.0, "pr": 0.83},
    {"name": "4T pero/Si tandem", "eff": 28.0, "mod_cost": 85.0,
     "bos_area": 55.0, "bos_power": 0.25, "om": 17.0, "deg": 0.8,
     "life": 25, "rate": 5.0, "irr": 1700.0, "pr": 0.84},
    {"name": "2T pero/Si tandem", "eff": 26.5, "mod_cost": 65.0,
     "bos_area": 55.0, "bos_power": 0.25, "om": 15.0, "deg": 1.0,
     "life": 25, "rate": 5.0, "irr": 1700.0, "pr": 0.84},
]


def evaluate(s):
    """One scenario dict -> results dict. Pure function, unit-tested."""
    eff = float(s["eff"]) / 100.0
    p_stc = 1000.0 * eff                       # W per m2
    mod_wp = float(s["mod_cost"]) / p_stc      # EUR per Wp
    capex_wp = (mod_wp + float(s["bos_power"])
                + float(s["bos_area"]) / p_stc)
    r = float(s["rate"]) / 100.0
    d = float(s["deg"]) / 100.0
    n = int(s["life"])
    e1 = float(s["irr"]) * float(s["pr"])      # kWh/kWp in year 1
    e_disc = e_tot = om_disc = 0.0
    for t in range(1, n + 1):
        e_t = e1 * (1.0 - d) ** (t - 1)
        e_tot += e_t
        e_disc += e_t / (1.0 + r) ** t
        om_disc += float(s["om"]) / (1.0 + r) ** t
    lcoe = (capex_wp * 1000.0 + om_disc) / e_disc if e_disc > 0 else None
    return {
        "Module EUR/Wp": round(mod_wp, 3),
        "CAPEX EUR/Wp": round(capex_wp, 3),
        "Year-1 yield (kWh/kWp)": round(e1, 0),
        "Lifetime energy (MWh/kWp)": round(e_tot / 1000.0, 2),
        "LCOE (EURc/kWh)": round(lcoe * 100.0, 2)
        if lcoe is not None else None,
    }


def lcoe_only(s):
    v = evaluate(s)["LCOE (EURc/kWh)"]
    return v if v is not None else float("nan")


def load_scenarios():
    if SCEN_FILE.exists():
        try:
            d = json.loads(SCEN_FILE.read_text(encoding="utf-8"))
            if d:
                return d
        except Exception:
            pass
    return [dict(s) for s in DEFAULT_SCENARIOS]


def save_scenarios(scen):
    try:
        TE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SCEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(scen, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(SCEN_FILE)
    except Exception:
        pass


TE_SYSTEM = """\
You write the narrative of a techno-economic briefing for investors and
technical due-diligence readers, from the model inputs and computed
results given. 3 short sections in markdown: ## Key findings (which
product line wins on LCOE and why, quantified), ## Sensitivities (which
levers matter most, from the tornado data), ## Caveats (the model's own
simplifications: single-year build, constant O&M, no financing
structure, input assumptions dominate). Use ONLY the numbers provided.
Sober tone; no hype; no invented market claims."""


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="TechnoEcon - by GrapheAI",
                   page_icon="📉", layout="wide")

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
    "<p class='an-title'>📉 TechnoEcon</p>"
    "<p class='an-sub'>LCOE & cost modelling · by <b>GrapheAI</b> · "
    "developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("📉 TechnoEcon")
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
    st.caption("All economics run locally - Claude is only used for "
               "the optional report narrative.")
    u = st.session_state.get("usage")
    if u:
        st.caption(f"Session: {u['calls']} calls - "
                   f"{u['in']:,}/{u['out']:,} tokens")

try:
    import numpy as np
    import pandas as pd
except Exception:
    st.error("numpy and pandas are required.")
    st.stop()
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None

with st.expander("❓ Model assumptions (read once)"):
    st.markdown(
        "- **CAPEX** = module €/Wp (from €/m² and efficiency) + "
        "area-BOS spread over the module power + power-BOS.\n"
        "- **Energy** in year *t* = irradiance × PR × (1−d)^(t−1) per "
        "kWp; **LCOE** = discounted costs / discounted energy.\n"
        "- Single-year build, constant O&M, no financing structure, no "
        "inverter replacement - a *comparison* model: it ranks product "
        "lines under identical assumptions rather than predicting "
        "absolute tariffs.\n"
        "- Degradation rates can come straight from your **PeroDeg** "
        "PLR fits - that is the bankability story: measured stability "
        "→ modelled LCOE.")

tab_scen, tab_sens, tab_map, tab_report = st.tabs(
    ["💰 Scenarios", "🌪️ Sensitivity", "🗺️ Parity map", "📄 Report"])

scen = load_scenarios()

# ------------------------------ SCENARIOS ---------------------------------
with tab_scen:
    st.markdown("**Edit any cell, add rows for new product lines** - "
                "then Save & compute.")
    df_in = pd.DataFrame(scen)
    df_in = df_in.rename(columns={v: k for k, v in PARAM_COLS.items()})
    cols = ["name"] + list(PARAM_COLS.keys())
    df_in = df_in[[c for c in cols if c in df_in.columns]]
    edited = st.data_editor(df_in, num_rows="dynamic",
                            use_container_width=True, key="scen_ed",
                            hide_index=True)
    if st.button("💾 Save & compute", type="primary", key="scen_save"):
        new = []
        for _, r in edited.iterrows():
            if not str(r.get("name") or "").strip():
                continue
            try:
                s = {"name": str(r["name"]).strip()}
                for label, key in PARAM_COLS.items():
                    s[key] = float(r[label])
                evaluate(s)          # validates
                new.append(s)
            except Exception as e:
                st.warning(f"Row '{r.get('name')}' skipped: {e}")
        if new:
            save_scenarios(new)
            st.success(f"Saved {len(new)} scenario(s).")
            scen = new
        else:
            st.warning("No valid rows to save.")

    if scen:
        res_rows = []
        for s in scen:
            try:
                out = {"Scenario": s["name"]}
                out.update(evaluate(s))
                res_rows.append(out)
            except Exception:
                pass
        dfr = pd.DataFrame(res_rows)
        st.markdown("### Results")
        st.dataframe(dfr, use_container_width=True, hide_index=True)
        base = next((s for s in scen
                     if "c-si" in s["name"].lower()), None)
        if plt is not None and len(dfr):
            fig, ax = plt.subplots(figsize=(7.2, 3.6))
            vals = dfr["LCOE (EURc/kWh)"].astype(float)
            bars = ax.bar(dfr["Scenario"], vals, color="#FF6B3D",
                          width=0.55)
            if base is not None:
                ax.axhline(lcoe_only(base), color="#3A4656", ls="--",
                           lw=1.2, label=f"{base['name']} parity")
                ax.legend(frameon=False, fontsize=9)
            for b, v in zip(bars, vals):
                ax.annotate(f"{v:.1f}", (b.get_x() + b.get_width() / 2,
                                         v), ha="center", va="bottom",
                            fontsize=9)
            ax.set_ylabel("LCOE (€c/kWh)")
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(axis="x", labelrotation=12)
            fig.tight_layout()
            st.pyplot(fig)
            if st.button("📤 Export as publication figure",
                         key="scen_fig"):
                p = save_pub_fig(fig, "LCOE_by_product")
                st.success(f"Saved 300-dpi PNG + SVG: {p}")
            plt.close(fig)
        st.download_button(
            "⬇️ Results CSV", dfr.to_csv(index=False),
            file_name="technoecon_results.csv", key="scen_csv")

# ------------------------------ SENSITIVITY -------------------------------
with tab_sens:
    if not scen:
        st.info("Define scenarios first.")
    else:
        s1, s2 = st.columns(2)
        with s1:
            pick = st.selectbox("Scenario", [s["name"] for s in scen],
                                key="sens_pick")
        with s2:
            swing = st.slider("Parameter swing (±%)", 5, 50, 20,
                              key="sens_swing")
        s0 = next(s for s in scen if s["name"] == pick)
        base_lcoe = lcoe_only(s0)
        st.metric("Base LCOE", f"{base_lcoe:.2f} €c/kWh")
        sweep = [k for k in PARAM_COLS.values() if k != "life"]
        rows = []
        for key in sweep:
            lo, hi = dict(s0), dict(s0)
            lo[key] = s0[key] * (1 - swing / 100.0)
            hi[key] = s0[key] * (1 + swing / 100.0)
            if key == "pr":
                lo[key] = max(min(lo[key], 1.0), 0.01)
                hi[key] = max(min(hi[key], 1.0), 0.01)
            try:
                l_lo, l_hi = lcoe_only(lo), lcoe_only(hi)
            except Exception:
                continue
            label = next(lbl for lbl, k in PARAM_COLS.items()
                         if k == key)
            rows.append({"param": label,
                         "low": l_lo - base_lcoe,
                         "high": l_hi - base_lcoe,
                         "span": abs(l_hi - l_lo)})
        rows.sort(key=lambda r: r["span"])
        if plt is not None and rows:
            fig, ax = plt.subplots(figsize=(7.0, 0.5 * len(rows) + 1.2))
            ypos = np.arange(len(rows))
            for i, r in enumerate(rows):
                a, b = sorted((r["low"], r["high"]))
                ax.barh(i, b - a, left=a, height=0.55,
                        color="#FF6B3D" if r is rows[-1] else "#FFA987")
            ax.axvline(0, color="#3A4656", lw=1)
            ax.set_yticks(ypos,
                          [f"{r['param']}" for r in rows], fontsize=9)
            ax.set_xlabel(f"Δ LCOE (€c/kWh) at ±{swing}% "
                          f"— {pick}")
            ax.spines[["top", "right"]].set_visible(False)
            fig.tight_layout()
            st.pyplot(fig)
            if st.button("📤 Export as publication figure",
                         key="sens_fig"):
                p = save_pub_fig(fig, f"Tornado_{pick}")
                st.success(f"Saved: {p}")
            plt.close(fig)
        st.caption("Bars show how far LCOE moves when one input swings "
                   "±{}% with everything else fixed. The longest bar "
                   "is your most powerful lever.".format(swing))

# ------------------------------ PARITY MAP --------------------------------
with tab_map:
    if not scen:
        st.info("Define scenarios first.")
    else:
        base = next((s for s in scen if "c-si" in s["name"].lower()),
                    scen[0])
        others = [s for s in scen if s is not base]
        pick = st.selectbox(
            "Product line to map (its costs & lifetime are held fixed)",
            [s["name"] for s in others] or [base["name"]],
            key="map_pick")
        s0 = next(s for s in scen if s["name"] == pick)
        base_lcoe = lcoe_only(base)
        c1, c2 = st.columns(2)
        with c1:
            eff_rng = st.slider("Efficiency range (%)", 10, 40,
                                (15, 32), key="map_eff")
        with c2:
            deg_rng = st.slider("Degradation range (%/yr)", 0.0, 5.0,
                                (0.2, 3.0), step=0.1, key="map_deg")
        effs = np.linspace(eff_rng[0], eff_rng[1], 41)
        degs = np.linspace(deg_rng[0], deg_rng[1], 41)
        Z = np.zeros((len(degs), len(effs)))
        for i, dg in enumerate(degs):
            for j, ef in enumerate(effs):
                s = dict(s0)
                s["eff"], s["deg"] = float(ef), float(dg)
                Z[i, j] = lcoe_only(s)
        if plt is not None:
            fig, ax = plt.subplots(figsize=(7.2, 5.2))
            cf = ax.contourf(effs, degs, Z, levels=18, cmap="RdYlGn_r")
            fig.colorbar(cf, ax=ax, label="LCOE (€c/kWh)")
            cs = ax.contour(effs, degs, Z, levels=[base_lcoe],
                            colors="#12161C", linewidths=2.2)
            ax.clabel(cs, fmt={base_lcoe:
                               f"{base['name']} parity"}, fontsize=9)
            for s in scen:
                mark = "s" if s is base else "o"
                ax.plot(s["eff"], s["deg"], mark, ms=8,
                        mfc="#FFD166" if s["name"] == pick else "white",
                        mec="#12161C")
                ax.annotate(s["name"], (s["eff"], s["deg"]),
                            textcoords="offset points", xytext=(7, 5),
                            fontsize=8)
            ax.set_xlabel("Module efficiency (%)")
            ax.set_ylabel("Degradation (%/yr)")
            ax.set_title(f"Where {pick} beats {base['name']} "
                         "(below/right of the parity line)",
                         fontsize=11)
            fig.tight_layout()
            st.pyplot(fig)
            if st.button("📤 Export as publication figure",
                         key="map_fig"):
                p = save_pub_fig(fig, f"Parity_map_{pick}")
                st.success(f"Saved: {p}")
            plt.close(fig)
        st.caption("Every point uses the mapped line's own costs, "
                   "lifetime and O&M - only efficiency and degradation "
                   "vary. The black contour is where its LCOE equals "
                   "the baseline's.")

# ------------------------------ REPORT ------------------------------------
with tab_report:
    st.markdown("One Word file: assumptions, results table, the three "
                "figures, and (optionally) a Claude-written narrative.")
    r1, r2 = st.columns(2)
    with r1:
        want_ai = st.checkbox("Include Claude narrative", value=True,
                              key="rep_ai")
    with r2:
        rep_model = st.selectbox("Model", list(MODELS), index=3,
                                 key="rep_model", disabled=not want_ai)
    if st.button("📄 Build report", type="primary", key="rep_go"):
        try:
            from docx import Document
            from docx.shared import Inches as DocxInches
        except ImportError:
            st.error("python-docx is not installed.")
            st.stop()
        if not scen:
            st.warning("Define scenarios first.")
            st.stop()
        res_rows = []
        for s in scen:
            out = {"Scenario": s["name"]}
            out.update(evaluate(s))
            res_rows.append(out)
        dfr = pd.DataFrame(res_rows)
        doc = Document()
        doc.add_heading("Techno-economic briefing", level=0)
        doc.add_paragraph(
            f"GrapheAI TechnoEcon · Dr. Anurag Krishna · "
            f"{datetime.date.today().isoformat()}")
        doc.add_heading("Assumptions", level=1)
        t = doc.add_table(rows=1, cols=len(PARAM_COLS) + 1)
        t.style = "Light Grid Accent 1"
        hdr = t.rows[0].cells
        hdr[0].text = "Scenario"
        for i, lbl in enumerate(PARAM_COLS, 1):
            hdr[i].text = lbl
        for s in scen:
            row = t.add_row().cells
            row[0].text = s["name"]
            for i, key in enumerate(PARAM_COLS.values(), 1):
                row[i].text = str(s[key])
        doc.add_heading("Results", level=1)
        t = doc.add_table(rows=1, cols=len(dfr.columns))
        t.style = "Light Grid Accent 1"
        for i, c in enumerate(dfr.columns):
            t.rows[0].cells[i].text = str(c)
        for _, r in dfr.iterrows():
            cells = t.add_row().cells
            for i, c in enumerate(dfr.columns):
                cells[i].text = str(r[c])
        # figures
        if plt is not None:
            import tempfile
            base = next((s for s in scen
                         if "c-si" in s["name"].lower()), scen[0])
            figs = []
            fig, ax = plt.subplots(figsize=(7.2, 3.6))
            ax.bar(dfr["Scenario"],
                   dfr["LCOE (EURc/kWh)"].astype(float),
                   color="#FF6B3D", width=0.55)
            ax.axhline(lcoe_only(base), color="#3A4656", ls="--", lw=1.2)
            ax.set_ylabel("LCOE (€c/kWh)")
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(axis="x", labelrotation=12)
            fig.tight_layout()
            figs.append(("LCOE by product line", fig))
            doc.add_heading("Figures", level=1)
            for title, f in figs:
                tmp = tempfile.NamedTemporaryFile(suffix=".png",
                                                  delete=False)
                f.savefig(tmp.name, dpi=200, bbox_inches="tight",
                          facecolor="white")
                doc.add_paragraph(title).runs[0].bold = True
                doc.add_picture(tmp.name, width=DocxInches(6.2))
                plt.close(f)
        if want_ai:
            try:
                umsg = ("INPUTS:\n" + json.dumps(scen, indent=1)
                        + "\n\nRESULTS:\n" + dfr.to_csv(index=False))
                with st.spinner("Writing the narrative..."):
                    narr = call_claude(api_key, TE_SYSTEM, umsg,
                                       MODELS[rep_model],
                                       max_tokens=2000)
                doc.add_heading("Narrative", level=1)
                md_to_docx(doc, narr)
            except Exception as e:
                st.warning(f"Narrative skipped: {e}")
        ANSWERS_DIR.mkdir(exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = ANSWERS_DIR / f"TechnoEcon_report_{stamp}.docx"
        doc.save(path)
        st.success(f"Report saved: {path}")
