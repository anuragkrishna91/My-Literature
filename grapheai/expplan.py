"""
Experiment Planner - by GrapheAI. Define a parameter space, log lab
results, and get statistically-guided suggestions for the next
experiments (Latin-hypercube start, Gaussian-process Bayesian
optimisation once results accumulate).

Run with:
    streamlit run expplan.py --server.port 8509

Optional (better suggestions):   pip install scikit-learn

Shares with the rest of GrapheAI:
  - answers/experiments/        campaign files (parameters + runs)
  - answers/spend.json          monthly API spend + budget
  - the Claude backend          (only for the Interpret tab)
"""

import datetime
import json
import re
from pathlib import Path

import sys
import streamlit as st

ANSWERS_DIR = Path("answers")
EXP_DIR = ANSWERS_DIR / "experiments"

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
# Campaign store
# --------------------------------------------------------------------------
def _safe(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60]


def list_campaigns():
    if not EXP_DIR.exists():
        return []
    return sorted(p.stem for p in EXP_DIR.glob("*.json"))


def load_campaign(name):
    p = EXP_DIR / f"{_safe(name)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_campaign(name, c):
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    p = EXP_DIR / f"{_safe(name)}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------
# Suggestion engine (pure functions, unit-tested offline)
# --------------------------------------------------------------------------
def lhs_sample(params, n, rng):
    """Latin hypercube over numeric params; balanced random for
    categorical. Returns a list of dicts."""
    import numpy as np
    cols = {}
    for p in params:
        if p["type"] == "num":
            lo, hi = float(p["min"]), float(p["max"])
            edges = np.linspace(0, 1, n + 1)
            pts = edges[:-1] + rng.random(n) * (1.0 / n)
            rng.shuffle(pts)
            cols[p["name"]] = lo + pts * (hi - lo)
        else:
            levels = p["levels"]
            reps = [levels[i % len(levels)] for i in range(n)]
            rng.shuffle(reps)
            cols[p["name"]] = reps
    out = []
    for i in range(n):
        row = {}
        for p in params:
            v = cols[p["name"]][i]
            row[p["name"]] = (round(float(v), 4)
                              if p["type"] == "num" else v)
        out.append(row)
    return out


def _encode(params, rows):
    """Rows -> normalized feature matrix (numeric scaled to [0,1],
    categoricals one-hot)."""
    import numpy as np
    feats = []
    for p in params:
        if p["type"] == "num":
            lo, hi = float(p["min"]), float(p["max"])
            span = (hi - lo) or 1.0
            feats.append(np.array(
                [(float(r[p["name"]]) - lo) / span for r in rows]
            ).reshape(-1, 1))
        else:
            for lv in p["levels"]:
                feats.append(np.array(
                    [1.0 if r[p["name"]] == lv else 0.0 for r in rows]
                ).reshape(-1, 1))
    return np.hstack(feats)


def replicate_noise(params, done_rows):
    """Measurement noise from replicate runs (identical settings,
    numerics rounded to 4 significant-ish decimals). Returns
    (n_replicate_groups, pooled_std) - (0, None) without replicates."""
    import numpy as np
    groups = {}
    for r in done_rows:
        key = []
        for p in params:
            v = r.get(p["name"])
            key.append(round(float(v), 4) if p["type"] == "num"
                       else str(v))
        groups.setdefault(tuple(key), []).append(float(r["_y"]))
    sq, dof = 0.0, 0
    ngrp = 0
    for vals in groups.values():
        if len(vals) >= 2:
            ngrp += 1
            m = sum(vals) / len(vals)
            sq += sum((v - m) ** 2 for v in vals)
            dof += len(vals) - 1
    if dof == 0:
        return 0, None
    return ngrp, float(np.sqrt(sq / dof))


