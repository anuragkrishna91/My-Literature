"""
Analytics - by GrapheAI. Schema-driven data extraction from papers
and proposals, with a plot studio.

Classifies the materials inside every paper of the corpus - absorber
compositions, passivation agents, transport layers, electrodes, additives -
together with their reported properties and device impact, into a
structured, queryable library.

Run with:
    streamlit run analytics.py --server.port 8505

Shares with GrapheAI / PV Radar:
  - chroma_db/                 read-only source of paper full text
  - answers/analytics/         extracted datasets (grow incrementally)
  - answers/spend.json         monthly API spend + budget
  - the Claude backend         API key or Claude Max via Claude Code

It reads text only (no semantic search), so it opens WITHOUT loading the
embedding model - startup is seconds.
"""

import datetime
import re
from pathlib import Path

import sys
import streamlit as st

ANSWERS_DIR = Path("answers")

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
    import json
    if SPEND_FILE.exists():
        try:
            return json.loads(SPEND_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_spend(data):
    import json
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


# ---------------------------------------------------------- OpenAI backend
# Third backend next to the Anthropic API and Claude Max: any model in the
# user's OpenAI account (listed live from the account, or typed by exact
# id). Uses the official `openai` SDK: Responses API first, Chat
# Completions as fallback. The sidebar's reasoning-effort slider maps to
# the OpenAI reasoning effort (xhigh / max -> high). Every call is tracked
# like the others; cost is counted only if you enter the model's prices.
OPENAI_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                 "xhigh": "high", "max": "high"}


def _openai_effort():
    return OPENAI_EFFORT.get(st.session_state.get("effort", "high"), "high")


def _openai_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI mode needs the SDK - in Terminal: "
                           f"{sys.executable} -m pip install -U openai") from exc
    key = (st.session_state.get("openai_key") or "").strip()
    if not key:
        raise RuntimeError("Enter the OpenAI API key in the sidebar.")
    return OpenAI(api_key=key, timeout=900, max_retries=2)


def openai_models(key):
    """Model ids available to this account (cached per session)."""
    cache = st.session_state.setdefault("_openai_models", {})
    k = key[-8:]
    if k in cache:
        return cache[k]
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=30)
    ids = sorted(m.id for m in client.models.list())
    cache[k] = ids
    return ids


def _openai_model():
    m = (st.session_state.get("openai_model") or "").strip()
    if not m:
        raise RuntimeError("Choose an OpenAI model id in the sidebar.")
    return m


def _openai_usage(resp):
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    n_in = getattr(u, "input_tokens", None)
    if n_in is None:
        n_in = getattr(u, "prompt_tokens", 0)
    n_out = getattr(u, "output_tokens", None)
    if n_out is None:
        n_out = getattr(u, "completion_tokens", 0)
    return int(n_in or 0), int(n_out or 0)


def _openai_unsupported(err):
    m = str(err).lower()
    return any(k in m for k in ("unsupported", "not supported", "unknown parameter",
                                "unrecognized", "invalid_request", "does not support",
                                "unexpected keyword", "not a valid"))


def call_openai(system, user_msg, max_tokens=None):
    """One text call. Responses API (with reasoning effort when enabled),
    then the same without reasoning, then Chat Completions."""
    client = _openai_client()
    model = _openai_model()
    _default_mt = getattr(globals().get("config"), "MAX_ANSWER_TOKENS", 4000) or 4000
    mt = int(max_tokens or _default_mt)
    if st.session_state.get("openai_reasoning", True):
        mt = max(mt, 16000)          # reasoning tokens share the budget
    attempts = []
    if st.session_state.get("openai_reasoning", True):
        attempts.append(("responses+reasoning",
                         dict(model=model, instructions=system, input=user_msg,
                              max_output_tokens=mt, reasoning={"effort": _openai_effort()})))
    attempts.append(("responses", dict(model=model, instructions=system, input=user_msg,
                                       max_output_tokens=mt)))
    last = None
    for name, kw in attempts:
        try:
            resp = client.responses.create(**kw)
            text = (getattr(resp, "output_text", "") or "").strip()
            n_in, n_out = _openai_usage(resp)
            _track_usage(n_in, n_out, model)
            if not text:
                raise RuntimeError("OpenAI returned no text (check max tokens / refusal)")
            return text
        except Exception as e:
            last = e
            if not _openai_unsupported(e) and "reasoning" not in name:
                break
    try:
        kw = dict(model=model, messages=[{"role": "system", "content": system},
                                         {"role": "user", "content": user_msg}],
                  max_completion_tokens=mt)
        if st.session_state.get("openai_reasoning", True):
            kw["reasoning_effort"] = _openai_effort()
        try:
            resp = client.chat.completions.create(**kw)
        except Exception as e:
            if "reasoning_effort" in kw and _openai_unsupported(e):
                kw.pop("reasoning_effort")
                resp = client.chat.completions.create(**kw)
            else:
                raise
        text = (resp.choices[0].message.content or "").strip()
        n_in, n_out = _openai_usage(resp)
        _track_usage(n_in, n_out, model)
        if not text:
            raise RuntimeError("OpenAI returned no text")
        return text
    except Exception as e2:
        raise RuntimeError(f"OpenAI error: {last or e2}") from e2


def call_openai_vision(system, text, png_path, max_tokens=3000):
    """Image + text call (figure critic) on the OpenAI backend."""
    import base64
    client = _openai_client()
    model = _openai_model()
    data_url = ("data:image/png;base64,"
                + base64.standard_b64encode(Path(png_path).read_bytes()).decode("ascii"))
    try:
        resp = client.responses.create(
            model=model, instructions=system,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "image_url": data_url}]}],
            max_output_tokens=int(max_tokens))
        out = (getattr(resp, "output_text", "") or "").strip()
    except Exception:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": [
                          {"type": "text", "text": text},
                          {"type": "image_url", "image_url": {"url": data_url}}]}],
            max_completion_tokens=int(max_tokens))
        out = (resp.choices[0].message.content or "").strip()
    n_in, n_out = _openai_usage(resp)
    _track_usage(n_in, n_out, model)
    if not out:
        raise RuntimeError("OpenAI returned no text for the figure review")
    return out


def render_openai_sidebar():
    """Sidebar controls for the OpenAI backend; returns the api_key
    sentinel ('' until a key is entered so buttons stay disabled)."""
    key = st.text_input("OpenAI API key", type="password", key="openai_key_in",
                        help="From platform.openai.com. Sent only to api.openai.com.")
    st.session_state["openai_key"] = key
    models = []
    if key.strip():
        try:
            models = openai_models(key.strip())
        except Exception as e:
            st.caption(f"Could not list the account's models: {str(e)[:90]}")
    typed = st.text_input("Model id", value=st.session_state.get("openai_model", ""),
                          key="openai_model_in",
                          help="Exact id as OpenAI names it. Pick from the list below "
                               "once the key is entered, or type it.")
    if models:
        pick = st.selectbox("...or pick from your account", ["(keep typed id)"] + models,
                            key="openai_model_pick")
        if pick != "(keep typed id)":
            typed = pick
    st.session_state["openai_model"] = typed.strip()
    st.session_state["openai_reasoning"] = st.checkbox(
        "Reasoning model (send effort; larger output budget)", value=True,
        key="openai_reason",
        help="Untick for non-reasoning models if the API rejects the reasoning "
             "parameter (the app also retries without it).")
    c1, c2 = st.columns(2)
    p_in = c1.number_input("$ / M input tokens", min_value=0.0, step=0.25,
                           value=float(st.session_state.get("openai_p_in", 0.0)),
                           key="openai_p_in_w")
    p_out = c2.number_input("$ / M output tokens", min_value=0.0, step=0.5,
                            value=float(st.session_state.get("openai_p_out", 0.0)),
                            key="openai_p_out_w")
    st.session_state["openai_p_in"], st.session_state["openai_p_out"] = p_in, p_out
    if typed.strip() and (p_in or p_out):
        PRICES[typed.strip()] = (p_in, p_out)
    st.caption("Requests (and the figure critic's images) go to api.openai.com. The "
               "reasoning-effort slider maps to OpenAI's low / medium / high. "
               "Cost is tracked only with the prices above.")
    return "openai-backend" if (key.strip() and typed.strip()) else ""


def call_claude(api_key, system, user_msg, model, max_tokens=None):
    """Single Claude call via the selected backend; tracks token usage.
    Backend 'api': the Anthropic API (Fable 5.1 with effort-controlled
    reasoning, streaming, refusal fallbacks). Backend 'max': the Claude
    Agent SDK, billed to the Claude Max plan."""
    if st.session_state.get("backend") == "openai":
        return call_openai(system, user_msg, max_tokens)
    if st.session_state.get("backend") == "max":
        return call_claude_max(system, user_msg, model, max_tokens)
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = _api_create(client, _api_kwargs(model, system, user_msg,
                                           max_tokens))
    _track_usage(resp.usage.input_tokens, resp.usage.output_tokens, model)
    return _response_text(resp)


# --------------------------------------------------------------------------
# Corpus access (text only - no embedding model needed)
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def load_collection():
    import chromadb
    import config
    client = chromadb.PersistentClient(path=str(config.DB_DIR))
    return client.get_collection(config.COLLECTION_NAME)


