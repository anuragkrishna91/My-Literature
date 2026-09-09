"""
Material Library - by GrapheAI. Round 1: extraction engine + browse/export.

Classifies the materials inside every paper of the corpus - absorber
compositions, passivation agents, transport layers, electrodes, additives -
together with their reported properties and device impact, into a
structured, queryable library.

Run with:
    streamlit run materials.py --server.port 8503

Shares with GrapheAI / PV Radar:
  - chroma_db/                 read-only source of paper full text
  - answers/materials.json     the extracted library (grows incrementally)
  - answers/spend.json         monthly API spend + budget
  - the Claude backend         API key or Claude Max via Claude Code

It reads text only (no semantic search), so it opens WITHOUT loading the
embedding model - startup is seconds.
"""

import datetime
import re
from pathlib import Path

import sys
import json
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
    if st.session_state.get("backend") not in ("max", "codex"):
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
    ChatGPT subscription: through OpenAI's Codex CLI signed in with your ChatGPT Plus/Pro account. Personal use of your own subscriptions only. max_tokens is not enforced on
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


# ---------------------------------------------------------- ChatGPT subscription backend (Codex CLI)
# Fourth backend: the models of a ChatGPT Plus/Pro plan (GPT-5.6 family,
# GPT-6-Astra, ...) through OpenAI's official Codex CLI signed in with the
# ChatGPT account - the same idea as Claude Max through Claude Code. No API
# key; usage counts against the plan's Codex limits. Every call is one
# non-interactive `codex exec` in an empty scratch folder with the
# read-only sandbox, asked for a plain answer, so nothing on disk is
# touched. The reasoning-effort slider maps 1:1 (low ... max), clamped to
# the levels the chosen model supports. Model ids come from the CLI's own
# catalogue (`codex debug models`), never from the app.
CODEX_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
CODEX_TIMEOUT = 3600            # seconds per call (long manuscripts at effort max)
CODEX_INSTALL_HINT = ("install it in Terminal with `npm install -g @openai/codex` "
                      "(needs Node) or `brew install --cask codex`, then run "
                      "`codex login` and sign in with your ChatGPT account - "
                      "CodexLogin.command does both")
CODEX_PREAMBLE = (
    "You are the writing and analysis model behind GrapheAI, a personal research "
    "platform. This is a single non-interactive request: answer it directly in your "
    "final message. Do not run commands, do not read, create or edit files, do not "
    "browse the web, do not ask questions and do not mention tools - there is no "
    "repository to inspect; everything you need is below. Follow the INSTRUCTIONS "
    "exactly (format, length, markers, JSON) and put the complete answer text in "
    "your final message.")


def _codex_candidates():
    """(origin, path) for Codex CLI binaries: the path typed in the sidebar,
    PATH, Homebrew, the standalone installer, npm globals, nvm."""
    import glob
    import shutil
    home = Path.home()
    cands = []
    override = (st.session_state.get("codex_path") or "").strip()
    if override:
        cands.append(("chosen path", override))
    w = shutil.which("codex")
    if w:
        cands.append(("on PATH", w))
    for p in ["/opt/homebrew/bin/codex", "/usr/local/bin/codex", home / ".codex/bin/codex",
              home / ".npm-global/bin/codex", home / ".local/bin/codex"]:
        cands.append(("installed codex", str(p)))
    for p in (glob.glob(str(home / ".nvm/versions/node/*/bin/codex"))
              + glob.glob("/opt/node*/bin/codex")):
        cands.append(("installed codex", p))
    seen, out = set(), []
    for origin, p in cands:
        if p and p not in seen and Path(p).is_file():
            seen.add(p)
            out.append((origin, p))
    return out


def _codex_env(path):
    """Environment for the CLI: PATH with the usual macOS tool folders so the
    npm launcher finds `node` even when the app was started by double-click."""
    import glob
    import os
    env = dict(os.environ)
    home = str(Path.home())
    extra = [str(Path(path).parent), "/opt/homebrew/bin", "/usr/local/bin",
             f"{home}/.npm-global/bin", f"{home}/.codex/bin"]
    extra += glob.glob(f"{home}/.nvm/versions/node/*/bin")
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    env.setdefault("NO_COLOR", "1")
    return env


def _codex_run(path, args, stdin_text=None, timeout=120, cwd=None):
    import subprocess
    return subprocess.run([path] + list(args), input=stdin_text, capture_output=True,
                          text=True, timeout=timeout, env=_codex_env(path), cwd=cwd)


def codex_status(refresh=False):
    """The newest Codex CLI found ({path, version, origin, login, login_text}),
    cached for the session. `login` is True when `codex login status`
    reports a signed-in account."""
    import re as _re
    key = "_codex_status"
    if not refresh and key in st.session_state:
        return st.session_state[key]
    best = {}
    for origin, p in _codex_candidates():
        try:
            r = _codex_run(p, ["--version"], timeout=30)
        except Exception:
            continue
        m = _re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
        if not m:
            continue
        v = m.group(0)
        if not best or _ver_tuple(v) > _ver_tuple(best["version"]):
            best = {"path": p, "version": v, "origin": origin}
    if best:
        try:
            r = _codex_run(best["path"], ["login", "status"], timeout=30)
            txt = ((r.stdout or "") + (r.stderr or "")).strip()
            best["login"] = r.returncode == 0 and "not logged in" not in txt.lower()
            best["login_text"] = (txt.splitlines()[0].strip() if txt else
                                  ("Logged in" if best["login"] else "Not logged in"))
        except Exception as e:
            best["login"] = False
            best["login_text"] = f"login status failed: {str(e)[:80]}"
    st.session_state[key] = best
    return best