def gp_suggest(params, done_rows, objective_goal, n, xi, rng,
               n_candidates=2000):
    """Gaussian-process expected-improvement suggestions.
    done_rows: dicts with param values + '_y'. Returns
    (suggestions, engine_name); falls back to LHS if the GP is
    unavailable or degenerate."""
    import numpy as np
    try:
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import (Matern,
                                                      WhiteKernel)
    except ImportError:
        return (lhs_sample(params, n, rng),
                "Latin hypercube (install scikit-learn for Bayesian "
                "suggestions)")
    sign = 1.0 if objective_goal == "max" else -1.0
    y = np.array([sign * float(r["_y"]) for r in done_rows])
    X = _encode(params, done_rows)
    # replicates pin the noise level; otherwise the GP estimates it
    ngrp, pooled = replicate_noise(params, done_rows)
    noise_note = ""
    if pooled is not None and pooled > 0:
        std_y = float(np.std(y)) or 1.0
        alpha = max((pooled / std_y) ** 2, 1e-6)
        kernel = Matern(nu=2.5, length_scale=0.3,
                        length_scale_bounds=(0.02, 5.0))
        noise_note = (f" · noise pinned by {ngrp} replicate group(s), "
                      f"sigma={pooled:.3g}")
    else:
        alpha = 1e-10
        kernel = (Matern(nu=2.5, length_scale=0.3,
                         length_scale_bounds=(0.02, 5.0))
                  + WhiteKernel(1e-4, (1e-8, 1e-1)))
    gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True,
                                  alpha=alpha,
                                  n_restarts_optimizer=2,
                                  random_state=0)
    try:
        gp.fit(X, y)
    except Exception:
        return lhs_sample(params, n, rng), "Latin hypercube (GP failed)"
    cands = lhs_sample(params, n_candidates, rng)
    Xc = _encode(params, cands)
    mu, sd = gp.predict(Xc, return_std=True)
    best = y.max()
    sd = np.maximum(sd, 1e-9)
    z = (mu - best - xi) / sd
    from math import erf, sqrt

    def _phi(v):
        return float(np.exp(-0.5 * v * v) / np.sqrt(2 * np.pi))

    def _Phi(v):
        return 0.5 * (1.0 + erf(v / sqrt(2.0)))

    ei = np.array([(mu[i] - best - xi) * _Phi(z[i]) + sd[i] * _phi(z[i])
                   for i in range(len(cands))])
    order = np.argsort(-ei)
    picked, picked_X = [], []
    for idx in order:
        x = Xc[idx]
        # keep suggestions apart from each other
        if any(np.linalg.norm(x - px) < 0.12 for px in picked_X):
            continue
        c = dict(cands[idx])
        c["_pred"] = round(float(sign * mu[idx]), 4)
        c["_uncertainty"] = round(float(sd[idx]), 4)
        picked.append(c)
        picked_X.append(x)
        if len(picked) >= n:
            break
    return picked, "Gaussian process + expected improvement" + noise_note


INTERPRET_SYSTEM = """\
You analyse a lab optimisation campaign for an experimental scientist.
You get the parameter space, the objective, and every logged run.
Write in markdown: ## What drives the objective (parameter-by-parameter,
citing actual run values), ## Recommended region (concrete ranges/levels
to focus on next), ## Cautions (confounds, sparse regions, whether the
apparent optimum could be noise - state group sizes). Use ONLY the runs
given; never invent measurements. If the data is too sparse to
conclude something, say so."""


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Experiment Planner - by GrapheAI",
                   page_icon="🧪", layout="wide")

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
    "<p class='an-title'>🧪 Experiment Planner</p>"
    "<p class='an-sub'>design of experiments · by <b>GrapheAI</b> · "
    "developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("🧪 Experiment Planner")
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
        import sklearn  # noqa: F401
        st.success("Bayesian engine ready (scikit-learn)")
    except ImportError:
        st.warning("scikit-learn missing - suggestions fall back to "
                   "space-filling.\n\nFor Bayesian mode:\n"
                   "`/opt/miniconda3/bin/pip install scikit-learn`")
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
    import altair as alt
except Exception:
    alt = None

