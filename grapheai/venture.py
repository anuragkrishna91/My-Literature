"""
Venture Studio - by GrapheAI. Commercialisation documents for a
technology-transfer project (built for EIC Transition TRANSPIRE):
business case, IP strategy, FTO preparation pack, and the milestone
roadmap - grounded in the data the other GrapheAI instruments hold.

Run with:
    streamlit run venture.py --server.port 8511

IMPORTANT SCOPE NOTE: the IP & FTO tab prepares material FOR a
qualified patent attorney. It is an information-organisation and
drafting aid, not legal advice, and its screening is preliminary by
design. A freedom-to-operate opinion must come from a European patent
attorney.

Shares with the rest of GrapheAI:
  - answers/venture/            project brief, features, patents, roadmap
  - answers/technoecon/         LCOE scenarios (pulled into the case)
  - answers/pv_radar.json       market/patent signals (context)
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
VENTURE_DIR = ANSWERS_DIR / "venture"

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


def save_docx(md_text, stem):
    try:
        from docx import Document
    except ImportError:
        return None
    doc = Document()
    md_to_docx(doc, md_text)
    ANSWERS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = ANSWERS_DIR / f"{stem}_{stamp}.docx"
    doc.save(path)
    return path


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
STORE_FILE = VENTURE_DIR / "project.json"

DEFAULT_BRIEF = """\
Project: TRANSPIRE (EIC Transition)
Technology: [one paragraph - what it is, what it does better]
TRL now / at project end: [x] / [y]
Product line(s): [e.g. 4T perovskite/Si tandem module ...]
Unique selling points: [efficiency / stability / cost / process]
Target customers & segment: [who pays, for what]
Team & host: Dr. Anurag Krishna, imec / EnergyVille [+ who else]
Background IP: [imec patents/know-how the project builds on]
Foreseen route: [spin-off / licence / joint venture]
Current traction: [LoIs, pilots, partners - only real ones]"""


def load_store():
    if STORE_FILE.exists():
        try:
            d = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            d.setdefault("brief", DEFAULT_BRIEF)
            d.setdefault("features", [])
            d.setdefault("patents", [])
            d.setdefault("milestones", [])
            return d
        except Exception:
            pass
    return {"brief": DEFAULT_BRIEF, "features": [], "patents": [],
            "milestones": []}


def save_store(d):
    try:
        VENTURE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(STORE_FILE)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Context pulled from the other instruments
# --------------------------------------------------------------------------
def technoecon_context():
    """Scenario inputs + computed LCOE from TechnoEcon's store."""
    p = ANSWERS_DIR / "technoecon" / "scenarios.json"
    if not p.exists():
        return ""
    try:
        scen = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    lines = []
    for s in scen:
        try:
            eff = float(s["eff"]) / 100.0
            p_stc = 1000.0 * eff
            capex = (float(s["mod_cost"]) / p_stc
                     + float(s["bos_power"])
                     + float(s["bos_area"]) / p_stc)
            r = float(s["rate"]) / 100.0
            d = float(s["deg"]) / 100.0
            n = int(s["life"])
            e1 = float(s["irr"]) * float(s["pr"])
            e_disc = om_disc = 0.0
            for t in range(1, n + 1):
                e_disc += e1 * (1 - d) ** (t - 1) / (1 + r) ** t
                om_disc += float(s["om"]) / (1 + r) ** t
            lcoe = (capex * 1000 + om_disc) / e_disc * 100
            lines.append(f"- {s['name']}: eff {s['eff']}%, module "
                         f"{s['mod_cost']} EUR/m2, degradation "
                         f"{s['deg']}%/yr, life {s['life']}y -> "
                         f"CAPEX {capex:.2f} EUR/Wp, "
                         f"LCOE {lcoe:.1f} EURc/kWh")
        except Exception:
            continue
    return "\n".join(lines)


def radar_context(categories, n=25):
    """Recent PV Radar items of the given categories."""
    p = ANSWERS_DIR / "pv_radar.json"
    if not p.exists():
        return ""
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(items, list):
        return ""
    sel = [i for i in items if i.get("category") in categories]
    sel.sort(key=lambda i: str(i.get("date", "")), reverse=True)
    return "\n".join(
        f"- [{i.get('date', '')[:10]}] {i.get('category')}: "
        f"{str(i.get('title', ''))[:140]}"
        + (f" (company: {i['company']})" if i.get("company") else "")
        for i in sel[:n])