@st.cache_data(show_spinner=False)
def list_papers(total):
    """One row per indexed paper: sig, title, file, source."""
    col = load_collection()
    papers = {}
    offset, BATCH = 0, 5000
    while offset < total:
        got = col.get(include=["metadatas"], limit=BATCH, offset=offset)
        metas = got["metadatas"]
        if not metas:
            break
        for m in metas:
            sig = m.get("doc_sig")
            if sig and sig not in papers:
                papers[sig] = {"sig": sig, "title": m.get("title", "?"),
                               "file": m.get("file", ""),
                               "source": m.get("source", "")}
        offset += len(metas)
    return sorted(papers.values(), key=lambda r: r["file"])


def _chunk_score(t):
    """Data density of a text chunk: digits + PV metric mentions per
    character. Used to decide what survives truncation."""
    if not t:
        return 0.0
    digits = sum(ch.isdigit() for ch in t)
    units = len(re.findall(
        r"%|mA/?cm|\bm?V\b|\beV\b|\bFF\b|Jsc|Voc|PCE|efficien|"
        r"T80|ISOS|\btable\b", t, re.I))
    return (digits + 8 * units) / max(len(t), 1)


def paper_full_text(sig, max_chars=16000, smart=True):
    """Paper text for extraction. If it fits the budget, everything in
    page order. If not (smart=True): keep the opening chunk plus the
    most data-dense chunks (results tables, metric sections), still in
    page order - instead of blindly cutting off the end."""
    col = load_collection()
    got = col.get(where={"doc_sig": sig}, include=["documents", "metadatas"])
    pairs = [(d, m) for d, m in zip(got["documents"], got["metadatas"])
             if m.get("type") != "figure"]
    pairs.sort(key=lambda x: x[1].get("page_start", 0))
    texts = [d or "" for d, m in pairs]
    if not texts:
        return ""
    total = sum(len(t) + 2 for t in texts)
    if not smart or total <= max_chars:
        return "\n\n".join(texts)[:max_chars]
    keep = {0}
    budget = len(texts[0]) + 2
    order = sorted(range(1, len(texts)),
                   key=lambda i: _chunk_score(texts[i]), reverse=True)
    for i in order:
        c = len(texts[i]) + 2
        if budget + c > max_chars:
            continue
        keep.add(i)
        budget += c
    return "\n\n".join(texts[i] for i in sorted(keep))[:max_chars]


# --------------------------------------------------------------------------
# Schemas + datasets
# --------------------------------------------------------------------------
ANALYTICS_DIR = ANSWERS_DIR / "analytics"
SCHEMAS_FILE = ANSWERS_DIR / "analytics_schemas.json"

PRESET_SCHEMAS = {
    "PV device metrics": [
        ("PCE (%)", "best power conversion efficiency, number only"),
        ("Voc (V)", "open-circuit voltage"),
        ("Jsc (mA/cm2)", "short-circuit current density"),
        ("FF", "fill factor, 0-1 or %"),
        ("Cell type", "single junction / 2T tandem / 4T tandem / module"),
        ("Architecture", "n-i-p or p-i-n"),
        ("Bandgap (eV)", "absorber optical bandgap"),
        ("Area (cm2)", "active device area"),
        ("Deposition", "absorber deposition method"),
        ("Year", "publication year"),
    ],
    "Stack & innovation survey": [
        ("PCE (%)", "best power conversion efficiency reported, number "
                    "only"),
        ("Voc (V)", "open-circuit voltage of the best device"),
        ("FF", "fill factor of the best device, 0-1 or %"),
        ("Cell type", "single junction / 2T tandem / 4T tandem / module"),
        ("Architecture", "n-i-p or p-i-n"),
        ("Device stack", "full layer sequence, e.g. "
                         "glass/ITO/NiO/perovskite/C60/BCP/Ag"),
        ("Absorber", "perovskite composition, formula if given"),
        ("Key innovation", "the paper's central novelty in ONE short "
                           "sentence - what did they do differently"),
        ("Deposition", "absorber deposition method"),
        ("Stability result", "headline stability claim with conditions, "
                             "e.g. '95% after 1000h ISOS-L-1'"),
        ("Year", "publication year"),
    ],
    "Stability results": [
        ("Protocol", "aging test type, ISOS code if stated"),
        ("Duration (h)", "test duration in hours"),
        ("Retained (%)", "performance retained at end, % of initial"),
        ("T80 (h)", "time to 80% of initial, hours, if stated"),
        ("Temperature (C)", "aging temperature"),
        ("Humidity (%)", "relative humidity during test"),
        ("Encapsulated", "yes / no"),
        ("Cell type", "single junction / tandem / module"),
    ],
    "Proposal metadata": [
        ("Instrument", "funding instrument, e.g. HE RIA, EIC Pathfinder"),
        ("Call", "call or topic identifier"),
        ("Budget (EUR)", "requested budget in euros"),
        ("Duration (months)", "project duration"),
        ("Overall score", "evaluation score if this is an ESR"),
        ("Outcome", "funded / rejected / unknown"),
        ("Year", "submission year"),
    ],
}


def load_schemas():
    import json as _json
    if SCHEMAS_FILE.exists():
        try:
            return _json.loads(SCHEMAS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_schemas(s):
    import json as _json
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        SCHEMAS_FILE.write_text(_json.dumps(s, ensure_ascii=False),
                                encoding="utf-8")
    except Exception:
        pass


def _ds_path(schema_name):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", schema_name)[:60]
    return ANALYTICS_DIR / f"{safe}.json"


def load_ds(schema_name):
    import json as _json
    p = _ds_path(schema_name)
    if p.exists():
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
            d["fields"] = list(d.get("fields", []))
            d["papers"] = dict(d.get("papers", {}))
            d["rows"] = list(d.get("rows", []))
            return d
        except Exception:
            pass
    return {"fields": [], "papers": {}, "rows": []}


def save_ds(schema_name, d):
    import json as _json
    try:
        ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _ds_path(schema_name).with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(d, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(_ds_path(schema_name))
    except Exception:
        pass


ANALYTICS_SYSTEM = """\
You extract a structured data table from ONE document (a research paper,
or a grant proposal / evaluation report). You get FIELDS (name + hint)
and the document text. Respond with ONLY a JSON array of 1-5 row
objects. Each row maps EVERY field name EXACTLY as given to a value:
- numbers as plain numbers (no units inside the value),
- short strings for categorical fields,
- null when the document does not state it.
Use multiple rows ONLY when the document genuinely reports multiple
distinct items (e.g. several devices or several tests). NEVER invent or
estimate a value - null is always the safe answer.
Additionally, EVERY row must include the key "_evidence": one sentence
or table fragment (max 40 words) COPIED VERBATIM from the document
that contains the row's main numeric value(s). Copy exactly -
"_evidence" is checked mechanically against the document text, and a
paraphrased or invented quote marks the whole row as unverified. Use
null only if the row has no numeric values."""


def _quote_in_text(quote, text):
    """Does the claimed evidence quote actually appear in the paper?
    Normalised substring match, with a token-overlap fallback that
    still requires EVERY numeric token to be present."""
    if not quote or not text:
        return False

    def _norm(s):
        s = re.sub(r"[^a-z0-9.]+", " ", str(s).lower())
        s = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    q, t = _norm(quote), _norm(text)
    toks = q.split()
    if len(toks) < 3:
        return False              # too short to count as evidence
    if q in t:
        return True
    if len(toks) < 4:
        return False
    nums = [w for w in toks if any(c.isdigit() for c in w)]
    if nums and not all(n in t for n in nums):
        return False
    hits = sum(1 for w in toks if w in t)
    return hits / len(toks) >= 0.8


def parse_rows(raw, fields):
    """Best-effort recovery of the JSON row array."""
    import json as _json
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    a, b = raw.find("["), raw.rfind("]")
    if a == -1 or b <= a:
        raise ValueError("no JSON array in reply")
    data = _json.loads(raw[a:b + 1])
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        row = {}
        for f in fields:
            v = r.get(f)
            if isinstance(v, str):
                v = v.strip()[:200] or None
            row[f] = v
        has_content = any(v is not None for v in row.values())
        ev = r.get("_evidence")
        row["_evidence"] = (ev.strip()[:300]
                            if isinstance(ev, str) and ev.strip()
                            else None)
        if has_content:
            out.append(row)
    return out[:5]


def numericize(df, fields):
    """Coerce columns that are mostly numeric; returns (df, numeric_cols,
    category_cols)."""
    import pandas as pd
    num_cols, cat_cols = [], []
    for f in fields:
        if f not in df.columns:
            continue
        as_num = pd.to_numeric(
            df[f].astype(str).str.extract(r"(-?\d+\.?\d*)")[0],
            errors="coerce")
        nonnull = int(df[f].notna().sum())
        parseable = int(as_num.notna().sum())
        if parseable >= 3 and parseable / max(nonnull, 1) >= 0.6:
            df[f] = as_num
            num_cols.append(f)
        else:
            cat_cols.append(f)
    return df, num_cols, cat_cols


# --------------------------------------------------------------------------
# Scientific QC: statistics, physics sanity rules, verification
# --------------------------------------------------------------------------
def mann_whitney_u(a, b):
    """Two-sided Mann-Whitney U test, normal approximation with tie and
    continuity correction (good for n>=8 per group; flagged otherwise).
    Returns (U, p) or (None, None) if a group is too small."""
    import numpy as np
    import pandas as pd
    from math import erfc, sqrt
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    n1, n2 = len(a), len(b)
    if n1 < 3 or n2 < 3:
        return None, None
    allv = np.concatenate([a, b])
    ranks = pd.Series(allv).rank().to_numpy()
    r1 = ranks[:n1].sum()
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u = min(u1, n1 * n2 - u1)
    mu = n1 * n2 / 2.0
    n = n1 + n2
    _, counts = np.unique(allv, return_counts=True)
    tie = (counts ** 3 - counts).sum()
    var = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1)))
    if var <= 0:
        return float(u), 1.0
    z = (abs(u - mu) - 0.5) / np.sqrt(var)
    p = erfc(z / sqrt(2.0))
    return float(u), float(min(max(p, 0.0), 1.0))