def codex_models(path, refresh=False):
    """Model catalogue of this Codex CLI as [{'slug', 'name', 'levels',
    'default', 'hidden', 'images', 'desc'}] in the CLI's own order."""
    key = "_codex_models"
    cache = st.session_state.get(key) or {}
    if not refresh and cache.get("path") == path:
        return cache["models"]
    models = []
    try:
        r = _codex_run(path, ["debug", "models"], timeout=60)
        raw = r.stdout or ""
        i = raw.find("{")
        d = json.loads(raw[i:]) if i >= 0 else {}
        for m in d.get("models", []) or []:
            slug = str(m.get("slug") or "").strip()
            if not slug:
                continue
            levels = [str(lv.get("effort")) for lv in (m.get("supported_reasoning_levels") or [])
                      if isinstance(lv, dict) and lv.get("effort")]
            mods = m.get("input_modalities") or []
            models.append({"slug": slug, "name": str(m.get("display_name") or slug),
                           "levels": levels, "default": str(m.get("default_reasoning_level") or ""),
                           "hidden": m.get("visibility") == "hide",
                           "images": (not mods) or any("image" in str(x).lower() for x in mods),
                           "desc": str(m.get("description") or "")})
    except Exception:
        models = []
    st.session_state[key] = {"path": path, "models": models}
    return models


def _codex_effort(model_info=None):
    """Slider effort clamped to what the model supports (never above)."""
    eff = st.session_state.get("effort", "high")
    if eff not in CODEX_EFFORT_ORDER:
        eff = "high"
    levels = [lv for lv in (model_info or {}).get("levels", []) if lv in CODEX_EFFORT_ORDER]
    if levels and eff not in levels:
        idx = CODEX_EFFORT_ORDER.index(eff)
        lower = [lv for lv in levels if CODEX_EFFORT_ORDER.index(lv) <= idx]
        eff = max(lower or levels, key=CODEX_EFFORT_ORDER.index)
    return eff


def _codex_parse_events(stdout):
    """(last agent message, usage, error) from `codex exec --json` lines:
    item.completed/agent_message carries text, turn.completed the usage."""
    text, usage, err = "", {}, ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("type") or "")
        item = ev.get("item")
        if (t.startswith("item.") and isinstance(item, dict)
                and item.get("type") == "agent_message" and item.get("text")):
            text = str(item["text"])
        if t == "turn.completed" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            usage = {"input_tokens": int(u.get("input_tokens") or 0),
                     "output_tokens": int(u.get("output_tokens") or 0),
                     "cached_input_tokens": int(u.get("cached_input_tokens") or 0)}
        if t in ("turn.failed", "error") or ev.get("error"):
            e = ev.get("error") or ev.get("message") or ev
            err = str(e.get("message") or e) if isinstance(e, dict) else str(e)
    return text.strip(), usage, err


def _codex_error_message(err, stderr, returncode):
    import re as _re
    tail = _re.sub(r"\x1b\[[0-9;]*m", "", stderr or "").strip()
    tail = "\n".join(ln for ln in tail.splitlines()
                     if ln.strip() and "bubblewrap" not in ln and "Reading additional input" not in ln)[-700:]
    low = (err + " " + tail).lower()
    hint = ""
    if any(k in low for k in ("not logged in", "unauthorized", "401", "login", "auth")):
        hint = " Sign in again: `codex login` in Terminal (or CodexLogin.command), then Re-check."
    elif any(k in low for k in ("usage limit", "rate limit", "429", "quota", "too many")):
        hint = " Your ChatGPT plan's Codex window seems used up - wait, or switch backend."
    elif "model" in low and any(k in low for k in ("not found", "unsupported", "not available",
                                                    "unknown", "does not")):
        hint = " That model id is not available to this account - pick another in the sidebar."
    msg = err or tail or f"codex exec exited with code {returncode}"
    return f"Codex error: {msg[:900]}{hint}"


def call_codex(system, user_msg, max_tokens=None, images=None):
    """One text call through `codex exec` (ChatGPT subscription). max_tokens
    is accepted for signature parity; the CLI has no output cap."""
    import os
    import shutil
    import subprocess
    import tempfile
    info = codex_status()
    if not info:
        raise RuntimeError(f"Codex CLI not found - {CODEX_INSTALL_HINT}.")
    if not info.get("login"):
        raise RuntimeError("Codex CLI is not signed in - run `codex login` in Terminal (or "
                           "double-click CodexLogin.command), sign in with your ChatGPT "
                           "account, then click Re-check in the sidebar.")
    model = (st.session_state.get("codex_model") or "").strip()
    if not model:
        raise RuntimeError("Choose a ChatGPT model in the sidebar.")
    minfo = next((m for m in codex_models(info["path"]) if m["slug"] == model), {})
    if images and minfo and not minfo.get("images", True):
        raise RuntimeError(f"{model} does not accept images (figure review needs a vision model).")
    effort = _codex_effort(minfo)
    prompt = (f"{CODEX_PREAMBLE}\n\n=== INSTRUCTIONS ===\n{system}\n\n"
              f"=== REQUEST ===\n{user_msg}\n")
    work = Path(tempfile.mkdtemp(prefix="grapheai_codex_"))
    out = work / "last_message.txt"
    args = ["exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "--color", "never", "--json", "-o", str(out), "-m", model, "-C", str(work),
            "-c", f'model_reasoning_effort="{effort}"', "-c", 'model_verbosity="high"']
    for img in (images or []):
        args += ["-i", str(img)]
    extra = os.environ.get("GRAPHEAI_CODEX_EXTRA_ARGS")     # testing / advanced overrides
    if extra:
        args += list(json.loads(extra))
    args.append("-")
    try:
        try:
            r = _codex_run(info["path"], args, stdin_text=prompt, timeout=CODEX_TIMEOUT,
                           cwd=str(work))
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Codex did not answer within {CODEX_TIMEOUT // 60} min "
                               "- lower the reasoning effort or split the task.")
        text, usage, err = _codex_parse_events(r.stdout)
        if out.exists():
            t2 = out.read_text(encoding="utf-8", errors="replace").strip()
            if t2:
                text = t2
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if usage:
        _track_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0), model)
    if r.returncode != 0 or (err and not text):
        raise RuntimeError(_codex_error_message(err, r.stderr, r.returncode))
    if not text:
        raise RuntimeError(_codex_error_message("Codex returned no answer text", r.stderr,
                                                r.returncode))
    return text