with st.expander("❓ How the planner works"):
    st.markdown(
        "**🎯 Campaign** - define the knobs (e.g. anneal T 80-150 °C, "
        "additive choice, concentration 0-5 mol%) and the objective "
        "(e.g. maximise PCE).\n\n"
        "**🧾 Log** - enter each experiment's settings + result, or "
        "import a CSV of past runs.\n\n"
        "**🤖 Suggest** - with few results you get *space-filling* "
        "(Latin hypercube) suggestions to map the terrain; from 5 "
        "results a *Gaussian process* proposes the experiments with "
        "the highest expected improvement. The explore-exploit slider "
        "sets how adventurous it is.\n\n"
        "**📈 Progress / 🧠 Interpret** - best-so-far curve, per-"
        "parameter scatter, and a Claude read-out of what drives your "
        "objective.\n\n"
        "*This is the decision loop of a self-driving lab, with you as "
        "the robot.*")

tab_camp, tab_log, tab_sugg, tab_prog, tab_int = st.tabs(
    ["🎯 Campaign", "🧾 Log", "🤖 Suggest", "📈 Progress",
     "🧠 Interpret"])

names = list_campaigns()
active = st.session_state.get("active_campaign")
if active not in names:
    active = names[0] if names else None

# ------------------------------ CAMPAIGN ----------------------------------
with tab_camp:
    c1, c2 = st.columns([1, 1])
    with c1:
        if names:
            pick = st.selectbox("Active campaign", names,
                                index=names.index(active)
                                if active in names else 0,
                                key="camp_pick")
            st.session_state["active_campaign"] = pick
            active = pick
            if st.button("🗑️ Delete this campaign", key="camp_del"):
                (EXP_DIR / f"{_safe(active)}.json").unlink(
                    missing_ok=True)
                st.session_state.pop("active_campaign", None)
                st.rerun()
        else:
            st.info("No campaigns yet - create one on the right.")
    with c2:
        with st.form("camp_new"):
            st.markdown("**New campaign**")
            nc_name = st.text_input("Name",
                                    placeholder="e.g. FAPbI3 additive "
                                                "screen")
            nc_obj = st.text_input("Objective (what you measure)",
                                   value="PCE (%)")
            nc_goal = st.radio("Goal", ["max", "min"], horizontal=True)
            if st.form_submit_button("➕ Create") and nc_name.strip():
                if load_campaign(nc_name) is not None:
                    st.warning("That name already exists.")
                else:
                    save_campaign(nc_name, {
                        "objective": {"name": nc_obj.strip() or "y",
                                      "goal": nc_goal},
                        "params": [], "runs": []})
                    st.session_state["active_campaign"] = \
                        _safe(nc_name)
                    st.rerun()

    if active:
        camp = load_campaign(active)
        st.markdown(f"### Parameter space — *{active}*")
        st.caption("type `num` needs min & max; type `cat` needs "
                   "comma-separated levels.")
        pdf = pd.DataFrame(camp["params"]) if camp["params"] else \
            pd.DataFrame([{"name": "", "type": "num", "min": 0.0,
                           "max": 1.0, "levels": ""}])
        if "levels" in pdf.columns:
            pdf["levels"] = pdf["levels"].apply(
                lambda v: ", ".join(v) if isinstance(v, list) else v)
        for col in ("name", "type", "min", "max", "levels"):
            if col not in pdf.columns:
                pdf[col] = "" if col in ("name", "levels") else 0.0
        pdf = pdf[["name", "type", "min", "max", "levels"]]
        edited = st.data_editor(
            pdf, num_rows="dynamic", hide_index=True,
            use_container_width=True, key="camp_ped",
            column_config={"type": st.column_config.SelectboxColumn(
                options=["num", "cat"])})
        if st.button("💾 Save parameter space", key="camp_psave"):
            new_params, problems = [], []
            for _, r in edited.iterrows():
                nm = str(r.get("name") or "").strip()
                if not nm:
                    continue
                if r["type"] == "num":
                    try:
                        lo, hi = float(r["min"]), float(r["max"])
                        assert hi > lo
                    except Exception:
                        problems.append(f"{nm}: need min < max")
                        continue
                    new_params.append({"name": nm, "type": "num",
                                       "min": lo, "max": hi})
                else:
                    levels = [x.strip() for x in
                              str(r.get("levels") or "").split(",")
                              if x.strip()]
                    if len(levels) < 2:
                        problems.append(f"{nm}: need ≥2 levels")
                        continue
                    new_params.append({"name": nm, "type": "cat",
                                       "levels": levels})
            for pb in problems:
                st.warning(pb)
            if new_params:
                camp["params"] = new_params
                save_campaign(active, camp)
                st.success(f"Saved {len(new_params)} parameter(s).")
            elif not problems:
                st.warning("No valid parameters.")