def group_stats(pdat, x, y):
    """Per-group n / median / IQR table + pairwise Mann-Whitney p's."""
    import itertools
    rows, groups = [], {}
    for g, sub in pdat.groupby(x):
        vals = sub[y].dropna()
        if len(vals) == 0:
            continue
        groups[str(g)] = vals
        rows.append({x: str(g), "n": len(vals),
                     "median": round(float(vals.median()), 3),
                     "IQR": f"{vals.quantile(.25):.3g}"
                            f"–{vals.quantile(.75):.3g}"})
    pairs = []
    for g1, g2 in itertools.combinations(sorted(groups), 2):
        u, p = mann_whitney_u(groups[g1], groups[g2])
        if p is None:
            continue
        small = len(groups[g1]) < 8 or len(groups[g2]) < 8
        pairs.append({"comparison": f"{g1} vs {g2}",
                      "n": f"{len(groups[g1])}/{len(groups[g2])}",
                      "p (Mann-Whitney)": round(p, 4),
                      "verdict": ("small n - indicative only" if small
                                  else ("significant (p<0.05)"
                                        if p < 0.05
                                        else "not significant"))})
    return rows, pairs


def _qc_col(cols, pattern):
    for c in cols:
        if re.search(pattern, str(c), re.I):
            return c
    return None


def _ff_frac(v):
    """Fill factor to a 0-1 fraction whether reported as 0.82 or 82."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v:                # NaN
        return None
    return v / 100.0 if v > 1.5 else v


QC_RULES_DOC = """\
- FF outside 0.25-0.92 (after %/fraction normalisation)
- PCE outside 0-34 %  ·  Voc outside 0.2-5 V  ·  Jsc outside 0-50 mA/cm2
- Voc ≥ bandgap (fine for 2T tandems - check the cell type)
- PCE cross-check: |Voc x Jsc x FF - reported PCE| > 10 %
- identical metric values appearing in more than one paper"""


def qc_dataset(df, fields):
    """Physics sanity pass. Returns (flags, colmap): flags is a list of
    {row, rule, detail}; row indexes into df."""
    cols = [f for f in fields if f in df.columns]
    cm = {"pce": _qc_col(cols, r"pce|efficien"),
          "voc": _qc_col(cols, r"voc"),
          "jsc": _qc_col(cols, r"jsc"),
          "ff": _qc_col(cols, r"^ff|fill\s*factor"),
          "eg": _qc_col(cols, r"bandgap|band\s*gap|\beg\b"),
          "area": _qc_col(cols, r"area")}
    flags = []

    def _num(row, key):
        c = cm[key]
        if c is None:
            return None
        try:
            v = float(row[c])
            return v if v == v else None
        except (TypeError, ValueError):
            return None

    for i, row in df.iterrows():
        pce, voc, jsc = _num(row, "pce"), _num(row, "voc"), _num(row,
                                                                 "jsc")
        eg, area = _num(row, "eg"), _num(row, "area")
        ff = _ff_frac(row[cm["ff"]]) if cm["ff"] is not None else None
        if ff is not None and not 0.25 <= ff <= 0.92:
            flags.append({"row": i, "rule": "FF out of range",
                          "detail": f"FF={ff:.3g} (as fraction)"})
        if pce is not None and not 0 < pce <= 34:
            flags.append({"row": i, "rule": "PCE out of range",
                          "detail": f"PCE={pce:g} %"})
        if voc is not None and not 0.2 <= voc <= 5:
            flags.append({"row": i, "rule": "Voc out of range",
                          "detail": f"Voc={voc:g} V"})
        if jsc is not None and not 0 < jsc <= 50:
            flags.append({"row": i, "rule": "Jsc out of range",
                          "detail": f"Jsc={jsc:g} mA/cm2"})
        if voc is not None and eg is not None and voc >= eg > 0:
            flags.append({"row": i, "rule": "Voc >= bandgap",
                          "detail": f"Voc={voc:g} V, Eg={eg:g} eV "
                                    "(fine for 2T tandem - check "
                                    "cell type)"})
        if area is not None and not 0.001 <= area <= 10000:
            flags.append({"row": i, "rule": "Area implausible",
                          "detail": f"{area:g} cm2"})
        if None not in (pce, voc, jsc, ff) and pce > 0:
            calc = voc * jsc * ff
            dev = abs(calc - pce) / pce
            if dev > 0.10:
                flags.append({"row": i,
                              "rule": "PCE cross-check failed",
                              "detail": f"Voc x Jsc x FF = {calc:.2f} "
                                        f"vs reported {pce:g} "
                                        f"({dev * 100:.0f}% off)"})
    # same metric values in different papers -> possible duplicate
    mcols = [c for c in (cm["pce"], cm["voc"], cm["jsc"]) if c]
    if len(mcols) >= 2 and "_sig" in df.columns:
        def _missing(v):
            if v is None:
                return True
            try:
                return bool(v != v)      # float NaN
            except Exception:
                return True              # pd.NA and friends
        groups = {}
        for i, row in df.iterrows():
            vals = [row[c] for c in mcols]
            if any(_missing(v) for v in vals):
                continue
            k = " / ".join(str(v) for v in vals)
            groups.setdefault(k, []).append(i)
        for k, idxs in groups.items():
            sigs = {df.at[i, "_sig"] for i in idxs}
            if len(sigs) > 1:
                for i in idxs:
                    flags.append({"row": i,
                                  "rule": "Duplicate across papers",
                                  "detail": f"{len(idxs)} rows share "
                                            f"{'/'.join(mcols)} = {k}"})
    return flags, cm


def _verify_agree(a, b):
    """Do a stored value and a re-extracted value agree? Numeric: within
    2% (or 0.02 absolute near zero). Both-null agrees; one-null does
    not."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= max(0.02 * abs(fb), 0.02)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


TRENDS_SYSTEM = """\
You describe a small extracted dataset for a researcher, from its
aggregate statistics and sample rows. Write 5-10 sentences: the main
patterns, ranges and outliers worth checking, and which comparisons look
statistically meaningful vs anecdotal (state group sizes). Use ONLY the
numbers provided; no speculation beyond them; mention data gaps (nulls)
explicitly."""


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


FILE_YEAR = re.compile(r"_((?:19|20)\d{2})_")

# Journal-family detection (shared cache with the Workbench:
# answers/journals.json). DOI prefix first, name patterns as fallback.
JOURNALS_FILE = ANSWERS_DIR / "journals.json"
DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s,;)\]]+)", re.I)
JOURNAL_PATTERNS = [
    (r"nature\s+(energy|materials|communications|photonics|"
     r"nanotechnology|physics|chemistry|reviews|sustainability|"
     r"catalysis)", "Nature"),
    (r"\bnpj\b|scientific\s+reports|communications\s+(materials|"
     r"physics|chemistry)", "Nature"),
    (r"science\s+advances|\bsci\.?\s*adv\b", "Science (AAAS)"),
    (r"journal\s+of\s+the\s+american\s+chemical\s+society|"
     r"\bjacs\b|\bacs\s+|chemistry\s+of\s+materials|"
     r"nano\s+letters", "ACS"),
    (r"energy\s*&?\s*environmental\s+science|\bees\b|"
     r"chemical\s+science|journal\s+of\s+materials\s+chemistry|"
     r"nanoscale|green\s+chemistry|\brsc\b|chem\.?\s*commun",
     "RSC"),
    (r"advanced\s+(materials|energy\s+materials|functional\s+"
     r"materials|science|optical)|angewandte|\bsmall\b|solar\s+rrl|"
     r"infomat|ecomat|progress\s+in\s+photovoltaics", "Wiley"),
    (r"\bjoule\b|\bmatter\b|nano\s+energy|solar\s+energy\s+"
     r"materials|cell\s+reports|journal\s+of\s+power\s+sources|"
     r"applied\s+surface|chemical\s+engineering\s+journal",
     "Elsevier"),
    (r"\bieee\b|journal\s+of\s+photovoltaics", "IEEE"),
    (r"applied\s+physics\s+letters|journal\s+of\s+applied\s+"
     r"physics|\bapl\b", "AIP"),
    (r"\bmdpi\b|\benergies\b|nanomaterials|\bcrystals\b", "MDPI"),
    (r"nano-?micro\s+letters|journal\s+of\s+materials\s+science",
     "Springer"),
    (r"optics\s+express|\boptica\b", "Optica"),
    (r"\barxiv\b|chemrxiv", "Preprint"),
    (r"\bnature\b", "Nature"),
    (r"\bscience\b", "Science (AAAS)"),
]