def call_codex_vision(system, text, png_path, max_tokens=3000):
    """Image + text call (figure critic): the PNG rides along with -i."""
    return call_codex(system, text, max_tokens, images=[png_path])


def render_codex_sidebar():
    """Sidebar for the ChatGPT-subscription backend; returns the api_key
    sentinel ('' until the CLI is signed in and a model is chosen)."""
    with st.expander("Codex CLI location (optional)", expanded=False):
        p = st.text_input("Path to `codex`", value=st.session_state.get("codex_path", ""),
                          key="codex_path_in",
                          help="Leave empty to search PATH, Homebrew, ~/.codex/bin, npm and nvm.")
        if p.strip() != (st.session_state.get("codex_path") or ""):
            st.session_state["codex_path"] = p.strip()
            codex_status(refresh=True)
    c1, c2 = st.columns([3, 1])
    if c2.button("Re-check", key="codex_recheck", help="After installing or `codex login`."):
        info = codex_status(refresh=True)
        if info:
            codex_models(info["path"], refresh=True)
    info = codex_status()
    if not info:
        c1.warning(f"Codex CLI not found - {CODEX_INSTALL_HINT}.")
        st.session_state["codex_model"] = ""
        return ""
    if not info.get("login"):
        c1.warning(f"Codex CLI {info['version']} found but not signed in "
                   f"({info.get('login_text', '')}). Run `codex login` in Terminal (or "
                   "double-click CodexLogin.command), choose 'Sign in with ChatGPT', then "
                   "click Re-check.")
    else:
        c1.caption(f"Codex CLI {info['version']} ({info['origin']}) · {info.get('login_text', '')}")
    models = codex_models(info["path"])
    listed = [m for m in models if not m["hidden"]] or models
    labels = [f"{m['name']}  ({m['slug']})" for m in listed] + ["Type a model id..."]
    pick = st.selectbox("ChatGPT model", labels, index=0, key="codex_model_pick",
                        help="From this Codex CLI's own catalogue (`codex debug models`). "
                             "The Claude selector below is ignored in this mode.")
    if pick == labels[-1] or not listed:
        slug = st.text_input("Model id", value=st.session_state.get("codex_model_typed", ""),
                             key="codex_model_typed_in",
                             help="Exact id as OpenAI names it for Codex.").strip()
        st.session_state["codex_model_typed"] = slug
    else:
        slug = listed[labels.index(pick)]["slug"]
    st.session_state["codex_model"] = slug
    minfo = next((m for m in models if m["slug"] == slug), {})
    if minfo.get("levels"):
        st.caption(f"Reasoning effort sent: {_codex_effort(minfo)} (slider "
                   f"{st.session_state.get('effort', 'high')}; this model supports "
                   f"{', '.join(minfo['levels'])})")
    st.caption("Runs through OpenAI's Codex CLI signed in with your ChatGPT account, so "
               "usage counts against your plan's Codex limits - no per-token cost. Personal "
               "use of your own subscription only. Each call is one `codex exec` in an "
               "empty read-only scratch folder; the figure critic attaches the image.")
    return "codex-backend" if (info.get("login") and slug) else ""


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
    if st.session_state.get("backend") == "codex":
        return call_codex(system, user_msg, max_tokens)
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


def paper_full_text(sig, max_chars=16000):
    col = load_collection()
    got = col.get(where={"doc_sig": sig}, include=["documents", "metadatas"])
    pairs = [(d, m) for d, m in zip(got["documents"], got["metadatas"])
             if m.get("type") != "figure"]
    pairs.sort(key=lambda x: x[1].get("page_start", 0))
    return "\n\n".join(d for d, m in pairs)[:max_chars]


# --------------------------------------------------------------------------
# The materials store
# --------------------------------------------------------------------------
MAT_FILE = ANSWERS_DIR / "materials.json"
MAT_ROLES = ["absorber", "passivation", "ETL", "HTL", "additive",
             "electrode", "interlayer", "encapsulation", "substrate",
             "other"]


def load_mats():
    import json as _json
    if MAT_FILE.exists():
        try:
            d = _json.loads(MAT_FILE.read_text(encoding="utf-8"))
            return {"papers": dict(d.get("papers", {})),
                    "entries": list(d.get("entries", []))}
        except Exception:
            pass
    return {"papers": {}, "entries": []}