# ------------------------------ helpers for runs --------------------------
def runs_df(camp):
    if not camp["runs"]:
        return pd.DataFrame()
    return pd.DataFrame(camp["runs"])


# ------------------------------ LOG ---------------------------------------
with tab_log:
    if not active:
        st.info("Create a campaign first.")
    else:
        camp = load_campaign(active)
        obj = camp["objective"]["name"]
        if not camp["params"]:
            st.info("Define the parameter space first (Campaign tab).")
        else:
            with st.form("log_add"):
                st.markdown("**Add a run**")
                cols = st.columns(len(camp["params"]) + 1)
                vals = {}
                for i, p in enumerate(camp["params"]):
                    with cols[i]:
                        if p["type"] == "num":
                            vals[p["name"]] = st.number_input(
                                p["name"], value=float(p["min"]),
                                min_value=float(p["min"]) - abs(
                                    float(p["max"]) - float(p["min"])),
                                max_value=float(p["max"]) + abs(
                                    float(p["max"]) - float(p["min"])))
                        else:
                            vals[p["name"]] = st.selectbox(
                                p["name"], p["levels"])
                with cols[-1]:
                    res = st.number_input(f"{obj} (blank = planned)",
                                          value=0.0, format="%.4f")
                    has_res = st.checkbox("result measured", value=True)
                note = st.text_input("Note (optional)")
                if st.form_submit_button("➕ Add run"):
                    row = dict(vals)
                    row["_y"] = float(res) if has_res else None
                    row["_note"] = note.strip()
                    row["_date"] = datetime.date.today().isoformat()
                    camp["runs"].append(row)
                    save_campaign(active, camp)
                    st.rerun()

            df = runs_df(camp)
            if len(df):
                show = df.rename(columns={"_y": obj, "_note": "note",
                                          "_date": "date"})
                st.dataframe(show, use_container_width=True,
                             hide_index=True)
                d1, d2 = st.columns([1, 2])
                with d1:
                    st.download_button("⬇️ Export CSV",
                                       show.to_csv(index=False),
                                       file_name=f"{active}_runs.csv",
                                       key="log_csv")
                with d2:
                    kill = st.number_input(
                        "Delete run # (1-based, top row = 1)",
                        min_value=0, max_value=len(df), value=0,
                        key="log_kill")
                    if kill and st.button("Delete", key="log_kill_go"):
                        camp["runs"].pop(int(kill) - 1)
                        save_campaign(active, camp)
                        st.rerun()

            with st.expander("📥 Import runs from CSV"):
                up = st.file_uploader("CSV with one column per "
                                      "parameter + one result column",
                                      type=["csv"], key="log_up")
                if up is not None:
                    try:
                        imp = pd.read_csv(up)
                        st.dataframe(imp.head(5),
                                     use_container_width=True)
                        maps = {}
                        mcols = st.columns(len(camp["params"]) + 1)
                        opts = ["(skip)"] + list(imp.columns)
                        for i, p in enumerate(camp["params"]):
                            with mcols[i]:
                                guess = (opts.index(p["name"])
                                         if p["name"] in imp.columns
                                         else 0)
                                maps[p["name"]] = st.selectbox(
                                    p["name"], opts, index=guess,
                                    key=f"map_{p['name']}")
                        with mcols[-1]:
                            guess = (opts.index(obj)
                                     if obj in imp.columns else 0)
                            ycol = st.selectbox(obj, opts, index=guess,
                                                key="map_y")
                        if st.button("Import", key="log_imp",
                                     disabled=(ycol == "(skip)")):
                            n = 0
                            for _, r in imp.iterrows():
                                row = {}
                                ok = True
                                for p in camp["params"]:
                                    src = maps[p["name"]]
                                    if src == "(skip)":
                                        ok = False
                                        break
                                    v = r[src]
                                    row[p["name"]] = (
                                        float(v) if p["type"] == "num"
                                        else str(v))
                                if not ok or pd.isna(r[ycol]):
                                    continue
                                row["_y"] = float(r[ycol])
                                row["_note"] = "imported"
                                row["_date"] = \
                                    datetime.date.today().isoformat()
                                camp["runs"].append(row)
                                n += 1
                            save_campaign(active, camp)
                            st.success(f"Imported {n} run(s).")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Import failed: {e}")

