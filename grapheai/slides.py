"""
Slide Studio - by GrapheAI. Turns any GrapheAI output (reports, memos,
manuscripts, datasets) into a finished PowerPoint deck with speaker
notes and your publication figures.

Run with:
    streamlit run slides.py --server.port 8507

Needs python-pptx:   pip install python-pptx

Shares with the rest of GrapheAI:
  - answers/                    source documents + figures_out/
  - answers/decks/              generated .pptx decks
  - answers/spend.json          monthly API spend + budget
  - the Claude backend          API key or Claude Max via Claude Code
"""

import datetime
import json
import re
from pathlib import Path

import sys
import streamlit as st

ANSWERS_DIR = Path("answers")
DECKS_DIR = ANSWERS_DIR / "decks"
FIGS_DIR = ANSWERS_DIR / "figures_out"

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
# Source documents
# --------------------------------------------------------------------------
def read_docx_text(path):
    """Docx -> markdown-ish text: heading styles become #/## so both
    the AI outline and Quick convert keep the document structure."""
    try:
        from docx import Document
        doc = Document(path)
        parts = []
        for p in doc.paragraphs:
            txt = p.text.strip()
            if not txt:
                continue
            style = (p.style.name or "") if p.style is not None else ""
            if style.startswith("Heading 1") or style == "Title":
                parts.append("# " + txt)
            elif style.startswith("Heading 2"):
                parts.append("## " + txt)
            elif style.startswith("Heading"):
                parts.append("### " + txt)
            elif style.startswith("List"):
                parts.append("- " + txt)
            else:
                parts.append(txt)
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip()
                                        for c in row.cells))
        return "\n".join(parts)
    except Exception:
        return ""


def list_answer_docs():
    """Text-bearing outputs in answers/, newest first."""
    if not ANSWERS_DIR.exists():
        return []
    docs = []
    for p in ANSWERS_DIR.rglob("*"):
        if (p.is_file() and p.suffix.lower() in
                (".md", ".txt", ".docx", ".csv")
                and "decks" not in p.parts
                and not p.name.startswith("~$")):
            docs.append(p)
    return sorted(docs, key=lambda p: p.stat().st_mtime, reverse=True)


def read_source(path):
    p = Path(path)
    if p.suffix.lower() == ".docx":
        return read_docx_text(p)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def list_figures():
    figs = []
    if FIGS_DIR.exists():
        figs = [p for p in FIGS_DIR.glob("*.png")]
    return sorted(figs, key=lambda p: p.stat().st_mtime, reverse=True)


# --------------------------------------------------------------------------
# Outline generation
# --------------------------------------------------------------------------
SLIDES_SYSTEM = """\
You design a slide deck from source material. Respond with ONLY one
JSON object, no prose, no code fences:

{"title": "deck title, max 9 words",
 "subtitle": "one line",
 "slides": [
   {"kind": "section" | "content" | "image" | "closing",
    "title": "max 8 words - a MESSAGE, not a topic label",
    "bullets": ["max 5 bullets, each max 14 words", ...],
    "figure": null or EXACTLY one filename from AVAILABLE FIGURES,
    "notes": "2-4 sentences of speaker notes"}]}

Rules:
- Slide titles state the takeaway ("Tandems cut LCOE 18%"), never a
  label ("Results").
- One message per slide. "section" slides have no bullets (divider).
- "image" slides: the figure carries the slide; at most 2 bullets.
- Use ONLY figures from AVAILABLE FIGURES, and only where genuinely
  relevant; figure=null otherwise. Never invent filenames.
- All numbers and claims must come from the SOURCE MATERIAL - never
  invent data. If the material lacks something, leave it out.
- Respect the requested slide count (+/-1) and the audience."""


def parse_outline(raw, valid_figs):
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("no JSON object in the reply")
    data = json.loads(raw[a:b + 1])
    slides = []
    for s in data.get("slides", []):
        if not isinstance(s, dict):
            continue
        fig = s.get("figure")
        if fig and fig not in valid_figs:
            fig = None
        slides.append({
            "kind": s.get("kind") if s.get("kind") in
            ("section", "content", "image", "closing") else "content",
            "title": str(s.get("title") or "")[:120],
            "bullets": [str(b)[:180] for b in
                        (s.get("bullets") or [])[:6]],
            "figure": fig,
            "notes": str(s.get("notes") or "")[:1200],
        })
    if not slides:
        raise ValueError("the outline contained no slides")
    return {"title": str(data.get("title") or "Untitled deck")[:120],
            "subtitle": str(data.get("subtitle") or "")[:200],
            "slides": slides}