def save_mats(store):
    import json as _json
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        tmp = MAT_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(store, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(MAT_FILE)
    except Exception:
        pass


MATERIALS_SYSTEM = """\
You extract a materials inventory from ONE perovskite / photovoltaics
research paper. Respond with ONLY a JSON array (no markdown fences, no
prose). Each entry:
{"material": short common name (e.g. "PEAI", "SnO2", "spiro-OMeTAD"),
 "formula": chemical formula/composition, or null,
 "role": one of "absorber"|"passivation"|"ETL"|"HTL"|"additive"|
         "electrode"|"interlayer"|"encapsulation"|"substrate"|"other",
 "device": device context (e.g. "p-i-n single junction",
           "2T perovskite-Si tandem"), or null,
 "deposition": how it was deposited/applied, or null,
 "properties": key measured properties with units, or null,
 "impact": the reported effect on device performance/stability, with
           numbers and before->after where stated, or null,
 "best_pce": best PCE %% of a device using it in THIS paper, or null}

Rules:
- Only materials actually used in this paper's OWN experiments/devices -
  never materials merely cited from other work.
- 3 to 12 entries; the absorber composition always counts as one entry.
- Copy numbers exactly; NEVER estimate or invent; use null when the
  paper does not state something.
- impact reflects what THIS paper reports - including negative or
  neutral results ("no significant change in FF").
- Keep material names as the paper's most common short form."""


def parse_entries(raw):
    """Best-effort JSON-array recovery from a model reply."""
    import json as _json
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    a, b = raw.find("["), raw.rfind("]")
    if a == -1 or b == -1 or b <= a:
        raise ValueError("no JSON array in reply")
    data = _json.loads(raw[a:b + 1])
    out = []
    for e in data:
        if not isinstance(e, dict) or not str(e.get("material", "")).strip():
            continue
        role = str(e.get("role", "other")).strip()
        if role not in MAT_ROLES:
            role = "other"
        out.append({
            "material": str(e["material"]).strip()[:80],
            "formula": (str(e["formula"]).strip()[:100]
                        if e.get("formula") else ""),
            "role": role,
            "device": (str(e["device"]).strip()[:80]
                       if e.get("device") else ""),
            "deposition": (str(e["deposition"]).strip()[:120]
                           if e.get("deposition") else ""),
            "properties": (str(e["properties"]).strip()[:300]
                           if e.get("properties") else ""),
            "impact": (str(e["impact"]).strip()[:400]
                       if e.get("impact") else ""),
            "best_pce": (float(e["best_pce"])
                         if isinstance(e.get("best_pce"), (int, float))
                         and 0 < float(e["best_pce"]) < 50 else None),
        })
    return out[:12]


def mat_key(name):
    """Light normalization key: case/space/hyphen-insensitive."""
    return re.sub(r"[\s\-_]+", "", str(name).lower())


FILE_YEAR = re.compile(r"_((?:19|20)\d{2})_")


def file_year(fname):
    m = FILE_YEAR.search(str(fname))
    return int(m.group(1)) if m else None


def _md_runs(par, text):
    """Render **bold**, *italic* and `code` inline markup as Word runs."""
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
    """Markdown -> real Word headings, lists, bold and tables."""
    from docx.shared import Pt
    lines = md.splitlines()
    i, in_code = 0, False
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            r = doc.add_paragraph().add_run(lines[i])
            r.font.name = "Consolas"
            r.font.size = Pt(9)
            i += 1
            continue
        if not stripped or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            i += 1
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            tbl = []
            while i < len(lines):
                s = lines[i].strip()
                if not (s.startswith("|") and s.endswith("|")):
                    break
                cells = [c.strip() for c in s.strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    tbl.append(cells)
                i += 1
            if tbl:
                ncols = max(len(r) for r in tbl)
                t = doc.add_table(rows=len(tbl), cols=ncols)
                try:
                    t.style = "Table Grid"
                except Exception:
                    pass
                for ri, row in enumerate(tbl):
                    for ci in range(ncols):
                        cpar = t.cell(ri, ci).paragraphs[0]
                        _md_runs(cpar, row[ci] if ci < len(row) else "")
                        if ri == 0:
                            for r_ in cpar.runs:
                                r_.bold = True
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            h = doc.add_heading("", level=min(len(m.group(1)), 4))
            _md_runs(h, m.group(2).strip())
            i += 1
            continue
        m = re.match(r"^[-*+]\s+(.*)", stripped)
        if m:
            _md_runs(doc.add_paragraph(style="List Bullet"), m.group(1))
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)", stripped)
        if m:
            _md_runs(doc.add_paragraph(style="List Number"), m.group(2)
                     if m.lastindex and m.lastindex >= 2 else m.group(1))
            i += 1
            continue
        _md_runs(doc.add_paragraph(), stripped)
        i += 1


def save_report(name, text):
    """Save a report to the shared answers folder as .md and .docx."""
    ANSWERS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", name)[:60].strip() or "report"
    (ANSWERS_DIR / f"Materials_{safe}_{stamp}.md").write_text(
        f"# Material Library: {name}\n\n{text}\n", encoding="utf-8")
    try:
        from docx import Document
        doc = Document()
        doc.add_heading(f"Material Library: {name}", level=1)
        md_to_docx(doc, text)
        doc.save(ANSWERS_DIR / f"Materials_{safe}_{stamp}.docx")
    except Exception:
        pass


DESIGN_SYSTEM = """\
You are a materials-design assistant for perovskite photovoltaics,
reasoning ONLY over the user's extracted literature library. Each entry:
material | role | device | deposition | properties | reported impact |
best PCE | [source paper].

Given the design question, produce (markdown):
1. **Evidence summary** - what the library says about this design space:
   the materials tried, grouped by approach, the reported outcomes, and
   how many papers back each. Cite source paper titles in brackets.
2. **Candidate recommendations** - a ranked shortlist (3-7). For each:
   the rationale from the entries, the reported effects with numbers,
   evidence strength (papers, consistency), and risks or conflicting
   reports.
3. **Gaps & opportunities** - approaches thinly or never explored in
   these entries: candidate white space for novel work and IP.
4. **Suggested experiments** - a short screening plan to validate the
   top candidates in the asker's device context.

Rules:
- Every claim traces to entries; cite the source paper title(s) in
  brackets. NEVER invent materials, values, or papers.
- Where the library is silent, say so explicitly - silence IS a finding
  (white space), not something to fill with general knowledge.
- Reported values are per-paper results in that specific stack, not
  universal constants; keep that framing.
- End with one line: "Based on N library entries from M papers." """


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Material Library - by GrapheAI",
                   page_icon="🧱", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:wght@600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"], .stMarkdown, p, li {
    font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
    font-size: 16.5px;
    color: #E6EAF0;
}
.stApp { background: #12161C; }
.ml-header { border-bottom: 3px solid #FF6B3D; padding-bottom: 12px;
             margin-bottom: 10px; }
.ml-title  { font-family: 'IBM Plex Serif', Georgia, serif;
             font-size: 2.6rem; font-weight: 600; color: #F4F6F9;
             margin: 0; letter-spacing: -0.5px; }
.ml-sub    { font-size: 0.82rem; color: #8E99A8; margin-top: 6px;
             text-transform: uppercase; letter-spacing: 2px; }
.ml-sub b  { color: #FF8A5C; font-weight: 600; }
.stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #242D38;
                                    gap: 2px; }
.stTabs [data-baseweb="tab"] {
    font-size: 1.05rem; font-weight: 500; padding: 12px 18px;
    color: #8E99A8;
}
.stTabs [data-baseweb="tab"]:hover { color: #FFB08F; }
.stTabs [aria-selected="true"] { color: #FF8A5C; font-weight: 600; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #FF6B3D;
                                         height: 3px; }
section[data-testid="stSidebar"] { background: #171D25;
    border-right: 1px solid #242D38; }
section[data-testid="stSidebar"] * { font-size: 15px; }
section[data-testid="stSidebar"] h1 { font-family: 'IBM Plex Serif', serif;
                                      font-size: 1.4rem; color: #F4F6F9; }
h2 { font-family: 'IBM Plex Serif', Georgia, serif; font-weight: 600;
     color: #DFE5EC; }
h3 { color: #C3CBD6; }
[data-testid="stForm"] {
    background: #1A212B; border: 1px solid #263140; border-radius: 14px;
    padding: 1.1rem 1.3rem .9rem; box-shadow: 0 2px 8px rgba(0,0,0,.35);
}
details { border: 1px solid #263140; border-radius: 12px;
          background: #1A212B; }
[data-testid="stExpander"] summary { font-size: 1.02rem; font-weight: 500; }
[data-testid="stDataFrame"] { border: 1px solid #263140;
                              border-radius: 12px; }
[data-testid="stMetric"] {
    background: #1A212B; border: 1px solid #263140; border-radius: 12px;
    padding: .7rem .95rem; box-shadow: 0 2px 6px rgba(0,0,0,.3);
}
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace;
                                color: #FF8A5C; }
[data-testid="stMetricLabel"] { color: #8E99A8;
                                text-transform: uppercase;
                                letter-spacing: 1px; font-size: .78rem; }
.stButton>button, .stDownloadButton>button, .stFormSubmitButton>button {
    font-size: 1.0rem; font-weight: 600; border-radius: 10px;
    padding: 0.5rem 1.25rem;
}
.stTextArea textarea, .stTextInput input { font-size: 1.02rem;
                                           border-radius: 10px; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='ml-header'>"
    "<p class='ml-title'>🧱 Material Library</p>"
    "<p class='ml-sub'>by <b>GrapheAI</b> · developed by "
    "<b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("🧱 Material Library")
    st.caption("by GrapheAI · Dr. Anurag Krishna")
    backend_label = st.radio(
        "Claude access", ["API key (pay per use)",
                          "Claude Max subscription (needs Claude Code)",
                          "OpenAI API (ChatGPT models, pay per use)",
                          "ChatGPT subscription (Plus/Pro via Codex CLI)"])
    st.session_state["backend"] = ("max" if "Max" in backend_label
                                    else "codex" if "Codex" in backend_label
                                    else "openai" if "OpenAI" in backend_label
                                    else "api")
    if st.session_state["backend"] == "codex":
        api_key = render_codex_sidebar()
    elif st.session_state["backend"] == "openai":
        api_key = render_openai_sidebar()
    elif st.session_state["backend"] == "api":
        api_key = st.text_input("Anthropic API key", type="password")
    else:
        # Sentinel so features unlock; never sent anywhere in Max mode.
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
        st.error("Index not found - run this app from the PaperRag "
                 "folder (chroma_db must exist).")
    _store = load_mats()
    st.caption(f"Library: {len(_store['entries'])} material entries from "
               f"{sum(1 for p in _store['papers'].values() if p.get('status') == 'done')} papers")
    u = st.session_state.get("usage")
    if u:
        line = (f"Session: {u['calls']} calls - "
                f"{u['in']:,} in / {u['out']:,} out tokens")
        if st.session_state.get("backend") not in ("max", "codex") and u.get("cost"):
            line += f"  ·  ≈ ${u['cost']:.2f}"
        st.caption(line)

st.caption("Extracts every material - absorbers, passivation, transport "
           "layers, electrodes, additives - from the papers in your corpus "
           "into a structured, exportable library. Incremental and "
           "resumable: run it in evening batches.")

tab_build, tab_browse, tab_ins, tab_design = st.tabs(
    ["🏗️ Build library", "🔎 Browse & export", "📈 Insights",
     "🧪 Design assistant"])

# ------------------------------ BUILD -------------------------------------
with tab_build:
    if not index_ok:
        st.info("No corpus found - run from the PaperRag folder.")
    else:
        store = load_mats()
        papers = list_papers(n_chunks)
        srcs = sorted({p["source"] for p in papers if p["source"]})
        non_paper = {"proposals", "evaluations", "proposal_docs"}
        default_srcs = [s for s in srcs if s not in non_paper]
        sel_srcs = st.multiselect(
            "Source folders to extract from", srcs,
            default=default_srcs, key="mat_srcs",
            help="Proposals/evaluations are excluded by default - this "
                 "library is about the scientific papers.")
        todo = [p for p in papers
                if (p["source"] in sel_srcs or not srcs)
                and store["papers"].get(p["sig"], {}).get("status")
                != "done"]
        done_n = sum(1 for p in papers
                     if store["papers"].get(p["sig"], {}).get("status")
                     == "done")
        m1, m2, m3 = st.columns(3)
        m1.metric("Papers in scope", len([p for p in papers
                                          if p["source"] in sel_srcs
                                          or not srcs]))
        m2.metric("Already extracted", done_n)
        m3.metric("Remaining", len(todo))

        bc1, bc2 = st.columns(2)
        with bc1:
            mat_model_label = st.selectbox(
                "Extraction model", list(MODELS.keys()), index=0,
                key="mat_model",
                help="Haiku is accurate for structured extraction and "
                     "cheap enough to run the whole corpus.")
        with bc2:
            batch_n = st.selectbox("Papers this run",
                                   [25, 50, 100, 200, 500, "All remaining"],
                                   index=1, key="mat_batch")
        n_run = (len(todo) if batch_n == "All remaining"
                 else min(int(batch_n), len(todo)))
        est_tokens = n_run * 5000
        if st.session_state.get("backend") == "max":
            st.caption(f"~{n_run} Claude calls on your Max plan. Large "
                       "batches can hit the usage window - progress is "
                       "saved after every paper, so just resume later.")
        else:
            p_in, p_out = PRICES[MODELS[mat_model_label]]
            st.caption(f"Rough cost: ~${est_tokens / 1e6 * p_in + n_run * 700 / 1e6 * p_out:.2f} "
                       f"for {n_run} papers on {mat_model_label}.")

        if st.button(f"🏗️ Extract {n_run} paper(s)", type="primary",
                     key="mat_go", disabled=n_run == 0):
            if (st.session_state.get("backend") not in ("max", "codex")
                    and not api_key.strip()):
                st.error("Needs the API key (sidebar).")
            else:
                bar = st.progress(0.0, text="Starting...")
                counts = {"ok": 0, "err": 0, "entries": 0}
                stop_reason = ""
                for i, p in enumerate(todo[:n_run], start=1):
                    bar.progress(i / n_run,
                                 text=f"{i}/{n_run}: {p['title'][:55]}")
                    try:
                        text = paper_full_text(p["sig"])
                        if len(text) < 500:
                            raise ValueError("too little text in index")
                        raw = call_claude(
                            api_key.strip(), MATERIALS_SYSTEM,
                            f"PAPER: {p['title']}\nFILE: {p['file']}\n\n"
                            f"{text}",
                            MODELS[mat_model_label], max_tokens=2000)
                        entries = parse_entries(raw)
                        for e in entries:
                            e.update({"sig": p["sig"],
                                      "paper": p["title"],
                                      "file": p["file"],
                                      "source": p["source"],
                                      "key": mat_key(e["material"])})
                        # replace any previous entries for this paper
                        store["entries"] = [e for e in store["entries"]
                                            if e.get("sig") != p["sig"]]
                        store["entries"].extend(entries)
                        store["papers"][p["sig"]] = {
                            "title": p["title"], "file": p["file"],
                            "status": "done", "n": len(entries),
                            "when": datetime.date.today().isoformat()}
                        counts["ok"] += 1
                        counts["entries"] += len(entries)
                    except Exception as e:
                        msg = str(e)
                        store["papers"][p["sig"]] = {
                            "title": p["title"], "file": p["file"],
                            "status": "error", "err": msg[:200],
                            "when": datetime.date.today().isoformat()}
                        counts["err"] += 1
                        low = msg.lower()
                        if ("usage" in low or "limit" in low
                                or "rate" in low or "credit" in low):
                            stop_reason = msg
                            save_mats(store)
                            break
                    save_mats(store)
                bar.empty()
                if stop_reason:
                    st.warning(f"Stopped early ({counts['ok']} done): "
                               f"{stop_reason[:200]} - progress is saved, "
                               "resume any time.")
                st.success(f"Extracted {counts['ok']} paper(s) -> "
                           f"{counts['entries']} material entries "
                           f"({counts['err']} errors). Library total: "
                           f"{len(store['entries'])} entries.")

        errs = [(s, p) for s, p in store["papers"].items()
                if p.get("status") == "error"]
        if errs:
            with st.expander(f"⚠️ {len(errs)} paper(s) failed - retryable"):
                for s, p in errs[:20]:
                    st.caption(f"{p.get('file', s)}: {p.get('err', '?')}")
                if st.button("Retry failed papers next run",
                             key="mat_retry",
                             help="Clears their error status so the next "
                                  "extraction run includes them again."):
                    for s, _p in errs:
                        store["papers"].pop(s, None)
                    save_mats(store)
                    st.rerun()

# ------------------------------ BROWSE ------------------------------------
with tab_browse:
    store = load_mats()
    if not store["entries"]:
        st.info("The library is empty - run an extraction batch in "
                "🏗️ Build library first.")
    else:
        import pandas as _pd
        mdf = _pd.DataFrame(store["entries"])
        f1, f2, f3 = st.columns([2, 2, 2])
        with f1:
            f_roles = st.multiselect("Role", MAT_ROLES, key="mb_roles")
        with f2:
            f_text = st.text_input("Search (material/formula/paper)",
                                   key="mb_text")
        with f3:
            f_dev = st.text_input("Device contains (e.g. tandem, p-i-n)",
                                  key="mb_dev")
        mview = mdf.copy()
        if f_roles:
            mview = mview[mview["role"].isin(f_roles)]
        if f_text.strip():
            s = f_text.strip().lower()
            hay = (mview["material"].fillna("") + " "
                   + mview["formula"].fillna("") + " "
                   + mview["paper"].fillna("")).str.lower()
            mview = mview[hay.str.contains(s, regex=False)]
        if f_dev.strip():
            mview = mview[mview["device"].fillna("").str.lower()
                          .str.contains(f_dev.strip().lower(),
                                        regex=False)]
        st.caption(f"{len(mview)} entries · "
                   f"{mview['key'].nunique()} distinct materials · "
                   f"{mview['sig'].nunique()} papers")
        show = mview[["material", "formula", "role", "device",
                      "deposition", "properties", "impact", "best_pce",
                      "paper"]].rename(columns={
                          "material": "Material", "formula": "Formula",
                          "role": "Role", "device": "Device",
                          "deposition": "Deposition",
                          "properties": "Properties", "impact": "Impact",
                          "best_pce": "Best PCE %", "paper": "Paper"})
        st.dataframe(show, use_container_width=True, height=420)
        d1, d2 = st.columns(2)
        d1.download_button(
            "⬇️ Filtered view as CSV",
            data=show.to_csv(index=False).encode("utf-8-sig"),
            file_name="material_library.csv", mime="text/csv",
            use_container_width=True)
        d2.download_button(
            "⬇️ Whole library as CSV",
            data=mdf.to_csv(index=False).encode("utf-8-sig"),
            file_name="material_library_full.csv", mime="text/csv",
            use_container_width=True)

        st.markdown("---")
        st.markdown("**🗂️ Material dossier** - every use of one material "
                    "across the corpus.")
        freq = (mview.groupby("key")
                .agg(name=("material", "first"), n=("material", "size"))
                .sort_values("n", ascending=False))
        opts = [f"{r['name']}  ({r['n']} entries)"
                for _, r in freq.iterrows()]
        pick = st.selectbox("Material", opts, key="mb_pick") if opts else None
        if pick:
            pkey = freq.index[opts.index(pick)]
            sub = mdf[mdf["key"] == pkey]
            st.caption(f"{len(sub)} entries in {sub['sig'].nunique()} "
                       "paper(s)")
            for _, r in sub.iterrows():
                bits = [f"**{r['material']}**", r["role"]]
                if r.get("device"):
                    bits.append(r["device"])
                if r.get("best_pce"):
                    bits.append(f"best PCE {r['best_pce']}%")
                st.markdown(" · ".join(str(b) for b in bits if b))
                if r.get("properties"):
                    st.caption(f"Properties: {r['properties']}")
                if r.get("impact"):
                    st.caption(f"Impact: {r['impact']}")
                st.caption(f"→ {r['paper']}  ({r['file']})")
                st.markdown("")

# ------------------------------ INSIGHTS ----------------------------------
with tab_ins:
    store = load_mats()
    if not store["entries"]:
        st.info("The library is empty - run an extraction batch first.")
    else:
        import pandas as _pd
        try:
            import altair as alt
        except Exception:
            alt = None
        odf = _pd.DataFrame(store["entries"])
        odf["year"] = odf["file"].apply(file_year)

        o1, o2, o3, o4 = st.columns(4)
        o1.metric("Entries", len(odf))
        o2.metric("Distinct materials", odf["key"].nunique())
        o3.metric("Papers covered", odf["sig"].nunique())
        pce = odf["best_pce"].dropna()
        o4.metric("Best PCE seen",
                  f"{pce.max():.1f}%" if not pce.empty else "-")

        oc1, oc2 = st.columns(2)
        with oc1:
            st.markdown("**Entries by role**")
            rc = (odf["role"].value_counts().rename_axis("role")
                  .reset_index(name="entries"))
            if alt is not None:
                st.altair_chart(alt.Chart(rc).mark_bar().encode(
                    x="entries:Q", y=alt.Y("role:N", sort="-x"),
                    tooltip=["role", "entries"]),
                    use_container_width=True)
            else:
                st.bar_chart(rc.set_index("role"))
        with oc2:
            st.markdown("**Most-used materials (non-absorber)**")
            top = (odf[odf["role"] != "absorber"].groupby("key")
                   .agg(name=("material", "first"),
                        papers=("sig", "nunique"))
                   .sort_values("papers", ascending=False).head(15)
                   .reset_index(drop=True))
            if alt is not None and not top.empty:
                st.altair_chart(alt.Chart(top).mark_bar().encode(
                    x="papers:Q", y=alt.Y("name:N", sort="-x",
                                          title="material"),
                    tooltip=["name", "papers"]),
                    use_container_width=True)
            elif not top.empty:
                st.bar_chart(top.set_index("name"))

        st.markdown("---")
        st.markdown("**🔬 Role deep-dive** - one role, every material "
                    "tried, with reported outcomes.")
        dd1, dd2 = st.columns([1, 2])
        with dd1:
            dd_role = st.selectbox("Role", MAT_ROLES,
                                   index=MAT_ROLES.index("passivation"),
                                   key="ins_role")
        with dd2:
            dd_dev = st.text_input("Device contains (optional)",
                                   key="ins_dev",
                                   placeholder="e.g. tandem, p-i-n")
        dsub = odf[odf["role"] == dd_role]
        if dd_dev.strip():
            dsub = dsub[dsub["device"].fillna("").str.lower()
                        .str.contains(dd_dev.strip().lower(), regex=False)]
        if dsub.empty:
            st.caption("No entries for this role/device yet.")
        else:
            def _first_impact(s):
                for v in s:
                    if str(v).strip():
                        return str(v)[:160]
                return ""
            agg = (dsub.groupby("key")
                   .agg(Material=("material", "first"),
                        Papers=("sig", "nunique"),
                        Entries=("material", "size"),
                        Best_PCE=("best_pce", "max"),
                        Example_impact=("impact", _first_impact))
                   .sort_values(["Papers", "Entries"], ascending=False)
                   .reset_index(drop=True)
                   .rename(columns={"Best_PCE": "Best PCE %",
                                    "Example_impact": "Example impact"}))
            st.dataframe(agg, use_container_width=True, height=340)
            if alt is not None:
                pcs = dsub.dropna(subset=["best_pce"])
                if not pcs.empty:
                    st.altair_chart(alt.Chart(pcs).mark_point(
                        size=90, filled=True, opacity=0.75).encode(
                        x=alt.X("material:N", sort="-y",
                                title=f"{dd_role} material"),
                        y=alt.Y("best_pce:Q",
                                title="Best PCE % (that paper)",
                                scale=alt.Scale(zero=False)),
                        color=alt.Color("device:N", title="Device"),
                        tooltip=["material", "best_pce", "device",
                                 "paper"]),
                        use_container_width=True)

        st.markdown("---")
        st.markdown("**📅 Adoption over time** - papers per year using "
                    "the top materials of this role (year from your "
                    "filename convention).")
        ysub = dsub.dropna(subset=["year"])
        if ysub.empty:
            st.caption("No dated entries (files need the "
                       "'1001_topic_2025_Title.pdf' pattern).")
        else:
            topk = (ysub.groupby("key")["sig"].nunique()
                    .sort_values(ascending=False).head(8).index)
            tsub = ysub[ysub["key"].isin(topk)]
            tl = (tsub.groupby(["year", "key"])
                  .agg(name=("material", "first"),
                       papers=("sig", "nunique")).reset_index())
            if alt is not None and not tl.empty:
                st.altair_chart(alt.Chart(tl).mark_line(
                    point=True).encode(
                    x=alt.X("year:O", title="Year"),
                    y=alt.Y("papers:Q", title="Papers"),
                    color=alt.Color("name:N", title="Material"),
                    tooltip=["year", "name", "papers"]),
                    use_container_width=True)

        st.markdown("---")
        st.markdown(f"**🧬 Stack pairing** - which absorbers the top "
                    f"{dd_role} materials were used with.")
        absb = odf[odf["role"] == "absorber"][["sig", "material"]]
        absb = absb.rename(columns={"material": "absorber"})
        pair = dsub.merge(absb, on="sig", how="inner")
        if pair.empty:
            st.caption("No absorber pairings yet.")
        else:
            pc = (pair.groupby(["material", "absorber"]).size()
                  .reset_index(name="papers")
                  .sort_values("papers", ascending=False).head(15)
                  .rename(columns={"material": f"{dd_role.capitalize()} "
                                               "material",
                                   "absorber": "Absorber"}))
            st.dataframe(pc, use_container_width=True, height=280,
                         hide_index=True)

# ------------------------------ DESIGN ------------------------------------
with tab_design:
    store = load_mats()
    if not store["entries"]:
        st.info("The library is empty - run an extraction batch first.")
    else:
        import pandas as _pd
        ddf = _pd.DataFrame(store["entries"])
        st.markdown("**Design assistant** - Claude reasons over your "
                    "extracted library (never general knowledge): "
                    "evidence-backed candidates, conflicting reports, and "
                    "the white space the literature has not explored. "
                    "Every claim cites its source papers.")
        dq = st.text_area(
            "Design question", height=110, key="dq",
            placeholder="e.g. Suggest passivation strategies for p-i-n "
                        "2T perovskite-Si tandems targeting Voc > 2.0 V; "
                        "we deposit by slot-die coating.")
        de1, de2, de3 = st.columns([2, 1, 1])
        with de1:
            d_roles = st.multiselect("Roles to consider", MAT_ROLES,
                                     default=["passivation", "additive",
                                              "interlayer"],
                                     key="d_roles")
        with de2:
            d_dev = st.text_input("Device contains", key="d_dev",
                                  placeholder="e.g. tandem")
        with de3:
            d_model_label = st.selectbox("Model", list(MODELS.keys()),
                                         index=3, key="d_model",
                                         help="Opus/Frontier recommended - "
                                              "this is synthesis work.")
        if st.button("🧪 Ask the library", type="primary", key="d_go",
                     disabled=not dq.strip()):
            can_call = (st.session_state.get("backend") == "max"
                        or bool(api_key.strip()))
            if not can_call:
                st.error("Needs the API key (sidebar).")
            else:
                sel = ddf[ddf["role"].isin(d_roles)] if d_roles else ddf
                if d_dev.strip():
                    dv = sel[sel["device"].fillna("").str.lower()
                             .str.contains(d_dev.strip().lower(),
                                           regex=False)]
                    # keep device-matched first but don't drop everything
                    sel = dv if len(dv) >= 10 else sel
                # rank: question-keyword hits first, then most-used
                words = [w for w in re.findall(r"[a-zA-Z]{4,}",
                                               dq.lower())][:12]
                def _score(r):
                    hay = " ".join(str(r.get(k, "")) for k in
                                   ("material", "formula", "device",
                                    "impact", "properties")).lower()
                    return sum(1 for w in words if w in hay)
                sel = sel.copy()
                sel["_score"] = sel.apply(_score, axis=1)
                use = (sel.sort_values("_score", ascending=False)
                       .head(150))
                lines = []
                for _, r in use.iterrows():
                    bits = [f"{r['material']}", f"role: {r['role']}"]
                    if r.get("formula"):
                        bits.append(f"formula: {r['formula']}")
                    if r.get("device"):
                        bits.append(f"device: {r['device']}")
                    if r.get("deposition"):
                        bits.append(f"deposition: {r['deposition']}")
                    if r.get("properties"):
                        bits.append(f"props: {r['properties']}")
                    if r.get("impact"):
                        bits.append(f"impact: {r['impact']}")
                    if r.get("best_pce"):
                        bits.append(f"best PCE {r['best_pce']}%")
                    bits.append(f"[{r['paper']}]")
                    lines.append(" | ".join(str(b) for b in bits))
                umsg = (f"DESIGN QUESTION: {dq.strip()}\n\n"
                        f"LIBRARY ENTRIES ({len(use)} of "
                        f"{len(ddf)} total, {use['sig'].nunique()} "
                        f"papers):\n" + "\n".join(lines))
                with st.spinner(f"{d_model_label} is reasoning over "
                                f"{len(use)} entries..."):
                    try:
                        out = call_claude(api_key.strip(), DESIGN_SYSTEM,
                                          umsg, MODELS[d_model_label],
                                          max_tokens=8000)
                    except Exception as e:
                        st.error(f"Claude error: {e}")
                        out = None
                if out:
                    st.session_state["last_design"] = out
                    save_report(dq.strip()[:50] or "design", out)
        if st.session_state.get("last_design"):
            st.markdown("---")
            st.markdown(st.session_state["last_design"])
            st.download_button(
                "⬇️ As Markdown",
                data=st.session_state["last_design"].encode("utf-8"),
                file_name="material_design.md", mime="text/markdown",
                key="d_dl")
            st.caption("Also saved to the answers folder as formatted "
                       ".docx + .md.")