# ------------------------------ SUGGEST -----------------------------------
with tab_sugg:
    if not active:
        st.info("Create a campaign first.")
    else:
        camp = load_campaign(active)
        obj = camp["objective"]
        done = [r for r in camp["runs"] if r.get("_y") is not None]
        if not camp["params"]:
            st.info("Define the parameter space first.")
        else:
            s1, s2 = st.columns(2)
            with s1:
                n_sugg = st.slider("How many suggestions", 1, 10, 4,
                                   key="sg_n")
            with s2:
                explore = st.slider(
                    "Explore ↔ exploit", 0.0, 1.0, 0.3, step=0.05,
                    key="sg_xi",
                    help="Left: refine around the current best. "
                         "Right: probe unknown regions.")
            st.caption(f"{len(done)} measured runs · engine: "
                       + ("**Gaussian process (Bayesian)**"
                          if len(done) >= 5 else
                          "**Latin hypercube** (space-filling; the GP "
                          "takes over at 5 measured runs)"))
            if done:
                ngrp, pooled = replicate_noise(camp["params"], done)
                if pooled is not None:
                    st.caption(f"🎯 Measurement noise from replicates: "
                               f"σ ≈ {pooled:.3g} ({ngrp} replicated "
                               "condition(s)) - the GP treats "
                               "differences smaller than this as "
                               "noise, not signal.")
                else:
                    st.caption("🎯 No replicates yet - repeat one "
                               "condition 2-3× so the planner can "
                               "separate real effects from scatter.")
            if st.button("🤖 Suggest next experiments", type="primary",
                         key="sg_go"):
                rng = np.random.default_rng()
                if len(done) >= 5:
                    sugg, engine = gp_suggest(
                        camp["params"], done, obj["goal"], n_sugg,
                        xi=float(explore) * 0.5, rng=rng)
                else:
                    sugg, engine = (lhs_sample(camp["params"], n_sugg,
                                               rng),
                                    "Latin hypercube (space-filling)")
                st.session_state["sg_out"] = (sugg, engine)
            out = st.session_state.get("sg_out")
            if out:
                sugg, engine = out
                st.markdown(f"**Engine:** {engine}")
                dfs = pd.DataFrame(sugg)
                ren = {"_pred": f"predicted {obj['name']}",
                       "_uncertainty": "± (model std)"}
                st.dataframe(dfs.rename(columns=ren),
                             use_container_width=True, hide_index=True)
                if st.button("📋 Adopt as planned runs", key="sg_adopt"):
                    for srow in sugg:
                        row = {k: v for k, v in srow.items()
                               if not k.startswith("_")}
                        row["_y"] = None
                        row["_note"] = "planned (suggested)"
                        row["_date"] = \
                            datetime.date.today().isoformat()
                        camp["runs"].append(row)
                    save_campaign(active, camp)
                    st.success("Added to the log as planned runs - "
                               "fill in results as you do them.")
                    st.session_state.pop("sg_out", None)
                    st.rerun()