# --------------------------------------------------------------------------
# Quick convert (no AI): headings -> slide titles, bullets -> bullets
# --------------------------------------------------------------------------
MAX_BULLETS_PER_SLIDE = 6
MAX_BULLET_CHARS = 140


def _clean_inline(text):
    text = re.sub(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`",
                  lambda m: m.group(1) or m.group(2) or m.group(3),
                  text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_BULLET_CHARS:
        cut = text[:MAX_BULLET_CHARS]
        text = cut[:cut.rfind(" ")] + " …" if " " in cut else cut + "…"
    return text


def outline_from_markdown(md, fallback_title):
    """Deterministic markdown/text -> outline (same shape the AI
    produces), so render_deck works unchanged. No Claude call."""
    title = None
    slides = []          # list of {"title", "bullets"}
    cur = None

    def _push():
        nonlocal cur
        if cur and (cur["bullets"] or cur["title"]):
            slides.append(cur)
        cur = None

    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("|") or set(s) <= {"-", "=", "*", "_"}:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            text = _clean_inline(m.group(2))
            if title is None and len(m.group(1)) == 1:
                title = text
                continue
            _push()
            cur = {"title": text, "bullets": []}
            continue
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)", s)
        text = _clean_inline(m.group(1) if m else s)
        if not text:
            continue
        if cur is None:
            cur = {"title": "", "bullets": []}
        cur["bullets"].append(text)
    _push()

    # split overfull slides into continuations
    final = []
    for sl in slides:
        chunks = ([sl["bullets"][i:i + MAX_BULLETS_PER_SLIDE]
                   for i in range(0, len(sl["bullets"]),
                                  MAX_BULLETS_PER_SLIDE)]
                  or [[]])
        for j, chunk in enumerate(chunks):
            t = sl["title"] or (title or fallback_title)
            final.append({"kind": "content",
                          "title": t + (" (cont.)" if j else ""),
                          "bullets": chunk, "figure": None,
                          "notes": ""})
    if not final:
        raise ValueError("nothing convertible found in the source")
    return {"title": (title or fallback_title or "Untitled deck")[:120],
            "subtitle": "Quick conversion · GrapheAI Slide Studio",
            "slides": final}


# --------------------------------------------------------------------------
# Deck rendering (python-pptx)
# --------------------------------------------------------------------------
TEMPLATES = {
    "Investor (light)": {
        "bg": "FFFFFF", "text": "1A212B", "muted": "6B7683",
        "accent": "FF6B3D", "bar": "FF6B3D",
    },
    "Conference (dark)": {
        "bg": "12161C", "text": "E6EAF0", "muted": "8E99A8",
        "accent": "FF8A5C", "bar": "FF6B3D",
    },
}

SLIDE_W, SLIDE_H = 13.333, 7.5   # inches, 16:9


def _rgb(hex6):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex6)


def _fill(shape, hex6):
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(hex6)
    shape.line.fill.background()


def _textbox(slide, x, y, w, h, text, size, color, bold=False,
             align="left", font="Calibri"):
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN
    box = slide.shapes.add_textbox(Inches(x), Inches(y),
                                   Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    lines = text.split("\n") if isinstance(text, str) else list(text)
    for i, line in enumerate(lines):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.alignment = {"left": PP_ALIGN.LEFT,
                         "center": PP_ALIGN.CENTER}[align]
        run = par.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = font
        run.font.color.rgb = _rgb(color)
    return box


def _bullets_box(slide, x, y, w, h, bullets, size, color):
    from pptx.util import Inches, Pt
    box = slide.shapes.add_textbox(Inches(x), Inches(y),
                                   Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    for i, b in enumerate(bullets):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.space_after = Pt(10)
        run = par.add_run()
        run.text = "▪  " + b
        run.font.size = Pt(size)
        run.font.color.rgb = _rgb(color)
    return box


def _base_slide(prs, theme):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0,
                                Inches(SLIDE_W), Inches(SLIDE_H))
    _fill(bg, theme["bg"])
    bg.shadow.inherit = False
    return slide


def _accent_bar(slide, theme, x, y, w, h):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x),
                                 Inches(y), Inches(w), Inches(h))
    _fill(bar, theme["bar"])
    bar.shadow.inherit = False


def _add_notes(slide, text):
    if text:
        slide.notes_slide.notes_text_frame.text = text


def _add_picture_fitted(slide, img_path, x, y, max_w, max_h):
    from pptx.util import Inches
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            iw, ih = im.size
    except Exception:
        iw, ih = 4, 3
    ratio = min(max_w / max(iw, 1), max_h / max(ih, 1))
    w, h = iw * ratio, ih * ratio
    slide.shapes.add_picture(str(img_path),
                             Inches(x + (max_w - w) / 2),
                             Inches(y + (max_h - h) / 2),
                             Inches(w), Inches(h))


def render_deck(outline, template_name, fig_paths):
    """outline dict -> pptx bytes + saved file path."""
    from pptx import Presentation
    from pptx.util import Inches
    theme = TEMPLATES[template_name]
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    figs = {p.name: p for p in fig_paths}

    # --- title slide
    s = _base_slide(prs, theme)
    _accent_bar(s, theme, 0.9, 2.35, 2.6, 0.09)
    _textbox(s, 0.9, 2.6, 11.5, 1.8, outline["title"], 40,
             theme["text"], bold=True)
    if outline["subtitle"]:
        _textbox(s, 0.9, 4.05, 11.5, 0.8, outline["subtitle"], 18,
                 theme["muted"])
    _textbox(s, 0.9, 6.55, 11.5, 0.5,
             "Dr. Anurag Krishna  ·  "
             + datetime.date.today().strftime("%B %Y"),
             13, theme["muted"])
    _add_notes(s, "Title slide.")

    n_section = 0
    for sl in outline["slides"]:
        kind = sl["kind"]
        if kind == "section":
            n_section += 1
            s = _base_slide(prs, theme)
            _textbox(s, 0.9, 2.5, 2.0, 1.2, f"{n_section:02d}", 54,
                     theme["accent"], bold=True)
            _accent_bar(s, theme, 0.95, 3.75, 1.4, 0.07)
            _textbox(s, 0.9, 4.0, 11.0, 1.5, sl["title"], 32,
                     theme["text"], bold=True)
        elif kind == "closing":
            s = _base_slide(prs, theme)
            _textbox(s, 0.9, 2.9, 11.5, 1.5, sl["title"] or "Thank you",
                     36, theme["text"], bold=True, align="center")
            if sl["bullets"]:
                _bullets_box(s, 3.2, 4.4, 7.0, 2.0, sl["bullets"], 16,
                             theme["muted"])
        else:
            s = _base_slide(prs, theme)
            _accent_bar(s, theme, 0.9, 0.62, 0.55, 0.07)
            _textbox(s, 0.9, 0.78, 11.6, 1.1, sl["title"], 26,
                     theme["text"], bold=True)
            fig = figs.get(sl.get("figure") or "")
            if kind == "image" and fig:
                if sl["bullets"]:
                    _bullets_box(s, 0.9, 2.0, 4.1, 4.8,
                                 sl["bullets"], 15, theme["text"])
                    _add_picture_fitted(s, fig, 5.3, 1.95, 7.2, 5.0)
                else:
                    _add_picture_fitted(s, fig, 1.6, 1.95, 10.1, 5.1)
            elif fig:
                _bullets_box(s, 0.9, 2.0, 5.6, 4.8, sl["bullets"], 16,
                             theme["text"])
                _add_picture_fitted(s, fig, 6.9, 1.95, 5.6, 5.0)
            else:
                _bullets_box(s, 0.9, 2.05, 11.4, 4.9, sl["bullets"],
                             18, theme["text"])
        _add_notes(s, sl["notes"])

    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", outline["title"])[:50]
    path = DECKS_DIR / f"{safe}_{stamp}.pptx"
    prs.save(path)
    return path


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Slide Studio - by GrapheAI",
                   page_icon="🎤", layout="wide")

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
    "<p class='an-title'>🎤 Slide Studio</p>"
    "<p class='an-sub'>decks from your outputs · by <b>GrapheAI</b> · "
    "developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("🎤 Slide Studio")
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
        import pptx  # noqa: F401
        st.success("python-pptx ready")
        HAVE_PPTX = True
    except ImportError:
        HAVE_PPTX = False
        st.error("Missing python-pptx.\n\nRun once in Terminal:\n"
                 "`/opt/miniconda3/bin/pip install python-pptx`")
    u = st.session_state.get("usage")
    if u:
        st.caption(f"Session: {u['calls']} calls - "
                   f"{u['in']:,}/{u['out']:,} tokens")

with st.expander("❓ How Slide Studio works"):
    st.markdown(
        "**1. Source** - pick any output already in `answers/` (a PV "
        "Radar report, an Analytics summary, a manuscript...), upload a "
        "file, or paste text.\n\n"
        "**2. Figures** - tick which of your publication figures "
        "(`answers/figures_out/`) Claude may place on slides.\n\n"
        "**3. Build** - choose audience, template and length; Claude "
        "writes a message-per-slide outline with speaker notes, and the "
        "deck is rendered to `answers/decks/` as .pptx - open it in "
        "PowerPoint or Keynote and fine-tune.\n\n"
        "*Every number on the slides comes from your source material - "
        "Claude is instructed never to invent data.*")

# ------------------------------ SOURCE ------------------------------------
st.markdown("## 1 · Source material")
src_mode = st.radio("Source", ["Pick from answers/", "Upload a file",
                               "Paste text"],
                    horizontal=True, key="src_mode")
source_text, source_label = "", ""
if src_mode == "Pick from answers/":
    docs = list_answer_docs()
    if not docs:
        st.info("Nothing in answers/ yet.")
    else:
        names = [str(p.relative_to(ANSWERS_DIR)) for p in docs[:200]]
        pick = st.selectbox("Document (newest first)", names,
                            key="src_pick")
        source_text = read_source(ANSWERS_DIR / pick)
        source_label = pick
elif src_mode == "Upload a file":
    up = st.file_uploader("File", type=["md", "txt", "docx", "csv"],
                          key="src_up")
    if up:
        if up.name.lower().endswith(".docx"):
            tmp = DECKS_DIR / "_upload.docx"
            DECKS_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(up.getvalue())
            source_text = read_docx_text(tmp)
        else:
            source_text = up.getvalue().decode("utf-8", errors="replace")
        source_label = up.name
else:
    source_text = st.text_area("Paste your material", height=240,
                               key="src_paste")
    source_label = "pasted text"

if source_text:
    st.caption(f"Loaded **{source_label}** - "
               f"{len(source_text):,} characters"
               + (" (will be trimmed to 60,000)"
                  if len(source_text) > 60000 else ""))

# ------------------------------ FIGURES -----------------------------------
st.markdown("## 2 · Figures (optional)")
figs = list_figures()
chosen_figs = []
if figs:
    fig_names = [p.name for p in figs[:60]]
    sel = st.multiselect("Figures Claude may use "
                         "(from answers/figures_out/)",
                         fig_names, key="fig_sel")
    chosen_figs = [p for p in figs if p.name in sel]
    if chosen_figs:
        cols = st.columns(min(len(chosen_figs), 4))
        for i, p in enumerate(chosen_figs[:8]):
            with cols[i % len(cols)]:
                st.image(str(p), caption=p.name, use_container_width=True)
else:
    st.caption("No figures in answers/figures_out yet - export some "
               "from Analytics or PeroDeg first.")
up_figs = st.file_uploader("...or upload extra images",
                           type=["png", "jpg", "jpeg"],
                           accept_multiple_files=True, key="fig_up")
if up_figs:
    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    for f in up_figs:
        dest = FIGS_DIR / f.name
        dest.write_bytes(f.getvalue())
        if dest not in chosen_figs:
            chosen_figs.append(dest)

# ------------------------------ BUILD -------------------------------------
st.markdown("## 3 · Build the deck")
mode = st.radio(
    "Mode",
    ["🧠 AI-designed (Claude reworks it into a real deck)",
     "⚡ Quick convert (no AI, instant, free: headings → slides)"],
    horizontal=True, key="bd_mode")
quick = mode.startswith("⚡")

if quick:
    template = st.selectbox("Template", list(TEMPLATES), key="bd_tpl_q")
    st.caption("Structure follows the document: every heading becomes "
               "a slide, bullets stay bullets, long slides are split. "
               "Good for turning a saved answer or report into slides "
               "in one click - use AI mode when you want it re-thought "
               "for an audience.")
    if st.button("⚡ Convert to deck", type="primary", key="bd_go_q",
                 disabled=not HAVE_PPTX):
        if not source_text.strip():
            st.warning("Load some source material first.")
        else:
            try:
                outline = outline_from_markdown(
                    source_text,
                    Path(source_label).stem.replace("_", " ")
                    if source_label else "Untitled deck")
                path = render_deck(outline, template, [])
                st.session_state["deck_path"] = str(path)
                st.session_state["deck_outline"] = outline
            except Exception as e:
                st.error(f"Quick convert failed: {e}")

if not quick:
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        audience = st.selectbox("Audience",
                                ["Investors", "Scientific conference",
                                 "Project review / consortium",
                                 "General / outreach"], key="bd_aud")
    with b2:
        template = st.selectbox("Template", list(TEMPLATES),
                                key="bd_tpl")
    with b3:
        n_slides = st.slider("Slides", 6, 20, 10, key="bd_n")
    with b4:
        bd_model = st.selectbox("Model", list(MODELS), index=3,
                                key="bd_model")
    extra = st.text_input("Extra instructions (optional)",
                          key="bd_extra",
                          placeholder="e.g. emphasise the stability "
                                      "data; end with the ask")

if not quick and st.button("🎤 Generate deck", type="primary",
                           key="bd_go", disabled=not HAVE_PPTX):
    if not source_text.strip():
        st.warning("Load some source material first.")
    else:
        fig_list = "\n".join(p.name for p in chosen_figs) or "(none)"
        umsg = (f"AUDIENCE: {audience}\n"
                f"REQUESTED SLIDES: {n_slides} (excluding title)\n"
                f"AVAILABLE FIGURES:\n{fig_list}\n"
                + (f"EXTRA INSTRUCTIONS: {extra}\n" if extra.strip()
                   else "")
                + f"\nSOURCE MATERIAL ({source_label}):\n"
                + source_text[:60000])
        try:
            with st.spinner("Designing the deck..."):
                raw = call_claude(api_key, SLIDES_SYSTEM, umsg,
                                  MODELS[bd_model], max_tokens=6000)
                outline = parse_outline(
                    raw, {p.name for p in chosen_figs})
                path = render_deck(outline, template, chosen_figs)
            st.session_state["deck_path"] = str(path)
            st.session_state["deck_outline"] = outline
        except Exception as e:
            st.error(f"Deck generation failed: {e}")

outline = st.session_state.get("deck_outline")
deck_path = st.session_state.get("deck_path")
if outline and deck_path and Path(deck_path).exists():
    st.markdown("---")
    st.success(f"Deck saved: `{deck_path}`")
    with open(deck_path, "rb") as fh:
        st.download_button(
            "⬇️ Download .pptx", fh.read(),
            file_name=Path(deck_path).name,
            mime="application/vnd.openxmlformats-officedocument."
                 "presentationml.presentation", key="deck_dl")
    st.markdown(f"### {outline['title']}")
    st.caption(outline["subtitle"])
    for i, sl in enumerate(outline["slides"], 1):
        with st.expander(f"Slide {i} · {sl['kind']} — {sl['title']}"):
            for b in sl["bullets"]:
                st.markdown(f"- {b}")
            if sl.get("figure"):
                st.caption(f"Figure: {sl['figure']}")
            if sl["notes"]:
                st.caption(f"🗣️ {sl['notes']}")

st.markdown("---")
with st.expander("📁 Previous decks"):
    prev = (sorted(DECKS_DIR.glob("*.pptx"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
            if DECKS_DIR.exists() else [])
    if not prev:
        st.caption("None yet.")
    for p in prev[:15]:
        st.markdown(f"- `{p.name}` — "
                    f"{datetime.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