def detect_journal(text):
    if not text:
        return "Unknown"
    try:
        from literature.publishers import publisher_for_doi
    except Exception:
        publisher_for_doi = None
    if publisher_for_doi:
        for m in DOI_IN_TEXT.finditer(text[:3000]):
            pub = publisher_for_doi(m.group(1).rstrip("."))
            if pub:
                return pub.split(" (")[0]
    low = re.sub(r"\s+", " ", text[:3000]).lower()
    for pattern, family in JOURNAL_PATTERNS:
        if re.search(pattern, low):
            return family
    return "Unknown"


def scan_journals_here(total, progress_cb=None):
    """One batched pass over the index: earliest text chunk per paper ->
    journal family. Saves to the shared journals.json cache."""
    import json as _json
    col = load_collection()
    best = {}
    offset, BATCH = 0, 2000
    while offset < total:
        got = col.get(include=["documents", "metadatas"],
                      limit=BATCH, offset=offset)
        metas, docs = got["metadatas"], got["documents"]
        if not metas:
            break
        for d, m in zip(docs, metas):
            if m.get("type") == "figure":
                continue
            sig = m.get("doc_sig")
            page = m.get("page_start", 9999) or 9999
            cur = best.get(sig)
            if cur is None or page < cur[0]:
                best[sig] = (page, (d or "")[:3000])
        offset += len(metas)
        if progress_cb:
            progress_cb(min(offset / max(total, 1), 1.0))
    found = {sig: detect_journal(txt) for sig, (_p, txt) in best.items()}
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        JOURNALS_FILE.write_text(_json.dumps(found), encoding="utf-8")
    except Exception:
        pass
    return found


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


def build_trends_text(df, num_cols, cat_cols, name, api_key, model):
    """The Describe-trends call, shared by the button and the report."""
    desc = (df[[c for c in num_cols if c != "_year"]]
            .describe().round(3).to_csv() if num_cols else "")
    counts = "\n".join(
        f"{c}: " + ", ".join(f"{k}({v})" for k, v in
                             df[c].value_counts().head(8).items())
        for c in cat_cols)
    sample = df.head(30).to_csv(index=False)
    umsg = (f"DATASET: {name} - {len(df)} rows from "
            f"{df['_sig'].nunique()} documents\n\n"
            f"NUMERIC SUMMARY:\n{desc}\n\nCATEGORY COUNTS:\n{counts}"
            f"\n\nSAMPLE ROWS:\n{sample}")
    return call_claude(api_key, TRENDS_SYSTEM, umsg, model,
                       max_tokens=1500)


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Analytics - by GrapheAI",
                   page_icon="📊", layout="wide")

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
    "<p class='an-title'>📊 Analytics</p>"
    "<p class='an-sub'>data extraction & plotting · by <b>GrapheAI</b> · "
    "developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("📊 Analytics")
    st.caption("by GrapheAI · Dr. Anurag Krishna")
    backend_label = st.radio(
        "Claude access", ["API key (pay per use)",
                          "Claude Max subscription (needs Claude Code)",
                          "OpenAI API (ChatGPT models, pay per use)"])
    st.session_state["backend"] = ("max" if "Max" in backend_label
                                    else "openai" if "OpenAI" in backend_label
                                    else "api")
    if st.session_state["backend"] == "openai":
        api_key = render_openai_sidebar()
    elif st.session_state["backend"] == "api":
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
    try:
        _col = load_collection()
        n_chunks = _col.count()
        index_ok = True
        st.success(f"Corpus: {n_chunks} chunks")
    except Exception:
        index_ok = False
        st.error("Index not found - run from the PaperRag folder.")
    u = st.session_state.get("usage")
    if u:
        st.caption(f"Session: {u['calls']} calls - "
                   f"{u['in']:,}/{u['out']:,} tokens")

try:
    import pandas as _pd
except Exception:
    st.error("pandas is required.")
    st.stop()
try:
    import altair as alt
except Exception:
    alt = None

st.caption("Define WHAT to extract (any fields), run it across papers "
           "and/or proposals, then plot the results. Datasets grow "
           "incrementally and persist in answers/analytics/.")

with st.expander("❓ How Analytics works - 3 steps"):
    st.markdown(
        "**1. 🧩 Schemas** - decide WHAT to extract. A schema is a list "
        "of fields (columns) with hints. Example: pick the preset *PV "
        "device metrics*, give it a name like `tandem survey`, Save.\n\n"
        "**2. 🏗️ Extract** - decide WHERE from and run it. Pick your "
        "schema, choose source folders (papers, proposals, ...) or "
        "specific papers, then Extract - Claude reads each document and "
        "fills one table row per device/test it reports. Progress is "
        "saved continuously; run more batches any time.\n\n"
        "**3. 📈 Plot studio** - make figures. Pick the dataset (or "
        "upload your own CSV/Excel, or use the Material Library), choose "
        "a plot type and columns - e.g. Scatter with X = `Bandgap (eV)`, "
        "Y = `Voc (V)`, colour = `Architecture`. Export as 300-dpi "
        "publication figure or CSV.\n\n"
        "*Typical first run: preset schema → 50 papers → a bandgap-vs-Voc "
        "scatter from your own literature, in ~10 minutes.*")

tab_schema, tab_extract, tab_plot, tab_qc = st.tabs(
    ["🧩 Schemas", "🏗️ Extract", "📈 Plot studio", "🔬 QC"])

# ------------------------------ SCHEMAS -----------------------------------
with tab_schema:
    st.markdown("**A schema = what to extract.** Each field has a name "
                "(becomes a column) and a hint that tells Claude exactly "
                "what to look for. Start from a preset and edit freely.")
    schemas = load_schemas()
    sc1, sc2 = st.columns(2)
    with sc1:
        base = st.selectbox("Start from",
                            ["(blank)"] + list(PRESET_SCHEMAS)
                            + [f"saved: {s}" for s in sorted(schemas)],
                            key="sch_base")
    with sc2:
        sch_name = st.text_input("Schema name", key="sch_name",
                                 placeholder="e.g. Tandem device survey")
    if base == "(blank)":
        seed = [("", "")] * 5
    elif base.startswith("saved: "):
        seed = [tuple(x) for x in schemas[base[7:]]]
    else:
        seed = PRESET_SCHEMAS[base]
    fdf = _pd.DataFrame(seed, columns=["Field", "Hint"])
    edited = st.data_editor(fdf, num_rows="dynamic",
                            use_container_width=True,
                            key=f"sch_ed_{base}")
    if st.button("💾 Save schema", type="primary", key="sch_save",
                 disabled=not sch_name.strip()):
        rows = [(str(r["Field"]).strip(), str(r["Hint"]).strip())
                for _, r in edited.iterrows()
                if str(r["Field"]).strip()]
        if len(rows) < 2:
            st.error("A schema needs at least 2 fields.")
        else:
            schemas[sch_name.strip()] = rows
            save_schemas(schemas)
            st.success(f"Saved '{sch_name.strip()}' with "
                       f"{len(rows)} fields.")
    if schemas:
        st.markdown("---")
        del_s = st.multiselect("Delete saved schemas", sorted(schemas),
                               key="sch_del")
        if del_s and st.button("Delete selected", key="sch_del_btn"):
            for s in del_s:
                schemas.pop(s, None)
            save_schemas(schemas)
            st.rerun()