def benchmark_context():
    p = ANSWERS_DIR / "pv_benchmark.json"
    if not p.exists():
        return ""
    try:
        return json.dumps(json.loads(p.read_text(encoding="utf-8")))[
            :4000]
    except Exception:
        return ""


def patent_url(query):
    """WIPO Patentscope full-text search (browser link)."""
    from urllib.parse import quote
    return ("https://patentscope.wipo.int/search/en/result.jsf?query="
            + quote(f'EN_ALLTXT:("{query}")'))


def espacenet_url(query):
    from urllib.parse import quote
    return ("https://worldwide.espacenet.com/patent/search?q="
            + quote(f'txt = "{query}"'))


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
BUSINESS_SYSTEM = """\
You draft the business case for a deep-tech energy project moving from
research to market (EIC-Transition style). You get the PROJECT BRIEF,
TECHNO-ECONOMIC DATA, MARKET SIGNALS and optional BENCHMARK data.
Write a complete business case in markdown with EXACTLY these sections:

# Business case: <project>
## Executive summary
## Problem & solution
## Product & unique selling points
## Market & competition
## Business model & route to market
## Techno-economics
## Traction & partnerships
## Roadmap & milestones
## Risks & mitigation  (as a markdown-free numbered list, each with
   likelihood/impact high-medium-low and one mitigation)
## The ask

Rules: use ONLY facts from the material given. Where a needed fact is
missing, insert a visible placeholder like [TO CONFIRM: 2026 module
cost target] rather than inventing one - a business case with honest
gaps beats one with invented numbers. Market signal lines are press
items: cite them as indicative, not as verified market data. Quantify
wherever the data allows. Sober investor tone, no hype adjectives."""

IPSTRAT_SYSTEM = """\
You draft an IP strategy discussion document for a research team
preparing a spin-off/licensing route (host institution: an RTO such as
imec, which will own background IP). You get the PROJECT BRIEF and the
PRODUCT FEATURES list. Write in markdown:

## What is potentially protectable
Per feature: patentable invention vs trade secret vs defensive
publication, with one-line reasoning.
## Background vs foreground IP
What likely belongs to the host institution already, what the project
generates, and the questions to settle with the TTO (tech transfer
office) - licensing terms, ownership, encumbrances.
## Filing strategy sketch
Priority filing, PCT timing, likely jurisdictions for PV manufacturing
and deployment - as a discussion basis.
## Open questions for the patent attorney

Rules: this is a discussion document for meetings with the TTO and a
patent attorney, NOT legal advice - say so in a final note. Never
assert that something IS patentable or free to use; frame everything
as 'candidate for', 'to be assessed'. Use only the material given."""

FTO_SYSTEM = """\
You prepare a PRELIMINARY freedom-to-operate screening matrix to brief
a European patent attorney. You get PRODUCT FEATURES and CANDIDATE
PATENTS (numbers, titles, assignees, and claim text where pasted, all
collected by the researcher). Produce in markdown:

## Screening matrix
One subsection per candidate patent: relevance to each feature rated
Low / Medium / Needs-attorney-review with 1-2 sentences of technical
reasoning based ONLY on the claim/title text given. If only a title is
given (no claims), the rating cannot exceed 'Needs-attorney-review -
claims not examined'.
## Possible design-around directions
Technical directions worth exploring, phrased as engineering options.
## Gaps in this screening
What was NOT searched or examined (jurisdictions, legal status, claim
construction, equivalents, pending applications).
## Questions for the attorney
Concrete, numbered.

HARD RULES: never conclude that anything does or does not infringe;
never rate anything 'High risk' or 'clear' - the strongest permitted
rating is 'Needs-attorney-review'. State in the opening line that this
is an information-organisation aid prepared without legal analysis and
that an FTO opinion must come from a qualified patent attorney."""


# --------------------------------------------------------------------------
# Page, theme, sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Venture Studio - by GrapheAI",
                   page_icon="🚀", layout="wide")

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
    "<p class='an-title'>🚀 Venture Studio</p>"
    "<p class='an-sub'>business case · IP · roadmap · by "
    "<b>GrapheAI</b> · developed by <b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("🚀 Venture Studio")
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
    ctx_te = technoecon_context()
    st.caption(("✓ TechnoEcon scenarios connected"
                if ctx_te else "○ No TechnoEcon scenarios yet")
               + "\n\n"
               + ("✓ PV Radar signals connected"
                  if (ANSWERS_DIR / "pv_radar.json").exists()
                  else "○ No PV Radar store yet"))
    st.warning("Confidential project material goes through the Claude "
               "API/Max like everything else - keep consortium-"
               "restricted documents out unless that is acceptable.")
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

