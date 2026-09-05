"""
Analytics - nightly auto-extract (headless).

For every Analytics schema whose dataset has auto-extract enabled (the
🌙 toggle in the app), this script finds newly indexed papers matching
that schema's saved scope and extracts them - up to 100 papers per
schema per night, using Haiku.

Claude access: Claude Code login (Max) via claude-agent-sdk, else the
ANTHROPIC_API_KEY environment variable.

Manual run:   /opt/miniconda3/bin/python analytics_refresh.py
Scheduled:    via launchd (see Claude's setup instructions).
Log:          answers/analytics_refresh.log
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
ANSWERS = HERE / "answers"
ANALYTICS_DIR = ANSWERS / "analytics"
LOG_FILE = ANSWERS / "analytics_refresh.log"

MODEL = "claude-haiku-4-5"
PER_SCHEMA_CAP = 100
FILE_YEAR = re.compile(r"_((?:19|20)\d{2})_")


def log(msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        ANSWERS.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ----------------------------- Claude --------------------------------------
def call_claude_max(system, user_msg):
    from claude_agent_sdk import (query, ClaudeAgentOptions,
                                  AssistantMessage, TextBlock,
                                  ResultMessage)
    import asyncio

    async def _run():
        opts = ClaudeAgentOptions(
            system_prompt=system, model=MODEL, max_turns=1,
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Glob",
                              "Grep", "WebSearch", "WebFetch", "Task",
                              "NotebookEdit"])
        parts = []
        async for message in query(prompt=user_msg, options=opts):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                if getattr(message, "is_error", False):
                    raise RuntimeError(getattr(message, "result", None)
                                       or "Claude Code error")
        return "".join(parts)

    return asyncio.run(_run())


def call_claude_api(system, user_msg):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=1500, system=system,
        messages=[{"role": "user", "content": user_msg}])
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text")


def make_caller():
    try:
        import claude_agent_sdk  # noqa: F401
        return call_claude_max, "Claude Max (Agent SDK)"
    except ImportError:
        pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        return call_claude_api, "Anthropic API key"
    return None, None


ANALYTICS_SYSTEM = """\
You extract a structured data table from ONE document (a research paper,
or a grant proposal / evaluation report). You get FIELDS (name + hint)
and the document text. Respond with ONLY a JSON array of 1-5 row
objects. Each row maps EVERY field name EXACTLY as given to a value:
- numbers as plain numbers (no units inside the value),
- short strings for categorical fields,
- null when the document does not state it.
Use multiple rows ONLY when the document genuinely reports multiple
distinct items. NEVER invent or estimate a value - null is always the
safe answer.
Additionally, EVERY row must include the key "_evidence": one sentence
or table fragment (max 40 words) COPIED VERBATIM from the document
that contains the row's main numeric value(s). Copy exactly -
"_evidence" is checked mechanically against the document text, and a
paraphrased or invented quote marks the whole row as unverified. Use
null only if the row has no numeric values."""


def _quote_in_text(quote, text):
    """Does the claimed evidence quote actually appear in the paper?"""
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
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    a, b = raw.find("["), raw.rfind("]")
    if a == -1 or b <= a:
        raise ValueError("no JSON array in reply")
    data = json.loads(raw[a:b + 1])
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


# ----------------------------- Corpus --------------------------------------
def load_corpus():
    import chromadb
    import config
    client = chromadb.PersistentClient(path=str(config.DB_DIR))
    col = client.get_collection(config.COLLECTION_NAME)
    total = col.count()
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
    return col, sorted(papers.values(), key=lambda r: r["file"])


def _chunk_score(t):
    if not t:
        return 0.0
    digits = sum(ch.isdigit() for ch in t)
    units = len(re.findall(
        r"%|mA/?cm|\bm?V\b|\beV\b|\bFF\b|Jsc|Voc|PCE|efficien|"
        r"T80|ISOS|\btable\b", t, re.I))
    return (digits + 8 * units) / max(len(t), 1)


def paper_text(col, sig, max_chars):
    """Full text if it fits; otherwise the opening chunk plus the most
    data-dense chunks, in page order (same logic as the app)."""
    got = col.get(where={"doc_sig": sig},
                  include=["documents", "metadatas"])
    pairs = [(d, m) for d, m in zip(got["documents"], got["metadatas"])
             if m.get("type") != "figure"]
    pairs.sort(key=lambda x: x[1].get("page_start", 0))
    texts = [d or "" for d, m in pairs]
    if not texts:
        return ""
    if sum(len(t) + 2 for t in texts) <= max_chars:
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


def in_scope(p, scope, journals):
    srcs = scope.get("sources") or []
    if srcs and p["source"] not in srcs:
        return False
    tf = (scope.get("title") or "").lower()
    if tf and tf not in p["title"].lower() and tf not in p["file"].lower():
        return False
    yrs = scope.get("years") or []
    yf = scope.get("year_from") or ""
    if yrs:
        m = FILE_YEAR.search(p["file"])
        if not m or m.group(1) not in yrs:
            return False
    elif yf:
        m = FILE_YEAR.search(p["file"])
        if not m or m.group(1) < yf:
            return False
    jrs = scope.get("journals") or []
    if jrs and journals.get(p["sig"]) not in jrs:
        return False
    return True


def main():
    if not ANALYTICS_DIR.exists():
        log("No analytics datasets - nothing to do.")
        return
    auto = []
    for f in ANALYTICS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("auto") and d.get("auto_scope"):
            auto.append((f, d))
    if not auto:
        log("No schemas with auto-extract enabled.")
        return

    call, backend = make_caller()
    if call is None:
        log("No Claude backend available - aborting.")
        sys.exit(1)

    journals = {}
    jr = ANSWERS / "journals.json"
    if jr.exists():
        try:
            journals = json.loads(jr.read_text(encoding="utf-8"))
        except Exception:
            pass

    col, papers = load_corpus()
    log(f"{len(auto)} auto schema(s), {len(papers)} papers in corpus, "
        f"backend: {backend}.")

    for f, ds in auto:
        scope = ds["auto_scope"]
        fields = [x[0] for x in scope.get("fields", [])]
        hints = dict(scope.get("fields", []))
        if len(fields) < 2:
            log(f"{f.stem}: bad saved scope - skipped.")
            continue
        todo = [p for p in papers
                if in_scope(p, scope, journals)
                and ds["papers"].get(p["sig"], {}).get("status")
                != "done"][:PER_SCHEMA_CAP]
        if not todo:
            log(f"{f.stem}: up to date.")
            continue
        field_block = "\n".join(f"- {x}: {hints.get(x, '')}"
                                for x in fields)
        ok = err = nrows = 0
        for p in todo:
            try:
                text = paper_text(col, p["sig"],
                                  int(scope.get("depth", 16000)))
                if len(text) < 400:
                    raise ValueError("too little text")
                raw = call(ANALYTICS_SYSTEM,
                           f"FIELDS:\n{field_block}\n\n"
                           f"DOCUMENT: {p['title']}\n\n{text}")
                rows = parse_rows(raw, fields)
                ym = FILE_YEAR.search(p["file"])
                for r in rows:
                    r["_ev_ok"] = _quote_in_text(r.get("_evidence"),
                                                 text)
                    r.update({"_paper": p["title"], "_file": p["file"],
                              "_sig": p["sig"], "_source": p["source"],
                              "_year": int(ym.group(1)) if ym else None})
                ds["rows"] = [r for r in ds["rows"]
                              if r.get("_sig") != p["sig"]]
                ds["rows"].extend(rows)
                ds["papers"][p["sig"]] = {"status": "done",
                                          "n": len(rows)}
                ok += 1
                nrows += len(rows)
            except Exception as e:
                msg = str(e)
                ds["papers"][p["sig"]] = {"status": "error",
                                          "err": msg[:200]}
                err += 1
                if any(w in msg.lower() for w in
                       ("usage", "limit", "rate", "credit")):
                    log(f"{f.stem}: stopped on limit after {ok}.")
                    break
            # crash-safe save each paper
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ds, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(f)
        log(f"{f.stem}: +{ok} docs -> {nrows} rows ({err} errors); "
            f"total {len(ds['rows'])} rows.")


if __name__ == "__main__":
    main()