# ------------------------------ PROGRESS ----------------------------------
with tab_prog:
    if not active:
        st.info("Create a campaign first.")
    else:
        camp = load_campaign(active)
        obj = camp["objective"]
        done = [r for r in camp["runs"] if r.get("_y") is not None]
        if not done:
            st.info("No measured runs yet.")
        else:
            ys = [float(r["_y"]) for r in done]
            best = (max(ys) if obj["goal"] == "max" else min(ys))
            m1, m2, m3 = st.columns(3)
            m1.metric("Measured runs", len(done))
            m2.metric(f"Best {obj['name']}", f"{best:g}")
            m3.metric("Last run", done[-1].get("_date", "-"))
            run_best = []
            cur = ys[0]
            for y in ys:
                cur = (max(cur, y) if obj["goal"] == "max"
                       else min(cur, y))
                run_best.append(cur)
            dfp = pd.DataFrame({"run": range(1, len(ys) + 1),
                                obj["name"]: ys,
                                "best so far": run_best})
            if alt is not None:
                base = alt.Chart(dfp).encode(x="run:Q")
                ch = (base.mark_circle(size=70, color="#FF8A5C")
                      .encode(y=alt.Y(f"{obj['name']}:Q",
                                      scale=alt.Scale(zero=False)))
                      + base.mark_line(color="#FF6B3D", strokeWidth=2)
                      .encode(y="best so far:Q"))
                st.altair_chart(ch, use_container_width=True)
            num_params = [p["name"] for p in camp["params"]
                          if p["type"] == "num"]
            cat_params = [p["name"] for p in camp["params"]
                          if p["type"] == "cat"]
            if num_params or cat_params:
                px = st.selectbox("Parameter vs result",
                                  num_params + cat_params, key="pg_px")
                dfd = pd.DataFrame(done)
                if alt is not None and px in dfd.columns:
                    is_num = px in num_params
                    color = (alt.Color(f"{cat_params[0]}:N")
                             if cat_params and px != cat_params[0]
                             else alt.value("#FF8A5C"))
                    ch2 = (alt.Chart(dfd).mark_circle(size=80)
                           .encode(
                               x=alt.X(f"{px}:{'Q' if is_num else 'N'}",
                                       scale=alt.Scale(zero=False)
                                       if is_num else alt.Undefined),
                               y=alt.Y("_y:Q",
                                       title=obj["name"],
                                       scale=alt.Scale(zero=False)),
                               color=color,
                               tooltip=list(dfd.columns)))
                    st.altair_chart(ch2, use_container_width=True)

# ------------------------------ INTERPRET ---------------------------------
with tab_int:
    if not active:
        st.info("Create a campaign first.")
    else:
        camp = load_campaign(active)
        done = [r for r in camp["runs"] if r.get("_y") is not None]
        it_model = st.selectbox("Model", list(MODELS), index=3,
                                key="it_model")
        if st.button("🧠 Interpret the campaign", type="primary",
                     key="it_go", disabled=len(done) < 3):
            dfd = pd.DataFrame(done).rename(
                columns={"_y": camp["objective"]["name"]})
            umsg = (f"CAMPAIGN: {active}\n"
                    f"OBJECTIVE: {camp['objective']['name']} "
                    f"({camp['objective']['goal']}imise)\n"
                    f"PARAMETER SPACE:\n"
                    + json.dumps(camp["params"], indent=1)
                    + f"\n\nALL {len(done)} MEASURED RUNS:\n"
                    + dfd.to_csv(index=False))
            try:
                with st.spinner("Analysing..."):
                    txt = call_claude(api_key, INTERPRET_SYSTEM, umsg,
                                      MODELS[it_model],
                                      max_tokens=2500)
                st.session_state["it_out"] = txt
            except Exception as e:
                st.error(f"Claude error: {e}")
        if len(done) < 3:
            st.caption("Needs at least 3 measured runs.")
        txt = st.session_state.get("it_out")
        if txt:
            st.markdown("---")
            st.markdown(txt)
            st.download_button("⬇️ Markdown", txt,
                               file_name=f"{active}_interpretation.md",
                               key="it_dl")