store = load_store()

with st.expander("❓ How Venture Studio works"):
    st.markdown(
        "**📁 Brief** - the project facts, once. Everything else reads "
        "from it.\n\n"
        "**📑 Business case** - a complete EIC-style business case, "
        "grounded in your TechnoEcon numbers and PV Radar market "
        "signals; missing facts become visible [TO CONFIRM] "
        "placeholders, never invented numbers.\n\n"
        "**🧾 IP & FTO prep** - list your product features, collect "
        "candidate patents (search links provided), paste their main "
        "claims - and get an IP strategy discussion draft and a "
        "preliminary screening matrix *to hand to a patent attorney*. "
        "It will not and cannot tell you that you are free to "
        "operate.\n\n"
        "**🗺️ Roadmap** - milestone table + Gantt chart, feeding the "
        "business case.")

tab_brief, tab_case, tab_ip, tab_road = st.tabs(
    ["📁 Brief", "📑 Business case", "🧾 IP & FTO prep", "🗺️ Roadmap"])

# ------------------------------ BRIEF -------------------------------------
with tab_brief:
    st.markdown("**Fill the bracketed placeholders with real facts** - "
                "this is the single source of truth for every "
                "document this tool writes.")
    brief = st.text_area("Project brief", store["brief"], height=420,
                         key="vb_text")
    if st.button("💾 Save brief", key="vb_save"):
        store["brief"] = brief
        save_store(store)
        st.success("Saved.")

# ------------------------------ BUSINESS CASE -----------------------------
with tab_case:
    c1, c2 = st.columns(2)
    with c1:
        bc_model = st.selectbox("Model", list(MODELS), index=3,
                                key="bc_model")
    with c2:
        bc_extra = st.text_input(
            "Extra instructions (optional)", key="bc_extra",
            placeholder="e.g. emphasise the licensing route; 2-page "
                        "version")
    use_te = st.checkbox("Include TechnoEcon scenarios", value=True,
                         key="bc_te")
    use_radar = st.checkbox("Include PV Radar market/funding signals",
                            value=True, key="bc_radar")
    use_bench = st.checkbox("Include Benchmark (startup vs "
                            "competition) data", value=True,
                            key="bc_bench")
    if st.button("📑 Draft business case", type="primary", key="bc_go"):
        parts = [f"PROJECT BRIEF:\n{store['brief']}"]
        if use_te:
            te = technoecon_context()
            if te:
                parts.append(f"TECHNO-ECONOMIC DATA (own model):\n{te}")
        if use_radar:
            rc = radar_context(("Market", "Funding", "Manufacturing"))
            if rc:
                parts.append(f"MARKET SIGNALS (press items):\n{rc}")
        if use_bench:
            bc = benchmark_context()
            if bc:
                parts.append(f"BENCHMARK DATA:\n{bc}")
        if store["milestones"]:
            parts.append("MILESTONES:\n" + "\n".join(
                f"- {m.get('milestone')}: {m.get('start')} -> "
                f"{m.get('end')} ({m.get('workstream')})"
                for m in store["milestones"]))
        if bc_extra.strip():
            parts.append(f"EXTRA INSTRUCTIONS: {bc_extra}")
        parts.append(f"DATE: {datetime.date.today().isoformat()}")
        try:
            with st.spinner("Drafting the business case..."):
                out = call_claude(api_key, BUSINESS_SYSTEM,
                                  "\n\n".join(parts),
                                  MODELS[bc_model], max_tokens=6000)
            st.session_state["bc_out"] = out
        except Exception as e:
            st.error(f"Claude error: {e}")
    out = st.session_state.get("bc_out")
    if out:
        st.markdown("---")
        n_todo = out.count("[TO CONFIRM")
        if n_todo:
            st.warning(f"{n_todo} [TO CONFIRM] placeholder(s) - fill "
                       "them from real data before this leaves your "
                       "desk.")
        st.markdown(out)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ Markdown", out,
                               file_name="business_case.md",
                               key="bc_dl")
        with d2:
            if st.button("📄 Save as Word to answers/", key="bc_docx"):
                p = save_docx(out, "Business_case")
                st.success(f"Saved: {p}") if p else st.error(
                    "python-docx missing.")