# ------------------------------ EXTRACT -----------------------------------
with tab_extract:
    schemas = load_schemas()
    if not index_ok:
        st.info("No corpus found.")
    elif not schemas:
        st.info("Save a schema first (🧩 Schemas).")
    else:
        ex_schema = st.selectbox("Schema", sorted(schemas),
                                 key="ex_schema")
        fields = [f for f, h in schemas[ex_schema]]
        hints = dict(schemas[ex_schema])
        ds = load_ds(ex_schema)
        papers = list_papers(n_chunks)
        srcs = sorted({p["source"] for p in papers if p["source"]})
        ex1, ex2 = st.columns(2)
        with ex1:
            sel_srcs = st.multiselect(
                "Source folders", srcs, default=srcs, key="ex_srcs",
                help="Include papers/proposals folders as the schema "
                     "needs - Proposal metadata wants the proposals "
                     "and evaluations sources only.")
        with ex2:
            title_filter = st.text_input(
                "Only documents whose title/filename contains "
                "(optional)", key="ex_filter",
                placeholder="e.g. tandem")
        scope = [p for p in papers
                 if (p["source"] in sel_srcs or not srcs)]
        if title_filter.strip():
            tf = title_filter.strip().lower()
            scope = [p for p in scope
                     if tf in p["title"].lower()
                     or tf in p["file"].lower()]

        # Year (filename pattern) and journal-family filters
        jr_file = ANSWERS_DIR / "journals.json"
        journals = {}
        if jr_file.exists():
            try:
                import json as _json
                journals = _json.loads(
                    jr_file.read_text(encoding="utf-8"))
            except Exception:
                journals = {}
        ex3, ex4 = st.columns(2)
        with ex3:
            years_avail = sorted({m.group(1) for p in papers
                                  for m in [FILE_YEAR.search(p["file"])]
                                  if m})
            f_years = st.multiselect(
                "Years (pick any)", years_avail, key="ex_years",
                help="Uses the year in your filename pattern "
                     "(1001_topic_2025_Title.pdf). Empty = all years. "
                     "Papers without a year in the filename are "
                     "excluded while this filter is active.")
        with ex4:
            jr_opts = sorted({journals.get(p["sig"]) for p in papers
                              if journals.get(p["sig"])
                              and journals.get(p["sig"]) != "Unknown"})
            f_jr = st.multiselect(
                "Journal families (pick any)", jr_opts, key="ex_jr",
                disabled=not jr_opts,
                help="Multiple selections allowed. Filled by the "
                     "journal scan (button below, or the Workbench "
                     "Library tab - the cache is shared).")
            if not jr_opts:
                if st.button("🔍 Detect journals now (one-time scan, "
                             "no AI cost)", key="ex_jrscan"):
                    bar = st.progress(0.0, text="Scanning index...")
                    try:
                        found = scan_journals_here(
                            n_chunks,
                            lambda f: bar.progress(
                                f, text="Scanning index..."))
                        bar.empty()
                        st.success(f"Detected journals for "
                                   f"{len(found)} papers.")
                        st.rerun()
                    except Exception as e:
                        bar.empty()
                        st.error(f"Scan failed: {e}")
        if f_years:
            scope = [p for p in scope
                     for m in [FILE_YEAR.search(p["file"])]
                     if m and m.group(1) in f_years]
        if f_jr:
            scope = [p for p in scope
                     if journals.get(p["sig"]) in f_jr]

        pick_opts = {f"{p['title'][:70]} · {Path(p['file']).name[:30]}": p
                     for p in scope}
        picked = st.multiselect(
            "...or pick specific papers from the library (optional - "
            "overrides the filters above)",
            sorted(pick_opts), key="ex_pick",
            help="Type to search your indexed library. Leave empty to "
                 "use the folder/title scope.")
        if picked:
            scope = [pick_opts[l] for l in picked]
        todo = [p for p in scope
                if ds["papers"].get(p["sig"], {}).get("status")
                != "done"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("In scope", len(scope))
        m2.metric("Done", len(scope) - len(todo))
        m3.metric("Remaining", len(todo))
        m4.metric("Rows so far", len(ds["rows"]))
        bx1, bx2, bx3 = st.columns(3)
        with bx1:
            ex_model_label = st.selectbox("Model", list(MODELS.keys()),
                                          index=0, key="ex_model")
        with bx2:
            ex_n = st.selectbox("Documents this run",
                                [25, 50, 100, 200, "All remaining"],
                                index=1, key="ex_n")
        with bx3:
            ex_depth = st.selectbox(
                "Text depth per paper", [16000, 32000, 48000],
                index=0, key="ex_depth",
                format_func=lambda v: f"{v // 1000}k chars",
                help="How much of each paper Claude reads. 16k covers "
                     "the first ~4-6 pages (abstract, intro, main "
                     "results); raise it when values hide in late "
                     "sections - proportionally more tokens per paper.")
        n_run = (len(todo) if ex_n == "All remaining"
                 else min(int(ex_n), len(todo)))
        if st.button(f"🏗️ Extract {n_run} document(s)", type="primary",
                     key="ex_go", disabled=n_run == 0):
            can_call = (st.session_state.get("backend") == "max"
                        or bool(api_key.strip()))
            if not can_call:
                st.error("Needs the API key (sidebar).")
            else:
                ds["fields"] = fields
                field_block = "\n".join(
                    f"- {f}: {hints.get(f, '')}" for f in fields)
                bar = st.progress(0.0)
                ok = err = nrows = 0
                stop_reason = ""
                for i, p in enumerate(todo[:n_run], start=1):
                    bar.progress(i / n_run,
                                 text=f"{i}/{n_run}: {p['title'][:55]}")
                    try:
                        text = paper_full_text(
                            p["sig"], max_chars=ex_depth)
                        if len(text) < 400:
                            raise ValueError("too little text")
                        raw = call_claude(
                            api_key.strip(), ANALYTICS_SYSTEM,
                            f"FIELDS:\n{field_block}\n\n"
                            f"DOCUMENT: {p['title']}\n\n{text}",
                            MODELS[ex_model_label], max_tokens=1500)
                        rows = parse_rows(raw, fields)
                        ym = FILE_YEAR.search(p["file"])
                        for r in rows:
                            r["_ev_ok"] = _quote_in_text(
                                r.get("_evidence"), text)
                            r["_paper"] = p["title"]
                            r["_file"] = p["file"]
                            r["_sig"] = p["sig"]
                            r["_source"] = p["source"]
                            r["_year"] = (int(ym.group(1)) if ym
                                          else None)
                        ds["rows"] = [r for r in ds["rows"]
                                      if r.get("_sig") != p["sig"]]
                        ds["rows"].extend(rows)
                        ds["papers"][p["sig"]] = {
                            "status": "done", "n": len(rows)}
                        ok += 1
                        nrows += len(rows)
                    except Exception as e:
                        msg = str(e)
                        ds["papers"][p["sig"]] = {"status": "error",
                                                  "err": msg[:200]}
                        err += 1
                        if any(w in msg.lower() for w in
                               ("usage", "limit", "rate", "credit")):
                            stop_reason = msg
                            save_ds(ex_schema, ds)
                            break
                    save_ds(ex_schema, ds)
                bar.empty()
                if stop_reason:
                    st.warning(f"Stopped early: {stop_reason[:150]} - "
                               "progress saved, resume any time.")
                st.success(f"Extracted {ok} document(s) -> {nrows} "
                           f"row(s) ({err} errors). Dataset total: "
                           f"{len(ds['rows'])} rows.")
        errs = [(s, v) for s, v in ds["papers"].items()
                if v.get("status") == "error"]
        if errs and st.button(f"Retry {len(errs)} failed document(s)",
                              key="ex_retry"):
            for s, _v in errs:
                ds["papers"].pop(s, None)
            save_ds(ex_schema, ds)
            st.rerun()

        with st.expander("🌙 Nightly auto-extract"):
            st.caption("When enabled, the nightly job extracts newly "
                       "indexed papers matching the scope currently set "
                       "above into this dataset (up to 100/night, "
                       "Haiku). Needs the launchd job installed - see "
                       "Claude's setup instructions.")
            auto_now = bool(ds.get("auto"))
            auto_on = st.toggle("Auto-extract this schema nightly",
                                value=auto_now, key="ex_auto")
            if auto_on != auto_now:
                ds["auto"] = auto_on
                ds["auto_scope"] = {
                    "sources": sel_srcs,
                    "title": title_filter.strip(),
                    "years": f_years,
                    "journals": f_jr,
                    "depth": ex_depth,
                    "fields": schemas[ex_schema],
                }
                save_ds(ex_schema, ds)
                st.success("Saved - the nightly job will "
                           + ("include" if auto_on else "skip")
                           + " this schema.")

# ------------------------------ PLOT STUDIO -------------------------------
with tab_plot:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    df = None
    fields = []
    pd_name = "dataset"
    src_choice = st.radio(
        "Data source",
        ["Extracted dataset", "Upload my own data (CSV/Excel)",
         "Material Library"],
        horizontal=True, key="pl_src")
    if src_choice == "Extracted dataset":
        dsets = sorted(p.stem for p in ANALYTICS_DIR.glob("*.json"))
        if not dsets:
            st.info("No datasets yet - run an extraction first "
                    "(🏗️ Extract).")
        else:
            pd_name = st.selectbox("Dataset", dsets, key="pl_ds")
            ds = load_ds(pd_name)
            if not ds["rows"]:
                st.info("This dataset is empty.")
            else:
                df = _pd.DataFrame(ds["rows"])
                fields = [f for f in ds["fields"] if f in df.columns]
    elif src_choice.startswith("Upload"):
        upf = st.file_uploader("Your data file (.csv / .xlsx)",
                               type=["csv", "xlsx"], key="pl_up")
        if upf is not None:
            import io as _io
            try:
                if upf.name.lower().endswith(".xlsx"):
                    df = _pd.read_excel(_io.BytesIO(upf.getvalue()))
                else:
                    data = upf.getvalue()
                    head = data[:3000].decode("utf-8", errors="replace")
                    delim = (";" if head.count(";") > head.count(",")
                             else ",")
                    df = _pd.read_csv(_io.BytesIO(data), sep=delim)
                pd_name = upf.name.rsplit(".", 1)[0]
                fields = list(df.columns)
                st.caption(f"{len(df)} rows, {len(fields)} columns.")
            except Exception as e:
                st.error(f"Could not read the file: {e} "
                         "(.xlsx needs: pip install openpyxl)")
                df = None
    else:  # Material Library
        import json as _json
        mat_p = ANSWERS_DIR / "materials.json"
        if not mat_p.exists():
            st.info("No Material Library yet - build it in the "
                    "Material Library app (port 8503) first.")
        else:
            try:
                mrows = _json.loads(
                    mat_p.read_text(encoding="utf-8")).get("entries", [])
            except Exception:
                mrows = []
            if not mrows:
                st.info("The Material Library is empty.")
            else:
                df = _pd.DataFrame(mrows)
                pd_name = "material_library"
                fields = [c for c in ("material", "role", "device",
                                      "deposition", "best_pce")
                          if c in df.columns]
                df["_paper"] = df.get("paper", "?")
                if "file" in df.columns:
                    df["_year"] = df["file"].apply(
                        lambda f: (int(FILE_YEAR.search(str(f)).group(1))
                                   if FILE_YEAR.search(str(f)) else None))
                st.caption(f"{len(df)} material entries loaded.")

    if df is not None and len(df) and fields:
        if "_paper" not in df.columns:
            df["_paper"] = pd_name
        if "_sig" not in df.columns:
            df["_sig"] = df["_paper"]
        df, num_cols, cat_cols = numericize(df, fields)
        if "_year" in df.columns:
            num_cols = num_cols + ["_year"]
        real_num = [c for c in num_cols if c != "_year"]
        if df["_sig"].nunique() < len(df) and real_num:
            ch1, ch2 = st.columns([1.2, 2])
            with ch1:
                champ = st.checkbox("🏆 Best row per document only",
                                    key="pl_champ",
                                    help="Papers reporting several "
                                         "devices contribute several "
                                         "rows; this keeps only each "
                                         "paper's best, so prolific "
                                         "papers don't dominate plots.")
            with ch2:
                champ_col = (st.selectbox("best by", real_num,
                                          key="pl_champcol",
                                          label_visibility="collapsed")
                             if champ else None)
            if champ and champ_col:
                dfc = df.dropna(subset=[champ_col])
                df = dfc.loc[dfc.groupby("_sig")[champ_col].idxmax()]
        st.caption(f"{len(df)} rows from "
                   f"{df['_sig'].nunique()} documents · numeric: "
                   f"{', '.join(num_cols) or '-'} · categorical: "
                   f"{', '.join(cat_cols) or '-'}")
        ptype = st.radio("Plot",
                         ["Scatter", "Box by category",
                          "Bar (aggregate)", "Histogram"],
                         horizontal=True, key="pl_type")
        c1, c2, c3 = st.columns(3)
        chart = None
        if ptype == "Scatter" and num_cols:
            x_opts = num_cols + cat_cols
            with c1:
                x = st.selectbox("X (any parameter)", x_opts,
                                 key="pl_x")
            with c2:
                y_opts = [c for c in num_cols if c != x] or num_cols
                y = st.selectbox("Y (numeric)", y_opts,
                                 index=0, key="pl_y")
            with c3:
                color = st.selectbox("Colour by",
                                     ["(none)"] + cat_cols,
                                     key="pl_c")
            x_is_num = x in num_cols
            pdat = df.dropna(subset=[x, y])
            ov = None
            if not x_is_num:
                st.caption(f"'{x}' is categorical - the plot becomes a "
                           "strip plot; the lab-data overlay needs a "
                           "numeric X.")
            with st.expander("⭐ Overlay my own data on this plot"):
                if not x_is_num:
                    st.caption("Pick a numeric X to enable the overlay.")
                st.caption("Upload your lab results and mark them "
                           "as gold diamonds on top of the "
                           "literature - the 'here is the field, "
                           "here is us' figure.")
                ov_up = st.file_uploader("Your results (.csv/.xlsx)",
                                         type=["csv", "xlsx"],
                                         key="ov_up")
                if ov_up is not None and x_is_num:
                    import io as _io2
                    try:
                        if ov_up.name.lower().endswith(".xlsx"):
                            ovraw = _pd.read_excel(
                                _io2.BytesIO(ov_up.getvalue()))
                        else:
                            odata = ov_up.getvalue()
                            oh = odata[:2000].decode(
                                "utf-8", errors="replace")
                            od = (";" if oh.count(";")
                                  > oh.count(",") else ",")
                            ovraw = _pd.read_csv(
                                _io2.BytesIO(odata), sep=od)
                    except Exception as e:
                        st.error(f"Could not read: {e}")
                        ovraw = None
                    if ovraw is not None:
                        ocols = list(ovraw.columns)
                        o1, o2, o3 = st.columns(3)
                        ov_x = o1.selectbox(f"Your '{x}' column",
                                            ocols, key="ov_x")
                        ov_y = o2.selectbox(
                            f"Your '{y}' column", ocols,
                            index=min(1, len(ocols) - 1),
                            key="ov_y")
                        ov_lab = o3.selectbox("Label column",
                                              ["(none)"] + ocols,
                                              key="ov_lab")
                        ov = _pd.DataFrame({
                            x: _pd.to_numeric(ovraw[ov_x],
                                              errors="coerce"),
                            y: _pd.to_numeric(ovraw[ov_y],
                                              errors="coerce"),
                        }).dropna()
                        ov["label"] = (ovraw[ov_lab].astype(str)
                                       if ov_lab != "(none)"
                                       else "my device")
                        st.caption(f"{len(ov)} of your points "
                                   "will be overlaid.")
            if alt is not None and not pdat.empty:
                xenc = (alt.X(f"{x}:Q", title=x,
                              scale=alt.Scale(zero=False))
                        if x_is_num else
                        alt.X(f"{x}:N", title=x, sort="-y"))
                enc = {"x": xenc,
                       "y": alt.Y(f"{y}:Q", title=y,
                                  scale=alt.Scale(zero=False)),
                       "tooltip": ["_paper:N", f"{x}:Q", f"{y}:Q"]}
                if color != "(none)":
                    enc["color"] = alt.Color(f"{color}:N")
                chart = alt.Chart(pdat).mark_circle(
                    size=70, opacity=0.7).encode(**enc)
                if ov is not None and len(ov):
                    ovc = alt.Chart(ov).mark_point(
                        shape="diamond", size=260, filled=True,
                        color="#FFD166", stroke="#12161C",
                        strokeWidth=1.5).encode(
                        x=f"{x}:Q", y=f"{y}:Q",
                        tooltip=["label:N", f"{x}:Q", f"{y}:Q"])
                    chart = chart + ovc
        elif ptype == "Box by category" and num_cols and cat_cols:
            with c1:
                y = st.selectbox("Value", num_cols, key="pl_by")
            with c2:
                x = st.selectbox("Category", cat_cols, key="pl_bx")
            pdat = df.dropna(subset=[y, x])
            if alt is not None and not pdat.empty:
                chart = alt.Chart(pdat).mark_boxplot(
                    color="#FF6B3D").encode(
                    x=alt.X(f"{x}:N", title=x),
                    y=alt.Y(f"{y}:Q", title=y,
                            scale=alt.Scale(zero=False)))
        elif ptype == "Bar (aggregate)":
            with c1:
                x = st.selectbox("Group by",
                                 cat_cols or ["_source"],
                                 key="pl_gx")
            with c2:
                agg = st.selectbox("Show",
                                   ["count"] + [f"mean {n}"
                                                for n in num_cols]
                                   + [f"max {n}" for n in num_cols],
                                   key="pl_agg")
            if agg == "count":
                pdat = (df.groupby(x).size()
                        .reset_index(name="value"))
            else:
                how, colname = agg.split(" ", 1)
                pdat = (df.groupby(x)[colname].agg(how)
                        .reset_index(name="value"))
            pdat = pdat.dropna().sort_values("value",
                                             ascending=False)
            if alt is not None and not pdat.empty:
                chart = alt.Chart(pdat.head(20)).mark_bar(
                    color="#FF6B3D").encode(
                    x=alt.X("value:Q", title=agg),
                    y=alt.Y(f"{x}:N", sort="-x", title=x),
                    tooltip=[f"{x}:N", "value:Q"])
        elif ptype == "Histogram" and num_cols:
            with c1:
                x = st.selectbox("Value", num_cols, key="pl_hx")
            pdat = df.dropna(subset=[x])
            if alt is not None and not pdat.empty:
                chart = alt.Chart(pdat).mark_bar(
                    color="#8AB4F8").encode(
                    x=alt.X(f"{x}:Q", bin=alt.Bin(maxbins=25),
                            title=x),
                    y=alt.Y("count():Q", title="Count"))
        if chart is not None:
            st.altair_chart(chart, use_container_width=True)
            if ptype == "Box by category" and not pdat.empty:
                with st.expander("📐 Group statistics (n, median, "
                                 "IQR, Mann-Whitney)"):
                    grows, gpairs = group_stats(pdat, x, y)
                    if grows:
                        st.dataframe(_pd.DataFrame(grows),
                                     use_container_width=True,
                                     hide_index=True)
                    if gpairs:
                        st.dataframe(_pd.DataFrame(gpairs),
                                     use_container_width=True,
                                     hide_index=True)
                        st.caption(
                            "Two-sided Mann-Whitney U (normal "
                            "approximation). Only call a difference "
                            "real when p < 0.05 AND both groups have "
                            "reasonable n - with n < 8 per group "
                            "treat everything as indicative.")
            if st.button("🖼️ Export publication figure",
                         key="pl_fig"):
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(5.2, 3.5))
                if ptype == "Scatter":
                    if not x_is_num:
                        cats = sorted(pdat[x].astype(str).unique())
                        pos = {c_: i for i, c_ in enumerate(cats)}
                        xs = pdat[x].astype(str).map(pos)
                        ax.plot(xs, pdat[y], "o", ms=4, alpha=0.6,
                                color="#888888")
                        ax.set_xticks(range(len(cats)))
                        ax.set_xticklabels([c_[:12] for c_ in cats],
                                           rotation=45, fontsize=7)
                    elif color != "(none)":
                        for gname, g in pdat.groupby(color):
                            ax.plot(g[x], g[y], "o", ms=4,
                                    alpha=0.7, label=str(gname))
                    else:
                        ax.plot(pdat[x], pdat[y], "o", ms=4,
                                alpha=0.7, color="#888888",
                                label="literature")
                    if ov is not None and len(ov):
                        ax.plot(ov[x], ov[y], "D", ms=9,
                                color="#E3A008",
                                markeredgecolor="black",
                                label="this work")
                    if x_is_num:
                        ax.legend(frameon=False, fontsize=7)
                    ax.set_xlabel(x, fontsize=10)
                    ax.set_ylabel(y, fontsize=10)
                elif ptype == "Box by category":
                    cats = sorted(pdat[x].dropna().unique())
                    ax.boxplot([pdat[pdat[x] == c_][y].dropna()
                                for c_ in cats],
                               tick_labels=[str(c_)[:12]
                                            for c_ in cats])
                    ax.set_ylabel(y, fontsize=10)
                    ax.tick_params(axis="x", rotation=45,
                                   labelsize=8)
                elif ptype == "Bar (aggregate)":
                    top = pdat.head(15)[::-1]
                    ax.barh([str(v)[:20] for v in top[x]],
                            top["value"], color="#D2401E")
                    ax.set_xlabel(agg, fontsize=10)
                else:
                    ax.hist(pdat[x].dropna(), bins=25,
                            color="#46586A")
                    ax.set_xlabel(x, fontsize=10)
                    ax.set_ylabel("Count", fontsize=10)
                ax.spines[["top", "right"]].set_visible(False)
                fig.tight_layout()
                png = save_pub_fig(fig, f"analytics_{ptype}")
                plt.close(fig)
                st.success(f"Saved: {png} (+ .svg)")
                st.download_button("⬇️ PNG (300 dpi)",
                                   data=png.read_bytes(),
                                   file_name=png.name,
                                   mime="image/png", key="pl_dl")
        else:
            st.caption("Pick columns with enough data for this "
                       "plot type.")

        with st.expander("📋 Data table & export"):
            show = df[[c for c in fields + ["_paper", "_year",
                                            "_source"]
                       if c in df.columns]]
            sc1, sc2 = st.columns([2, 1])
            with sc1:
                sort_opts = ["(original order)"] + list(show.columns)
                default_sort = ("Year" if "Year" in show.columns
                                else ("_year" if "_year" in show.columns
                                      else "(original order)"))
                sort_by = st.selectbox(
                    "Sort by", sort_opts,
                    index=sort_opts.index(default_sort),
                    key="pl_sort")
            with sc2:
                sort_desc = st.checkbox("Newest/largest first",
                                        value=True, key="pl_sortdesc")
            if sort_by != "(original order)":
                show = show.sort_values(sort_by, ascending=not sort_desc,
                                        na_position="last")
            st.dataframe(show, use_container_width=True,
                         height=320)
            st.download_button(
                "⬇️ Dataset CSV (sorted as shown)",
                data=show.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{pd_name}.csv", mime="text/csv")

        if st.button("🧠 Describe trends", key="pl_trends"):
            can_call = (st.session_state.get("backend") == "max"
                        or bool(api_key.strip()))
            if not can_call:
                st.error("Needs the API key (sidebar).")
            else:
                with st.spinner("Analysing..."):
                    try:
                        out = build_trends_text(
                            df, num_cols, cat_cols, pd_name,
                            api_key.strip(),
                            MODELS["Frontier (Fable 5.1)"])
                    except Exception as e:
                        st.error(f"Claude error: {e}")
                        out = None
                if out:
                    st.markdown(out)

        st.markdown("---")
        rb1, rb2 = st.columns([1.5, 2])
        with rb1:
            rep_go = st.button("📄 Build Word report", key="pl_repgo",
                               type="primary")
        with rb2:
            rep_ai = st.checkbox("Include AI trends narrative",
                                 value=True, key="pl_repai")
        if rep_go:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from docx import Document
            from docx.shared import Inches
            doc = Document()
            doc.add_heading(f"Analytics report - {pd_name}", level=1)
            doc.add_paragraph(
                f"{len(df)} rows from {df['_sig'].nunique()} documents. "
                f"Generated {datetime.date.today().isoformat()} by "
                "Analytics (GrapheAI).")
            realn = [c for c in num_cols if c != "_year"]
            if realn:
                doc.add_heading("Numeric summary", level=2)
                desc = df[realn].describe().round(3)
                t = doc.add_table(rows=len(desc.index) + 1,
                                  cols=len(desc.columns) + 1)
                try:
                    t.style = "Table Grid"
                except Exception:
                    pass
                for ci, cname in enumerate(desc.columns):
                    t.cell(0, ci + 1).text = str(cname)
                for ri, rname in enumerate(desc.index):
                    t.cell(ri + 1, 0).text = str(rname)
                    for ci, cname in enumerate(desc.columns):
                        t.cell(ri + 1, ci + 1).text = str(
                            desc.iloc[ri, ci])
            figs = []
            try:
                if realn:
                    f1, a1 = plt.subplots(figsize=(5.2, 3.2))
                    a1.hist(df[realn[0]].dropna(), bins=25,
                            color="#46586A")
                    a1.set_xlabel(realn[0])
                    a1.set_ylabel("Count")
                    a1.spines[["top", "right"]].set_visible(False)
                    figs.append((f1, f"Distribution of {realn[0]}"))
                if realn and cat_cols:
                    f2, a2 = plt.subplots(figsize=(5.2, 3.2))
                    cats = [c for c in
                            df[cat_cols[0]].dropna().unique()][:8]
                    a2.boxplot([df[df[cat_cols[0]] == c_]
                                [realn[0]].dropna() for c_ in cats],
                               tick_labels=[str(c_)[:10]
                                            for c_ in cats])
                    a2.set_ylabel(realn[0])
                    a2.tick_params(axis="x", rotation=45, labelsize=7)
                    a2.spines[["top", "right"]].set_visible(False)
                    figs.append((f2, f"{realn[0]} by {cat_cols[0]}"))
                ycol = ("Year" if "Year" in num_cols else
                        ("_year" if "_year" in df.columns else None))
                if realn and ycol is not None:
                    f3, a3 = plt.subplots(figsize=(5.2, 3.2))
                    sc = df.dropna(subset=[realn[0], ycol])
                    a3.plot(sc[ycol], sc[realn[0]], "o", ms=4,
                            alpha=0.6, color="#D2401E")
                    a3.set_xlabel("Year")
                    a3.set_ylabel(realn[0])
                    a3.spines[["top", "right"]].set_visible(False)
                    figs.append((f3, f"{realn[0]} over time"))
            except Exception:
                pass
            if figs:
                doc.add_heading("Figures", level=2)
                for fg, cap in figs:
                    png = save_pub_fig(fg, f"report_{pd_name}")
                    plt.close(fg)
                    doc.add_picture(str(png), width=Inches(5.5))
                    doc.add_paragraph(cap)
            if rep_ai:
                can_call2 = (st.session_state.get("backend") == "max"
                             or bool(api_key.strip()))
                if can_call2:
                    with st.spinner("Writing trends narrative..."):
                        try:
                            ttxt = build_trends_text(
                                df, num_cols, cat_cols, pd_name,
                                api_key.strip(),
                                MODELS["Frontier (Fable 5.1)"])
                            doc.add_heading("Trends", level=2)
                            md_to_docx(doc, ttxt)
                        except Exception as e:
                            st.warning(f"Narrative skipped: {e}")
            doc.add_heading("Data (first 150 rows, year-sorted)",
                            level=2)
            tcols = ([c for c in fields][:6]
                     + [c for c in ("_paper", "_year")
                        if c in df.columns])
            tdf = df[tcols]
            ysort = ("Year" if "Year" in tdf.columns
                     else ("_year" if "_year" in tdf.columns
                           else None))
            if ysort:
                tdf = tdf.sort_values(ysort, ascending=False,
                                      na_position="last")
            tdf = tdf.head(150)
            t2 = doc.add_table(rows=len(tdf) + 1, cols=len(tcols))
            try:
                t2.style = "Table Grid"
            except Exception:
                pass
            for ci, cname in enumerate(tcols):
                t2.cell(0, ci).text = str(cname)
            for ri in range(len(tdf)):
                for ci, cname in enumerate(tcols):
                    v = tdf.iloc[ri, ci]
                    t2.cell(ri + 1, ci).text = ("" if _pd.isna(v)
                                                else str(v)[:80])
            if len(df) > 150:
                doc.add_paragraph(f"({len(df) - 150} further rows in "
                                  "the CSV export.)")
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", pd_name)[:50]
            outp = (ANSWERS_DIR
                    / f"Analytics_report_{safe}_{stamp}.docx")
            doc.save(outp)
            st.success(f"Report saved: {outp}")
            st.download_button("⬇️ Report (.docx)",
                               data=outp.read_bytes(),
                               file_name=outp.name,
                               mime="application/vnd.openxmlformats-"
                                    "officedocument.wordprocessingml"
                                    ".document",
                               key="pl_repdl")

# ------------------------------ QC ----------------------------------------
with tab_qc:
    st.markdown("**Quality control for extracted data** - free physics "
                "sanity checks, plus a Claude verification sample that "
                "puts a measured error bar on the whole dataset.")
    qdsets = (sorted(p.stem for p in ANALYTICS_DIR.glob("*.json"))
              if ANALYTICS_DIR.exists() else [])
    if not qdsets:
        st.info("No datasets yet - run an extraction first.")
    else:
        q_name = st.selectbox("Dataset", qdsets, key="qc_ds")
        qds = load_ds(q_name)
        if not qds["rows"]:
            st.info("This dataset is empty.")
        else:
            qdf = _pd.DataFrame(qds["rows"])
            qfields = [f for f in qds["fields"] if f in qdf.columns]
            qdf, qnum, qcat = numericize(qdf, qfields)

            ver = qds.get("verification")
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Rows", len(qdf))
            b2.metric("Papers", qdf["_sig"].nunique()
                      if "_sig" in qdf.columns else "—")
            if "_ev_ok" in qdf.columns:
                ev_known = qdf["_ev_ok"].notna()
                n_ev = int(ev_known.sum())
                ok_ev = int(qdf.loc[ev_known, "_ev_ok"]
                            .fillna(False).astype(bool).sum())
                b3.metric("Evidence verified",
                          f"{ok_ev}/{n_ev}" if n_ev else "—")
            else:
                b3.metric("Evidence verified", "older rows")
            b4.metric("Verified agreement",
                      f"{ver['overall'] * 100:.0f}% (n={ver['n']})"
                      if ver else "not yet")

            if "_ev_ok" in qdf.columns:
                bad_ev = qdf[qdf["_ev_ok"].notna()
                             & ~qdf["_ev_ok"].fillna(False)
                             .astype(bool)]
                if len(bad_ev):
                    with st.expander(
                            f"🧿 {len(bad_ev)} row(s) whose evidence "
                            "quote did NOT match the paper text - "
                            "treat these values as unverified"):
                        show_cols = ([c for c in qfields
                                      if c in bad_ev.columns]
                                     + ["_evidence", "_paper"])
                        st.dataframe(bad_ev[show_cols],
                                     use_container_width=True,
                                     hide_index=True)
                        st.caption(
                            "Each extracted row now carries a "
                            "verbatim quote that is checked "
                            "mechanically against the paper. A "
                            "failed check usually means the value "
                            "was paraphrased, misread, or "
                            "hallucinated - re-extract that paper "
                            "with a stronger model, or check it by "
                            "hand. Rows extracted before this "
                            "feature have no quote and show as "
                            "'older rows'.")

            st.markdown("### 1 · Physics sanity checks (free, instant)")
            with st.expander("Rules applied"):
                st.markdown(QC_RULES_DOC)
            flags, qcm = qc_dataset(qdf, qfields)
            found = ", ".join(f"{k}→{v}" for k, v in qcm.items() if v)
            st.caption("Column mapping: " + (found or
                       "no PV metric columns recognised - rules that "
                       "need them are skipped."))
            if not flags:
                st.success("No rows flagged. ✓")
            else:
                st.warning(f"{len(flags)} flag(s) on "
                           f"{len({f['row'] for f in flags})} row(s) "
                           f"of {len(qdf)}.")
                frows = []
                for f in flags:
                    r = qdf.loc[f["row"]]
                    frows.append({
                        "rule": f["rule"], "detail": f["detail"],
                        "paper": str(r.get("_paper", ""))[:70],
                        "year": r.get("_year"),
                        **{c: r.get(c) for c in
                           (qcm["pce"], qcm["voc"], qcm["jsc"],
                            qcm["ff"]) if c}})
                fdf = _pd.DataFrame(frows)
                st.dataframe(fdf, use_container_width=True,
                             hide_index=True)
                st.download_button("⬇️ Flagged rows CSV",
                                   fdf.to_csv(index=False),
                                   file_name=f"{q_name}_qc_flags.csv",
                                   key="qc_csv")
                st.caption("Flags are review prompts, not verdicts - "
                           "a flagged row may be a real (unusual) "
                           "result, an extraction slip, or a paper "
                           "worth double-checking. Nothing is deleted "
                           "automatically.")

            st.markdown("### 2 · Verification sample (Claude re-reads "
                        "the papers)")
            st.caption("Re-extracts a random sample at full text depth "
                       "with a strong model and compares numeric "
                       "fields against the stored values. The result "
                       "is the error bar for this dataset.")
            v1, v2 = st.columns(2)
            with v1:
                n_ver = st.slider("Sample size", 5, 30, 15,
                                  key="qc_n")
            with v2:
                ver_model = st.selectbox("Model", list(MODELS),
                                         index=3, key="qc_model")
            num_fields = [f for f in qnum if f in qfields]
            if not num_fields:
                st.info("No numeric fields to verify in this schema.")
            elif st.button("🔬 Run verification sample",
                           type="primary", key="qc_go"):
                import random as _random
                cand = [r for r in qds["rows"] if r.get("_sig")]
                sample = (_random.sample(cand, n_ver)
                          if len(cand) > n_ver else list(cand))
                hints = dict(qds["fields"])
                field_block = "\n".join(
                    f"- {f}: {hints.get(f, '')}"
                    for f in [x[0] for x in qds["fields"]])
                agree = {f: 0 for f in num_fields}
                checked = {f: 0 for f in num_fields}
                errors = 0
                cache = {}
                prog = st.progress(0.0, text="Verifying...")
                for si, row in enumerate(sample):
                    sig = row["_sig"]
                    try:
                        if sig not in cache:
                            text = paper_full_text(sig, 48000)
                            if len(text) < 400:
                                raise ValueError("too little text")
                            raw = call_claude(
                                api_key, ANALYTICS_SYSTEM,
                                f"FIELDS:\n{field_block}\n\n"
                                f"DOCUMENT: {row.get('_paper', '?')}"
                                f"\n\n{text}",
                                MODELS[ver_model], max_tokens=2000)
                            cache[sig] = parse_rows(
                                raw, [x[0] for x in qds["fields"]])
                        newrows = cache[sig]
                        # best-matching re-extracted row
                        best, best_n = None, -1
                        for nr in newrows:
                            nmatch = sum(
                                1 for f in num_fields
                                if _verify_agree(row.get(f),
                                                 nr.get(f)))
                            if nmatch > best_n:
                                best, best_n = nr, nmatch
                        if best is None:
                            raise ValueError("no rows re-extracted")
                        for f in num_fields:
                            if row.get(f) is None and \
                                    best.get(f) is None:
                                continue
                            checked[f] += 1
                            if _verify_agree(row.get(f), best.get(f)):
                                agree[f] += 1
                    except Exception:
                        errors += 1
                    prog.progress((si + 1) / len(sample),
                                  text=f"Verifying... "
                                       f"{si + 1}/{len(sample)}")
                prog.empty()
                tot_c = sum(checked.values())
                tot_a = sum(agree.values())
                result = {
                    "date": datetime.date.today().isoformat(),
                    "model": MODELS[ver_model],
                    "n": len(sample) - errors,
                    "errors": errors,
                    "overall": (tot_a / tot_c) if tot_c else 0.0,
                    "per_field": {
                        f: {"checked": checked[f], "agree": agree[f]}
                        for f in num_fields},
                }
                qds["verification"] = result
                save_ds(q_name, qds)
                st.rerun()

            if ver:
                st.markdown(
                    f"**Last verification** ({ver['date']}, "
                    f"{ver.get('model', '?')}): "
                    f"**{ver['overall'] * 100:.0f}% field agreement** "
                    f"over {ver['n']} sampled row(s)"
                    + (f", {ver['errors']} paper(s) unreadable"
                       if ver.get("errors") else "") + ".")
                pf = []
                for f, d in ver.get("per_field", {}).items():
                    pct = (f"{d['agree'] / d['checked'] * 100:.0f}%"
                           if d["checked"] else "—")
                    pf.append({"field": f, "checked": d["checked"],
                               "agree": d["agree"],
                               "agreement": pct})
                if pf:
                    st.dataframe(_pd.DataFrame(pf),
                                 use_container_width=True,
                                 hide_index=True)
                st.caption("Agreement = value within 2% on re-reading "
                           "with a stronger model at full depth. "
                           "Fields well below the overall rate are "
                           "candidates for a better hint in the "
                           "schema, or for re-extracting with a "
                           "stronger model.")