# ------------------------------ IP & FTO ----------------------------------
with tab_ip:
    st.info("**Scope:** this tab organises information and drafts "
            "discussion documents for your TTO and patent attorney. "
            "It does not perform legal analysis, and a "
            "freedom-to-operate opinion can only come from a "
            "qualified patent attorney.")

    st.markdown("### 1 · Product features")
    st.caption("The concrete technical features of what you will "
               "sell - these are what patents are screened against.")
    fdf = pd.DataFrame(store["features"]) if store["features"] else \
        pd.DataFrame([{"feature": "", "description": ""}])
    fdf = fdf.reindex(columns=["feature", "description"])
    f_ed = st.data_editor(fdf, num_rows="dynamic", hide_index=True,
                          use_container_width=True, key="ip_fed")
    if st.button("💾 Save features", key="ip_fsave"):
        store["features"] = [
            {"feature": str(r["feature"]).strip(),
             "description": str(r.get("description") or "").strip()}
            for _, r in f_ed.iterrows()
            if str(r.get("feature") or "").strip()]
        save_store(store)
        st.success(f"Saved {len(store['features'])} feature(s).")

    st.markdown("### 2 · Collect candidate patents")
    q = st.text_input("Search phrase", key="ip_q",
                      placeholder="e.g. perovskite silicon tandem "
                                  "interconnect")
    if q.strip():
        st.markdown(f"[🔎 Patentscope]({patent_url(q.strip())}) · "
                    f"[🔎 Espacenet]({espacenet_url(q.strip())}) — "
                    "open, review, and add the relevant ones below "
                    "(paste the main independent claim where you can; "
                    "titles alone limit the screening).")
    pdf_ = pd.DataFrame(store["patents"]) if store["patents"] else \
        pd.DataFrame([{"number": "", "title": "", "assignee": "",
                       "claims": "", "notes": ""}])
    pdf_ = pdf_.reindex(columns=["number", "title", "assignee",
                                 "claims", "notes"])
    p_ed = st.data_editor(
        pdf_, num_rows="dynamic", hide_index=True,
        use_container_width=True, key="ip_ped",
        column_config={
            "claims": st.column_config.TextColumn(width="large"),
            "title": st.column_config.TextColumn(width="medium")})
    if st.button("💾 Save patents", key="ip_psave"):
        store["patents"] = [
            {k: str(r.get(k) or "").strip()
             for k in ("number", "title", "assignee", "claims",
                       "notes")}
            for _, r in p_ed.iterrows()
            if str(r.get("number") or "").strip()
            or str(r.get("title") or "").strip()]
        save_store(store)
        st.success(f"Saved {len(store['patents'])} patent(s).")

    st.markdown("### 3 · Generate documents")
    g1, g2 = st.columns(2)
    with g1:
        ip_model = st.selectbox("Model", list(MODELS), index=3,
                                key="ip_model")
    with g2:
        st.write("")
    b1, b2 = st.columns(2)
    with b1:
        if st.button("🧭 Draft IP strategy (for the TTO meeting)",
                     key="ip_strat", type="primary",
                     disabled=not store["features"]):
            umsg = (f"PROJECT BRIEF:\n{store['brief']}\n\n"
                    "PRODUCT FEATURES:\n" + "\n".join(
                        f"- {f['feature']}: {f['description']}"
                        for f in store["features"]))
            try:
                with st.spinner("Drafting..."):
                    st.session_state["ip_out"] = call_claude(
                        api_key, IPSTRAT_SYSTEM, umsg,
                        MODELS[ip_model], max_tokens=4000)
                    st.session_state["ip_kind"] = "IP_strategy"
            except Exception as e:
                st.error(f"Claude error: {e}")
    with b2:
        if st.button("🧾 Build FTO preparation pack (for the "
                     "attorney)", key="ip_fto", type="primary",
                     disabled=not (store["features"]
                                   and store["patents"])):
            umsg = ("PRODUCT FEATURES:\n" + "\n".join(
                        f"- {f['feature']}: {f['description']}"
                        for f in store["features"])
                    + "\n\nCANDIDATE PATENTS:\n" + "\n\n".join(
                        f"[{p['number'] or '?'}] {p['title']}\n"
                        f"Assignee: {p['assignee'] or '?'}\n"
                        f"Claims pasted: "
                        f"{p['claims'] or '(none - title only)'}\n"
                        f"Notes: {p['notes']}"
                        for p in store["patents"]))
            try:
                with st.spinner("Building the pack..."):
                    st.session_state["ip_out"] = call_claude(
                        api_key, FTO_SYSTEM, umsg,
                        MODELS[ip_model], max_tokens=6000)
                    st.session_state["ip_kind"] = "FTO_prep_pack"
            except Exception as e:
                st.error(f"Claude error: {e}")
    if not store["features"]:
        st.caption("Save at least one feature to enable generation; "
                   "the FTO pack also needs at least one patent.")
    ip_out = st.session_state.get("ip_out")
    if ip_out:
        st.markdown("---")
        st.markdown(ip_out)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ Markdown", ip_out,
                               file_name=f"{st.session_state.get('ip_kind', 'ip')}.md",
                               key="ip_dl")
        with d2:
            if st.button("📄 Save as Word to answers/", key="ip_docx"):
                p = save_docx(ip_out,
                              st.session_state.get("ip_kind", "IP"))
                st.success(f"Saved: {p}") if p else st.error(
                    "python-docx missing.")

# ------------------------------ ROADMAP -----------------------------------
with tab_road:
    st.caption("Milestones feed the business case automatically.")

    with st.expander("🚀 Grant-to-startup roadmap (from project end to first revenue)",
                     expanded=not store["milestones"]):
        st.caption("Turns the facts of a funded project (TRL now and at the end, IP status, "
                   "team, cash need) into a staged roadmap with decision gates across "
                   "six lanes - technology, IP, team, financing, market, "
                   "certification/regulatory - and adds the milestones to the Gantt below. "
                   "Dates and amounts come only from what you enter; gaps are marked "
                   "[FOUNDER: ...].")
        g1, g2, g3 = st.columns(3)
        with g1:
            gs_name = st.text_input("Project", value=store.get("project_name", ""), key="gs_name")
            gs_end = st.text_input("Grant end date (YYYY-MM-DD)", key="gs_end")
        with g2:
            gs_trl_now = st.selectbox("TRL now", list(range(1, 10)), index=3, key="gs_trl_now")
            gs_trl_end = st.selectbox("TRL at grant end", list(range(1, 10)), index=5, key="gs_trl_end")
        with g3:
            gs_horizon = st.selectbox("Roadmap horizon (months after grant end)",
                                      [12, 18, 24, 36], index=2, key="gs_hz")
            gs_cash = st.text_input("Cash need to first revenue (EUR, if known)", key="gs_cash")
            gs_model = st.selectbox("Model", list(MODELS), index=min(3, len(MODELS) - 1), key="gs_model")
        gs_facts = st.text_area("Facts: IP status (filed / to file), team and gaps, pilot "
                                "customers, certification needed, spin-out policy of the host, "
                                "follow-on funding options considered", height=120, key="gs_facts")
        if st.button("🗺️ Build the roadmap", type="primary", key="gs_go",
                     disabled=not (gs_end.strip() and gs_facts.strip() and api_key.strip())):
            sysm = (
                "You are a deep-tech venture builder who has taken university energy "
                "technologies from EU grants to seed-funded companies. From the FACTS "
                "given, write a grant-to-startup roadmap from the grant end date over the "
                "horizon given, in six lanes: Technology (TRL steps, pilots), IP (filings, "
                "FTO, licensing from the host), Team (founder roles, hires), Financing "
                "(non-dilutive follow-ons such as EIC Transition/Accelerator, national "
                "spin-off grants, angel/seed; amounts only if given), Market (LOIs, pilot "
                "customers, pricing evidence), Certification/regulatory (IEC 61215/61730, "
                "bankability where relevant). Give 2-4 decision gates with go/no-go criteria. "
                "Use ONLY the facts provided; mark every missing fact as [FOUNDER: ...]; "
                "never invent partners, customers, amounts or dates. Markdown with one "
                "section per lane, then '## Decision gates', then a fenced json block "
                '{"milestones": [{"milestone": "...", "workstream": "<lane>", "start": '
                '"YYYY-MM-DD", "end": "YYYY-MM-DD"}]} with 10-20 dated milestones inside '
                "the horizon.")
            umsg = (f"PROJECT: {gs_name}\nGRANT END: {gs_end}\nTRL NOW: {gs_trl_now}; TRL AT "
                    f"GRANT END: {gs_trl_end}\nHORIZON: {gs_horizon} months after grant end\n"
                    f"CASH NEED: {gs_cash or '[FOUNDER: estimate]'}\n\nFACTS:\n{gs_facts}\n\n"
                    f"EXISTING MILESTONES:\n{json.dumps(store.get('milestones', []))[:4000]}")
            try:
                with st.spinner("Building the roadmap..."):
                    out = call_claude(api_key.strip(), sysm, umsg, MODELS[gs_model], max_tokens=6000)
                st.session_state["gs_out"] = out
            except Exception as e:
                st.error(f"Claude error: {e}")
        gs_out = st.session_state.get("gs_out")
        if gs_out:
            st.markdown(re.sub(r"```json.*?```", "", gs_out, flags=re.S))
            m = re.search(r"```json\s*(.*?)```", gs_out, re.S)
            new_ms = []
            if m:
                try:
                    new_ms = [x for x in json.loads(m.group(1)).get("milestones", [])
                              if isinstance(x, dict) and x.get("milestone")]
                except Exception:
                    new_ms = []
            c1, c2 = st.columns(2)
            with c1:
                if new_ms and st.button(f"➕ Add {len(new_ms)} milestone(s) to the Gantt",
                                        key="gs_add"):
                    have = {(x.get("milestone"), x.get("start")) for x in store["milestones"]}
                    for x in new_ms:
                        try:
                            datetime.date.fromisoformat(str(x.get("start"))[:10])
                            datetime.date.fromisoformat(str(x.get("end"))[:10])
                        except ValueError:
                            continue
                        if (x["milestone"], x.get("start")) not in have:
                            store["milestones"].append({
                                "milestone": str(x["milestone"])[:80],
                                "workstream": str(x.get("workstream", ""))[:40],
                                "start": str(x["start"])[:10], "end": str(x["end"])[:10]})
                    save_store(store)
                    st.success("Added. Save the roadmap below to keep edits.")
                    st.rerun()
            with c2:
                if st.button("📄 Save roadmap as Word", key="gs_docx"):
                    p = save_docx(gs_out, "grant_to_startup_roadmap")
                    st.success(f"Saved: {p}") if p else st.error("python-docx missing.")
    mdf = pd.DataFrame(store["milestones"]) if store["milestones"] \
        else pd.DataFrame([{"milestone": "", "workstream": "",
                            "start": "", "end": ""}])
    mdf = mdf.reindex(columns=["milestone", "workstream", "start",
                               "end"])
    m_ed = st.data_editor(
        mdf, num_rows="dynamic", hide_index=True,
        use_container_width=True, key="rd_ed",
        column_config={
            "start": st.column_config.TextColumn(
                help="YYYY-MM-DD"),
            "end": st.column_config.TextColumn(help="YYYY-MM-DD")})
    if st.button("💾 Save roadmap", key="rd_save"):
        rows, bad = [], 0
        for _, r in m_ed.iterrows():
            name = str(r.get("milestone") or "").strip()
            if not name:
                continue
            s_, e_ = str(r.get("start") or ""), str(r.get("end") or "")
            try:
                datetime.date.fromisoformat(s_[:10])
                datetime.date.fromisoformat(e_[:10])
            except ValueError:
                bad += 1
                continue
            rows.append({"milestone": name,
                         "workstream":
                         str(r.get("workstream") or "").strip(),
                         "start": s_[:10], "end": e_[:10]})
        store["milestones"] = rows
        save_store(store)
        msg = f"Saved {len(rows)} milestone(s)."
        if bad:
            msg += f" {bad} row(s) skipped (dates must be YYYY-MM-DD)."
        st.success(msg)
    if store["milestones"] and alt is not None:
        gdf = pd.DataFrame(store["milestones"])
        gdf["start"] = pd.to_datetime(gdf["start"])
        gdf["end"] = pd.to_datetime(gdf["end"])
        chart = alt.Chart(gdf).mark_bar(
            cornerRadius=4, height=18, color="#FF6B3D").encode(
            x=alt.X("start:T", title=None),
            x2="end:T",
            y=alt.Y("milestone:N", sort=alt.EncodingSortField(
                field="start", order="ascending"), title=None),
            color=alt.Color("workstream:N",
                            title="Workstream") if
            gdf["workstream"].astype(bool).any() else
            alt.value("#FF6B3D"),
            tooltip=["milestone", "workstream", "start:T", "end:T"])
        st.altair_chart(chart, use_container_width=True)
        today = pd.Timestamp.today().normalize()
        done = int((gdf["end"] < today).sum())
        st.caption(f"{len(gdf)} milestones · {done} past their end "
                   "date.")
