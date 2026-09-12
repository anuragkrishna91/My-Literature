"""
Manuscript Workbench v2 - browser UI for the literature RAG assistant.

Run with:
    streamlit run workbench.py

Tabs:
  Ask            - Q&A across the whole corpus, with answer modes and
                   optional follow-up context.
  Claim checker  - paste draft sentences; each is checked against the
                   corpus: SUPPORTED / PARTIALLY / CONTRADICTED / NOT FOUND.
  Library        - browse all indexed papers; summarise or chat with a
                   single paper.

Sidebar: API key, model selector (Haiku/Sonnet/Opus), answer mode,
retrieval depth, auto-save toggle, session exports, token counter.
Every answer is auto-saved as .docx and .md in the 'answers' folder.
"""

import datetime
import io
import re
from pathlib import Path

import sys
import json
import streamlit as st
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from docx import Document
from docx.shared import Pt, RGBColor, Inches

import config
from ask_rules import (build_system_prompt, ANSWER_MODES,
                       build_draft_prompt, DRAFT_FORMATS)

ANSWERS_DIR = Path("answers")

MODELS = {
    "Fast (Haiku 4.5)": "claude-haiku-4-5",
    "Balanced (Sonnet 5)": "claude-sonnet-5",
    "Deep (Opus 5)": "claude-opus-5",
    "Frontier (Fable 5.1)": "claude-fable-5-1",
}

CLAIM_SYSTEM = """\
You are a scientific fact-checker. You receive ONE claim from a manuscript
draft and numbered excerpts from the literature. Judge the claim strictly
against the excerpts.

Output format (exactly):
VERDICT: one of SUPPORTED | PARTIALLY SUPPORTED | CONTRADICTED | NOT FOUND
EVIDENCE: 2-5 sentences citing excerpts as [n]. Quote the decisive values
with units and conditions. If PARTIALLY SUPPORTED, state precisely which
part holds and which does not. If CONTRADICTED, give the conflicting
value(s). If NOT FOUND, say what evidence would be needed.

Rules: use only the excerpts; never use outside knowledge; never soften a
contradiction; treat absence of evidence as NOT FOUND, not as support."""

# ---------------------------------------------------------------------------
# Review tab: system prompts per review type
# ---------------------------------------------------------------------------
REVIEW_COMMON = """\
You are reviewing a document supplied by its author, who wants honest,
expert, actionable feedback before submission. Rules that apply throughout:
- Quote the exact phrase or sentence every comment refers to, so the author
  can find it instantly.
- Every criticism must come with a concrete suggested fix.
- Do not invent facts, results, or references; if something needs a citation
  or a value you cannot know, say so explicitly instead of guessing.
- Be rigorous the way a good referee is: direct about problems, never vague,
  never rude."""

REVIEW_MODES = {
    "Referee report (like a journal reviewer)": REVIEW_COMMON + """

Write a complete peer-review report with exactly these sections:
1. SUMMARY - 3-5 sentences showing you understood the work.
2. SIGNIFICANCE & NOVELTY - strengths first, then genuine concerns.
3. MAJOR COMMENTS - numbered; issues that affect the science, logic, or
   completeness. Each: the quoted text or section concerned, the problem,
   and the specific change that would resolve it.
4. MINOR COMMENTS - numbered quick fixes: clarity, missing experimental
   details, figure/table issues, referencing gaps.
5. RECOMMENDATION - accept / minor revision / major revision / reject,
   with a one-sentence justification.""",

    "Language & clarity (line edits)": REVIEW_COMMON + """

Act as a scientific copy editor. List the most important line edits (up to
40), highest-impact first, each formatted exactly as:
ORIGINAL: "<quoted text>"
REVISED: "<your rewrite>"
WHY: <reason in under 10 words>

Prioritise ambiguity, wordiness, grammar, tense consistency, and weak topic
sentences. Finish with the 3 recurring writing habits the author should fix
across the whole document.""",

    "Structure & argumentation": REVIEW_COMMON + """

Review the document's structure and logic, not its prose. Cover:
- Whether title and abstract match what the document actually shows.
- Section order and balance (what is over- or under-weighted).
- Paragraph-level flow: where a paragraph carries no clear point, repeats
  another, or breaks the argument's chain.
- Where a reader gets lost or a claim arrives before its support.
End with a proposed revised outline (section by section, one line each).""",

    "Revise & polish (rewrites the text)": REVIEW_COMMON + """

Rewrite the passage the user provides. Keep every technical claim, value,
unit, and citation marker unchanged; improve clarity, flow, and concision;
tighten wordy sentences; fix grammar. Mark each substantive wording change
in **bold** so the author can spot what changed. Return ONLY the revised
text - no preamble, no commentary.""",
}


def extract_uploaded_text(uploaded):
    """Read text from an uploaded .docx / .pdf / .txt / .md file."""
    name = uploaded.name.lower()
    data = uploaded.getvalue()
    if name.endswith(".docx"):
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(p for p in parts if p.strip())
    if name.endswith(".pdf"):
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return "\n\n".join(page.get_text("text") for page in doc)
    if name.endswith((".csv", ".tsv", ".xlsx", ".xls")):
        return table_to_text(extract_uploaded_table(uploaded))
    return data.decode("utf-8", errors="replace")


def extract_uploaded_table(uploaded, max_rows=5000):
    """CSV / TSV / XLSX upload -> {"name", "columns", "rows", "n_rows"}.
    First sheet of a workbook; blank rows/columns dropped; values are plain
    Python (floats, strings, None) so the table can be saved in job state
    and handed to the figure renderer as TABLES[name]."""
    import pandas as pd
    name = uploaded.name
    data = uploaded.getvalue()
    low = name.lower()
    if low.endswith((".xlsx", ".xls")):
        try:
            df = pd.read_excel(io.BytesIO(data), sheet_name=0)
        except ImportError as e:
            raise RuntimeError("Reading Excel files needs openpyxl: "
                               "/opt/miniconda3/bin/pip install openpyxl") from e
    else:
        sep = "\t" if low.endswith(".tsv") else None
        df = pd.read_csv(io.BytesIO(data), sep=sep, engine="python")
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    head = df.head(max_rows)
    rows = head.astype(object).where(head.notna(), None).values.tolist()
    clean = []
    for r in rows:
        out = []
        for v in r:
            if v is None:
                out.append(None)
            elif hasattr(v, "item"):
                try:
                    out.append(v.item())
                except Exception:
                    out.append(str(v))
            elif isinstance(v, (int, float, str)):
                out.append(v)
            else:
                out.append(str(v))
        clean.append(out)
    return {"name": name, "columns": list(df.columns), "rows": clean,
            "n_rows": int(len(df))}


def extract_path_table(path):
    """Saved CSV/XLSX (PeroDeg, Analytics, project files) -> table dict."""
    class _Up:
        def __init__(self, p):
            self.name = Path(p).name
            self._p = Path(p)

        def getvalue(self):
            return self._p.read_bytes()
    return extract_uploaded_table(_Up(path))


def table_to_text(t, max_rows=60):
    """Text rendering of a table for the evidence digest (header + first
    rows), so ledger and planner know what the file holds."""
    lines = [f"[DATA TABLE {t['name']} - {t['n_rows']} rows x "
             f"{len(t['columns'])} columns]", " | ".join(t["columns"])]
    for r in t["rows"][:max_rows]:
        lines.append(" | ".join("" if v is None else
                                (f"{v:g}" if isinstance(v, float) else str(v))
                                for v in r))
    if t["n_rows"] > max_rows:
        lines.append(f"... ({t['n_rows'] - max_rows} more rows; the full table "
                     "is available to the figure engine)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extract tab: structured data extraction from papers
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = """\
You extract structured data from a scientific paper's full text. You receive
the text of ONE paper and a list of fields. Respond with ONLY a JSON object
(no markdown fences, no prose) whose keys are exactly the given field names.

Rules:
- Values come strictly from the text; never estimate or use outside knowledge.
- Report the CHAMPION/best device value when several are given, and note the
  spread in that field's value if important (e.g. "23.1 (champion; avg 22.4)").
- Include units only if the field name does not already specify them.
- If a field is not reported in the text, use "n/a".
- Keep every value a short string, not nested objects."""

PV_PRESET_FIELDS = [
    "PCE (%)", "Voc (V)", "Jsc (mA/cm2)", "FF (%)",
    "device architecture (n-i-p / p-i-n / module...)",
    "perovskite composition", "deposition method",
    "active area (cm2)", "stability test protocol",
    "stability result", "certification (yes/no/details)",
]

# ---------------------------------------------------------------------------
# Proposal tab: instruments, drafting, refinement, mock evaluation
# ---------------------------------------------------------------------------
PROPOSAL_SOURCES = ["proposals", "evaluations", "proposal_docs"]

INSTRUMENTS = {
    "Horizon Europe RIA/IA": """\
Criteria: Excellence (soundness of concept and methodology, ambition beyond
state of the art, interdisciplinarity, open science); Impact (pathway to
scientific/economic/societal impact, dissemination & exploitation, scale and
credibility of contributions to expected outcomes of the call topic);
Quality and efficiency of Implementation (work plan coherence, resources,
consortium capability and complementarity). Each scored 0-5, typical
threshold 3/5 per criterion and ~10/15 total; IA proposals must show
credible demonstration at scale and route to market.""",

    "EIC Pathfinder": """\
Criteria: Excellence (novel, ambitious science-towards-technology
breakthrough vision, high-risk/high-gain, beyond incremental; convincing
scientific basis at low TRL 1-3); Impact (transformational potential of the
future technology, credible innovation pathway); Implementation (soundness
of approach for a science-driven project, consortium quality). Excellence
typically needs >=4/5. Evaluators reject anything that reads as incremental
improvement of existing technology or as a disguised development project.""",

    "EIC Transition": """\
Criteria: Excellence (maturation path from validated results TRL 3/4 toward
TRL 5/6, technological novelty); Impact (market opportunity, business
model, IP strategy, team's route to commercialisation); Implementation
(work plan, milestones with go/no-go, team capabilities including business
competence). Both technology AND market maturation must advance together -
purely technical plans score poorly on Impact.""",

    "MSCA Postdoctoral Fellowship": """\
Criteria and weights: Excellence 50% (quality/novelty of the research,
two-way transfer of knowledge between researcher and host, quality of
supervision and integration); Impact 30% (career development of the
researcher, dissemination/exploitation/communication); Implementation 20%
(work plan, host infrastructure). Threshold 70% overall; winning proposals
score ~93%+, so every sub-criterion must be addressed explicitly - the
researcher's career narrative matters as much as the science.""",
}

PROPOSAL_SECTIONS = {
    "State of the art & beyond": """\
Quantified current SoA with citations [n]; the specific gap; the advance
beyond SoA with measurable targets; why this is timely now.""",
    "Objectives & KPIs": """\
Overall objective plus 3-6 specific objectives. Each specific objective:
one sentence, measurable, with a KPI (value + unit) and means of
verification. Link explicitly to the call/instrument scope.""",
    "Concept & methodology": """\
The core concept and its scientific basis [n]; methodology per objective;
key risks with mitigation; what makes the approach credible (preliminary
evidence, team expertise).""",
    "Impact & exploitation": """\
Impact pathway: results -> outcomes -> scientific, economic, societal
impacts; quantified where defensible; dissemination, exploitation and
communication measures; alignment with EU policy priorities.""",
    "Implementation & work plan": """\
Work packages with objectives, tasks, deliverables and milestones; the logic
of the timeline and dependencies; roles, effort and complementarity of the
partners; a risk register with likelihood, impact and mitigation; management
and decision-making structure; go/no-go criteria where the instrument
expects them.""",
    "Researcher, training & transfer (MSCA)": """\
Two-way knowledge transfer between researcher and host; training
objectives; supervision arrangements; career development after the
fellowship.""",
}

PROP_EVAL_SYSTEM = """\
You are an experienced European Commission expert evaluator writing an
Evaluation Summary Report (ESR) for a {instrument} proposal.

Instrument criteria:
{criteria}

Write the ESR exactly as panels do:
For EACH criterion: a score out of 5.00 (one decimal), then "Strengths:"
bullets and "Weaknesses:" bullets. Weaknesses must be specific and quote or
reference the proposal's own text; vague weaknesses ("could be clearer")
are useless. Real panels punish: unquantified claims, generic impact
statements, objectives without KPIs, missing risk mitigation, and SoA
sections an expert would find shallow or outdated.
Then: TOTAL SCORE, a verdict against typical thresholds, and "The 5 changes
that would most raise this score", ranked.
If excerpts from the applicant's PAST evaluation reports are provided,
check whether previously criticised weaknesses reappear here - flag any
repeat offence explicitly.
Base everything on the provided text; never invent content the proposal
does not contain. Note where the current call's work programme must be
checked (criteria evolve between calls)."""

PROP_DRAFT_SYSTEM = """\
You draft one section of a {instrument} proposal for an expert applicant.

Instrument criteria this section will be scored against:
{criteria}

Section brief and type are given by the user. Rules:
- Ground every state-of-the-art or technical claim in the numbered excerpts
  [n]; never invent literature. Where evidence is missing, mark
  [EVIDENCE NEEDED: ...] rather than bluffing.
- Quantify: targets, KPIs, benchmarks with values and units.
- Write in confident, concrete proposal English - no generic filler
  ("cutting-edge", "holistic approach") that evaluators recognise as
  padding. Short paragraphs; bold key claims sparingly with **markdown**.
- This is raw material: the applicant will rework it in their own voice."""

PROP_REVISE_PLAN_SYSTEM = """\
You are an expert proposal editor working on a FULL draft of a {instrument}
proposal.

Instrument criteria:
{criteria}

If evaluator feedback is provided, treat it as the primary driver: every
criticised weakness must be addressed by name.

Produce a REVISION PLAN, not a rewrite:
1. VERDICT IN BRIEF - 3-4 sentences: where this draft stands against the
   criteria and what most limits its score.
2. SECTION-BY-SECTION REVISIONS - for each section of the draft, in order:
   the problems (quote the draft's own text), then concrete instructions,
   and for the 1-2 most load-bearing passages per section a ready-to-paste
   rewrite. Mark each item [CRITICAL] / [IMPORTANT] / [POLISH].
3. CROSS-CUTTING FIXES - terminology, numbers that disagree between
   sections, missing quantification, structure.
4. PRIORITY ORDER - the 5 changes to make first if time is short.
If AUTHOR-SUPPLIED REVISION MATERIAL is provided (new results, partially
rewritten sections, notes), the plan must say exactly where each piece
belongs in the draft and what it replaces; where it conflicts with the
draft, the new material wins.
Base everything on the draft (and feedback/material/excerpts) - never
invent results they do not contain."""

PROP_REWRITE_CHUNK_SYSTEM = """\
You are revising one PART of a full {instrument} proposal draft. The whole
document is being revised part by part; you get global revision goals, any
evaluator feedback, the tail of the previous (already revised) part for
continuity, and the part to revise.

Instrument criteria:
{criteria}

Rules:
- Revise ONLY the given part. Keep its headings and its place in the
  document's structure; do not write content that belongs to other parts.
- Keep all technical facts, values, project names and citation markers
  unless the revision goals or feedback explicitly require changing them.
  Never invent results, partners, or figures - where something is needed but
  unknowable, insert [AUTHOR: add ...].
- Sharpen against the criteria: quantify claims, make objectives measurable,
  cut filler, tighten structure.
- If AUTHOR-SUPPLIED REVISION MATERIAL is provided, weave into THIS part
  only the pieces that belong here (matching its topic/section); where the
  material conflicts with the draft, the material wins. Ignore material that
  belongs to other parts.
- Mark substantive wording changes in **bold** so the author can review
  what changed. Unchanged sentences stay unmarked.
- Return ONLY the revised text of this part - no commentary, no preamble."""


def split_proposal(text, target=7000):
    """Split a long draft into revision chunks at paragraph/heading
    boundaries. Returns a list of text chunks whose concatenation preserves
    the document."""
    heading_re = re.compile(r"^\s{0,3}(\d+(\.\d+)*[\.\)]?\s+\S|[A-Z][A-Z \-&]{6,80}$)")
    lines = text.split("\n")
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        is_heading = bool(heading_re.match(line.strip())) and len(line) < 90
        if cur and (cur_len >= target
                    or (is_heading and cur_len >= target * 0.4)):
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


PROP_COMPLETE_SYSTEM = """\
You complete a PARTIAL draft of a {instrument} proposal section into a
finished section.

Instrument criteria this section is scored against:
{criteria}

The author's draft may mix finished prose, bullet points, rough notes,
placeholders ([TODO], TBD, "..."), empty headings, and gaps.

Rules:
- PRESERVE the author's existing sentences and their voice wherever they are
  already serviceable. Do not rewrite good prose just to make it yours - the
  author must still recognise this as their proposal.
- EXPAND bullets and notes into full proposal prose.
- FILL gaps and placeholders with substantive content that fits the section's
  role and the instrument's criteria.
- Ground state-of-the-art and technical claims in the numbered excerpts [n];
  never invent literature. Where a specific value, result, partner name or
  budget figure is needed but unknowable, insert [AUTHOR: add ...] instead of
  inventing it.
- Quantify what the criteria reward: targets, KPIs with units, baselines,
  milestones.
- Wrap every passage YOU wrote in <<...>> so the author can see at a glance
  what is new versus their own text. Do not mark preserved text.
- End with a "GAPS REMAINING" list: what the author must still supply for
  this section to be submission-ready.
Write to approximately the requested length."""

PROP_REFINE_SYSTEM = """\
You strengthen a section of a {instrument} proposal against its evaluation
criteria:
{criteria}

You receive the section text (and optionally supporting literature
excerpts [n]). Return:
1. The REVISED section - same substance, sharpened for evaluation: claims
   quantified, objectives made measurable, vague phrases replaced,
   structure tightened. Mark substantive changes in **bold**. Where a
   needed number/fact is missing, insert [AUTHOR: add ...].
2. "What changed and why" - short bullets, each mapped to the criterion it
   serves.
Never change the technical substance or invent results."""

COMPARE_SYSTEM = """\
You write the benchmarking discussion for a manuscript. You receive a
comparison table with numbered literature entries and the author's own
results (rows labelled "This work").

Produce:
1. A manuscript-ready discussion paragraph (120-200 words) positioning
   "This work" against the literature: where it leads, matches, or trails,
   quoting the decisive values. Cite literature rows as [n]. Measured,
   objective tone - overstated claims do not survive peer review.
2. "Standout comparisons": up to 5 bullets, each one sharp quantitative
   contrast.

Rules: use ONLY the values in the table; treat "n/a" as unreported, never
as zero; if This work trails in a metric, say so plainly - and where the
table supports it, note the trade-off context (e.g. larger area, harsher
stability test). Note where [n] must be replaced by the manuscript's real
reference numbers."""

RESPONSE_SYSTEM = """\
You draft a response-to-reviewers letter for a manuscript author. You receive
the reviewers' comments, the manuscript text, and (optionally) numbered
excerpts from the author's literature corpus.

Produce a complete point-by-point response:
- Number every distinct reviewer comment (R1.1, R1.2, R2.1, ...). Quote the
  comment (shortened if long), then write "Response:" followed by the reply.
- Tone: polite, confident, never defensive or obsequious. Thank reviewers
  once at the start, not in every reply.
- Agree where the reviewer is right and state the concrete change made,
  writing it as the author would ("We have revised Section 3 to...").
- Push back where the reviewer is wrong, with evidence: cite the manuscript's
  own data or the literature excerpts as [n].
- Where a reply needs new data, analysis, or a decision only the author can
  make, insert a clearly marked placeholder: [AUTHOR ACTION: ...].
- Never invent results, references, or changes that are not supported by the
  manuscript or the excerpts.
End with a one-paragraph summary of the main revisions."""


# ---------------------------------------------------------------------------
# Reference list builder: map cited corpus papers to real bibliography entries
# ---------------------------------------------------------------------------
def crossref_lookup(title, email):
    """Find a paper on Crossref by title. Cached per session. Returns a dict
    (with 'ok' False when the match looks doubtful) or None on failure."""
    cache = st.session_state.setdefault("crossref_cache", {})
    if title in cache:
        return cache[title]
    import requests as _rq
    try:
        r = _rq.get("https://api.crossref.org/works",
                    params={"query.bibliographic": title[:250], "rows": 1,
                            "mailto": email},
                    timeout=20)
        item = r.json()["message"]["items"][0]
    except Exception:
        cache[title] = None
        return None
    found_title = " ".join(item.get("title") or [])
    ta = set(re.findall(r"\w+", title.lower()))
    tb = set(re.findall(r"\w+", found_title.lower()))
    overlap = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    rec = {
        "ok": overlap >= 0.45,
        "authors": item.get("author") or [],
        "year": (item.get("issued", {}).get("date-parts") or [[None]])[0][0],
        "journal": (" ".join(item.get("container-title") or [])
                    or item.get("publisher", "")),
        "volume": item.get("volume", ""),
        "pages": item.get("page", ""),
        "doi": item.get("DOI", ""),
        "title": found_title,
    }
    cache[title] = rec
    return rec


def _author_names(rec, max_names=3):
    auth = rec.get("authors") or []
    if not auth:
        return "Unknown authors"

    def one(a):
        return f"{(a.get('given') or '')[:1]}. {a.get('family', '')}".strip(". ")

    if len(auth) > max_names:
        return f"{one(auth[0])} et al."
    return ", ".join(one(a) for a in auth)


def format_reference(rec, fallback_title, fallback_file):
    if not rec:
        return (f"{fallback_title} ({fallback_file}) - not found on Crossref; "
                f"cite manually.")
    flag = "" if rec["ok"] else "  [VERIFY - uncertain Crossref match]"
    vol = f" {rec['volume']}" if rec["volume"] else ""
    pg = f", {rec['pages']}" if rec["pages"] else ""
    return (f"{_author_names(rec)}, {rec['title']}, {rec['journal']}{vol}{pg} "
            f"({rec['year']}). https://doi.org/{rec['doi']}{flag}")


def bibtex_entry(rec, key):
    authors = " and ".join(
        f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")
        for a in (rec.get("authors") or [])) or "Unknown"
    fields = [f"  author = {{{authors}}}",
              f"  title = {{{rec['title']}}}",
              f"  journal = {{{rec['journal']}}}",
              f"  year = {{{rec['year']}}}"]
    if rec["volume"]:
        fields.append(f"  volume = {{{rec['volume']}}}")
    if rec["pages"]:
        fields.append(f"  pages = {{{rec['pages']}}}")
    if rec["doi"]:
        fields.append(f"  doi = {{{rec['doi']}}}")
    return "@article{" + key + ",\n" + ",\n".join(fields) + "\n}"


def bibtex_key(rec, used):
    fam = (rec.get("authors") or [{}])[0].get("family", "Unknown")
    fam = re.sub(r"[^A-Za-z]", "", fam) or "Unknown"
    base = f"{fam}{rec.get('year') or ''}"
    key, suffix = base, "a"
    while key in used:
        key = base + suffix
        suffix = chr(ord(suffix) + 1)
    used.add(key)
    return key


# --------------------------------------------------------------------------
# Cached resources
# --------------------------------------------------------------------------
@st.cache_resource
def load_collection():
    embed_fn = SentenceTransformerEmbeddingFunction(model_name=config.EMBED_MODEL)
    client = chromadb.PersistentClient(path=str(config.DB_DIR))
    return client.get_collection(config.COLLECTION_NAME, embedding_function=embed_fn)


@st.cache_data(show_spinner="Reading library catalogue...")
def library_table():
    """One row per paper: title, file, passages, figures.

    Cached per index size, so filters and the Library tab don't re-scan
    137k chunk metadatas on every interaction.
    """
    return _library_table_cached(load_collection().count())


@st.cache_data(show_spinner=False)
def _library_table_cached(total):
    # Metadata is fetched in batches: a single get() on a large collection
    # exceeds SQLite's bound-variable limit ('too many SQL variables').
    col = load_collection()
    papers = {}
    BATCH = 5000
    offset = 0
    while offset < total:
        got = col.get(include=["metadatas"], limit=BATCH, offset=offset)
        metas = got["metadatas"]
        if not metas:
            break
        for m in metas:
            sig = m["doc_sig"]
            p = papers.setdefault(sig, {"Title": m["title"], "File": m["file"],
                                        "Passages": 0, "Figures": 0,
                                        "Publisher": m.get("publisher", ""),
                                        "Source": m.get("source", ""),
                                        "sig": sig})
            if m.get("type") == "figure":
                p["Figures"] += 1
            else:
                p["Passages"] += 1
        offset += len(metas)
    rows = sorted(papers.values(), key=lambda r: r["File"])
    return rows


def _meta_matches(meta, clause):
    """Client-side evaluation of the small where-clause subset we use."""
    if "$and" in clause:
        return all(_meta_matches(meta, c) for c in clause["$and"])
    for key, cond in clause.items():
        if isinstance(cond, dict):
            if "$in" in cond and meta.get(key) not in cond["$in"]:
                return False
        elif meta.get(key) != cond:
            return False
    return True


# ---------------------------------------------------------------------------
# Library: journal-family detection
# The DOI is nearly always printed on a paper's first page, and that page is
# already in the index - so we read it from there and map the DOI prefix to a
# publisher. Journal-name keywords are the fallback. No API calls, one pass,
# cached to answers/journals.json.
# ---------------------------------------------------------------------------
JOURNALS_FILE = ANSWERS_DIR / "journals.json"

# Ordered: first match wins, so specific titles come before generic words
# ("Energy & Environmental Science" must beat a bare "Science").
JOURNAL_PATTERNS = [
    (r"nature\s+(energy|materials|communications|photonics|nanotechnology|"
     r"physics|chemistry|reviews|sustainability|catalysis)", "Nature"),
    (r"\bnpj\b|scientific\s+reports|communications\s+(materials|physics|"
     r"chemistry)", "Nature"),
    (r"science\s+advances|\bsci\.?\s*adv\b", "Science (AAAS)"),
    (r"journal\s+of\s+the\s+american\s+chemical\s+society|\bjacs\b|"
     r"\bacs\s+|chemistry\s+of\s+materials|nano\s+letters", "ACS"),
    (r"energy\s*&?\s*environmental\s+science|\bees\b|chemical\s+science|"
     r"journal\s+of\s+materials\s+chemistry|nanoscale|green\s+chemistry|"
     r"\brsc\b|chem\.?\s*commun", "RSC"),
    (r"advanced\s+(materials|energy\s+materials|functional\s+materials|"
     r"science|optical)|angewandte|\bsmall\b|solar\s+rrl|infomat|ecomat|"
     r"progress\s+in\s+photovoltaics", "Wiley"),
    (r"\bjoule\b|\bmatter\b|nano\s+energy|solar\s+energy\s+materials|"
     r"cell\s+reports|journal\s+of\s+power\s+sources|applied\s+surface|"
     r"chemical\s+engineering\s+journal", "Elsevier"),
    (r"\bieee\b|journal\s+of\s+photovoltaics", "IEEE"),
    (r"applied\s+physics\s+letters|journal\s+of\s+applied\s+physics|\bapl\b",
     "AIP"),
    (r"\bmdpi\b|\benergies\b|nanomaterials|\bcrystals\b", "MDPI"),
    (r"nano-?micro\s+letters|journal\s+of\s+materials\s+science", "Springer"),
    (r"optics\s+express|\boptica\b", "Optica"),
    (r"\barxiv\b|chemrxiv", "Preprint"),
    (r"\bnature\b", "Nature"),
    (r"\bscience\b", "Science (AAAS)"),
]

DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s,;)\]]+)", re.I)


def detect_journal(text):
    """Publisher family from a paper's first-page text."""
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


def load_journals():
    import json as _json
    if JOURNALS_FILE.exists():
        try:
            return _json.loads(JOURNALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_journals(data):
    import json as _json
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        JOURNALS_FILE.write_text(_json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def scan_journals(progress_cb=None):
    """One batched pass over the index, keeping each paper's earliest text
    chunk, then classifying it. Returns {doc_sig: family}."""
    col = load_collection()
    total = col.count()
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
    return {sig: detect_journal(txt) for sig, (_p, txt) in best.items()}


FILENAME_META = re.compile(r"^(\d+)_([A-Za-z-]+)_(\d{4})_")


def filename_meta(fname):
    """Pull topic + year out of the '1001_solar-cell_2025_Title.pdf' pattern."""
    m = FILENAME_META.match(Path(str(fname)).name)
    if not m:
        return "", ""
    return m.group(2), m.group(3)


def retrieve(question, k, doc_sig=None, where_extra=None):
    col = load_collection()
    clauses = []
    if doc_sig:
        clauses.append({"doc_sig": doc_sig})
    if where_extra:
        clauses.append(where_extra)
    where = (clauses[0] if len(clauses) == 1
             else {"$and": clauses} if clauses else None)

    kwargs = {"query_texts": [question], "n_results": k,
              "include": ["documents", "metadatas", "distances"]}
    if where:
        kwargs["where"] = where
    try:
        res = col.query(**kwargs)
    except Exception:
        if not where:
            raise
        # Some ChromaDB versions fail on filtered semantic queries
        # ("Error finding id"). Fall back: over-fetch without the filter
        # and apply it here instead.
        big_k = min(max(k * 40, 400), 2000, col.count())
        res = col.query(query_texts=[question], n_results=big_k,
                        include=["documents", "metadatas", "distances"])
        hits = [{"text": d, "meta": m, "score": 1 - dist}
                for d, m, dist in zip(res["documents"][0],
                                      res["metadatas"][0],
                                      res["distances"][0])
                if _meta_matches(m, where)]
        return hits[:k]
    return [{"text": d, "meta": m, "score": 1 - dist}
            for d, m, dist in zip(res["documents"][0], res["metadatas"][0],
                                  res["distances"][0])]


def _paper_attrs():
    """One row per paper with every filterable attribute: publisher and
    source folder (chunk metadata), journal family (journals.json), and
    topic/year (the '1001_topic_2025_Title.pdf' filename pattern)."""
    journals = load_journals()
    out = []
    for r in library_table():
        topic, year = filename_meta(r["File"])
        out.append({"sig": r["sig"], "title": r["Title"], "file": r["File"],
                    "publisher": r.get("Publisher", ""),
                    "source": r.get("Source", ""),
                    "journal": journals.get(r["sig"], ""),
                    "topic": topic, "year": year})
    return out


def _unindexed_folders(indexed_sources):
    """Sub-folders of the papers folder that hold documents but are not in
    the index (added after the last indexing run)."""
    out = []
    try:
        base = Path(config.PDF_DIR)
        for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if d.name in indexed_sources:
                continue
            n = sum(1 for f in d.rglob("*") if f.suffix.lower() in (".pdf", ".docx", ".txt", ".md"))
            if n:
                out.append((d.name, n))
        if "root" not in indexed_sources:
            n_root = sum(1 for f in base.iterdir() if f.is_file()
                         and f.suffix.lower() in (".pdf", ".docx", ".txt", ".md"))
            if n_root:
                out.append(("papers (top-level folder)", n_root))
    except Exception:
        pass
    return out


def restrict_search_widget(key):
    """The 'Restrict search' expander, shared by Ask and Draft.

    Returns a where-clause for retrieve() (or None when nothing is
    selected). Journal/topic/year/paper filters resolve to a doc_sig
    list, so they work without those fields being in the chunk metadata.
    """
    papers = _paper_attrs()
    if not papers:
        return None
    pubs = sorted({p["publisher"] for p in papers if p["publisher"]})
    srcs = sorted({p["source"] for p in papers if p["source"]})
    jrns = sorted({p["journal"] for p in papers
                   if p["journal"] and p["journal"] != "Unknown"})
    topics = sorted({p["topic"] for p in papers if p["topic"]})
    years = sorted({p["year"] for p in papers if p["year"]})
    label_to_sig = {f"{p['title'][:80]}  ·  {Path(p['file']).name[:45]}":
                    p["sig"] for p in papers}

    with st.expander("🔎 Restrict search (optional)"):
        c1, c2, c3 = st.columns(3)
        f_pub = c1.multiselect("Publisher", pubs, key=f"{key}_pub")
        f_jrn = c2.multiselect("Journal family", jrns, key=f"{key}_jrn",
                               disabled=not jrns,
                               help="Filled by the journal scan in the "
                                    "Library tab - run it once per index.")
        _cnt = {}
        for p in papers:
            _cnt[p["source"]] = _cnt.get(p["source"], 0) + 1
        _lab = {s: (f"papers (top-level folder) · {_cnt.get(s, 0)}" if s == "root"
                    else f"{s} · {_cnt.get(s, 0)}") for s in srcs}
        _pick = c3.multiselect("Source folder", [_lab[s] for s in srcs],
                               key=f"{key}_src",
                               help="Sub-folders of your papers folder as they were "
                                    "indexed; the number is the count of indexed "
                                    "papers. 'root' = PDFs placed directly in the "
                                    "papers folder.")
        f_src = [s for s in srcs if _lab[s] in _pick]
        _missing = _unindexed_folders(set(srcs))
        if _missing:
            st.warning("On disk but not in the index yet: "
                       + "; ".join(f"{n} ({c} documents)" for n, c in _missing)
                       + " - double-click IndexPapers.command (or run "
                         "`python ingest.py`) and reload, then the folder appears here.")
        c4, c5 = st.columns([1, 1])
        f_top = c4.multiselect("Topic (from filename)", topics,
                               key=f"{key}_top", disabled=not topics)
        f_yr = None
        if len(years) > 1:
            f_yr = c5.select_slider("Year range (from filename)",
                                    options=years,
                                    value=(years[0], years[-1]),
                                    key=f"{key}_yr")
        f_pap = st.multiselect("Specific papers", sorted(label_to_sig),
                               key=f"{key}_pap",
                               help="Type to search; the answer will cite "
                                    "only the selected papers.")

        year_active = f_yr is not None and f_yr != (years[0], years[-1])
        if not any([f_pub, f_jrn, f_src, f_top, f_pap]) and not year_active:
            return None

        want_sigs = {label_to_sig[l] for l in f_pap}
        sel = []
        for p in papers:
            if f_pub and p["publisher"] not in f_pub:
                continue
            if f_jrn and p["journal"] not in f_jrn:
                continue
            if f_src and p["source"] not in f_src:
                continue
            if f_top and p["topic"] not in f_top:
                continue
            if year_active and not (p["year"]
                                    and f_yr[0] <= p["year"] <= f_yr[1]):
                continue
            if want_sigs and p["sig"] not in want_sigs:
                continue
            sel.append(p["sig"])

        if not sel:
            st.warning("No papers match this combination - the filter is "
                       "ignored for this question.")
            return None
        st.caption(f"Searching {len(sel)} of {len(papers)} papers.")
        if len(sel) == len(papers):
            return None
        return {"doc_sig": {"$in": sel}}


def paper_full_text(sig, max_chars=60000):
    """Full extracted text of one indexed paper (text chunks only, page order)."""
    col = load_collection()
    got = col.get(where={"doc_sig": sig}, include=["documents", "metadatas"])
    pairs = [(d, m) for d, m in zip(got["documents"], got["metadatas"])
             if m.get("type") != "figure"]
    pairs.sort(key=lambda x: x[1]["page_start"])
    return "\n\n".join(d for d, m in pairs)[:max_chars]


# ---------------------------------------------------------------------------
# Reference export: cited papers -> BibTeX / RIS / formatted list
# ---------------------------------------------------------------------------
def paper_doi(sig):
    """DOI from a paper's first indexed page, if printed there."""
    try:
        m = DOI_IN_TEXT.search(paper_full_text(sig, 4000))
        return m.group(1).rstrip(".,;") if m else ""
    except Exception:
        return ""


def hits_to_refs(hits):
    """Unique cited papers from a hit list, in citation-number order."""
    journals = load_journals()
    seen, refs = set(), []
    for h in hits:
        m = h.get("meta", {})
        sig = m.get("doc_sig")
        if not sig or sig in seen:
            continue
        seen.add(sig)
        topic, year = filename_meta(m.get("file", ""))
        refs.append({"sig": sig, "title": m.get("title", "?"),
                     "file": m.get("file", ""),
                     "publisher": m.get("publisher", ""),
                     "journal": journals.get(sig, ""),
                     "year": year, "doi": paper_doi(sig)})
    return refs


def _bibkey(r, i):
    words = re.sub(r"[^A-Za-z ]", "", r["title"]).split()
    return f"{(words[0].lower() if words else 'ref')}{r.get('year') or ''}n{i}"


def refs_to_bibtex(refs):
    out = []
    for i, r in enumerate(refs, 1):
        fields = [f"  title = {{{r['title']}}}"]
        if r.get("year"):
            fields.append(f"  year = {{{r['year']}}}")
        pub = r.get("journal") or r.get("publisher")
        if pub:
            fields.append(f"  publisher = {{{pub}}}")
        if r.get("doi"):
            fields.append(f"  doi = {{{r['doi']}}}")
        fields.append(f"  note = {{local file: {r['file']}}}")
        out.append("@article{" + _bibkey(r, i) + ",\n"
                   + ",\n".join(fields) + "\n}")
    return "\n\n".join(out) + "\n"


def refs_to_ris(refs):
    lines = []
    for r in refs:
        lines += ["TY  - JOUR", f"TI  - {r['title']}"]
        if r.get("year"):
            lines.append(f"PY  - {r['year']}")
        pub = r.get("journal") or r.get("publisher")
        if pub:
            lines.append(f"PB  - {pub}")
        if r.get("doi"):
            lines.append(f"DO  - {r['doi']}")
        lines += [f"L1  - {r['file']}", "ER  - ", ""]
    return "\n".join(lines)


def refs_to_list(refs):
    out = []
    for i, r in enumerate(refs, 1):
        bits = [f"**[{i}]** {r['title']}"]
        pub = r.get("journal") or r.get("publisher")
        if pub:
            bits.append(pub)
        if r.get("year"):
            bits.append(r["year"])
        line = ". ".join(bits)
        if r.get("doi"):
            line += f". doi: {r['doi']}"
        out.append(line)
    return "\n\n".join(out)


def crossref_bibtex(doi):
    """Full formal BibTeX (authors, journal, pages) via doi.org."""
    import requests as _rq
    r = _rq.get("https://doi.org/" + doi,
                headers={"Accept": "application/x-bibtex"}, timeout=20)
    r.raise_for_status()
    return r.text.strip()


def render_reference_exporter(hits, key):
    """Expander with .bib/.ris/list downloads for the papers cited in hits."""
    if not hits:
        return
    with st.expander("📚 Export the references for this answer"):
        cache_key = f"{key}_refs"
        if st.session_state.get(cache_key, (None,))[0] is not hits:
            st.session_state[cache_key] = (hits, hits_to_refs(hits))
        refs = st.session_state[cache_key][1]
        if not refs:
            st.caption("No papers to export.")
            return
        st.markdown(refs_to_list(refs))
        c1, c2, c3 = st.columns(3)
        c1.download_button("⬇️ BibTeX (.bib)",
                           data=refs_to_bibtex(refs).encode("utf-8"),
                           file_name="references.bib", mime="text/plain",
                           key=f"{key}_bib", use_container_width=True)
        c2.download_button("⬇️ RIS (.ris)",
                           data=refs_to_ris(refs).encode("utf-8"),
                           file_name="references.ris", mime="text/plain",
                           key=f"{key}_ris", use_container_width=True)
        with c3:
            if st.button("🌐 Formal BibTeX via DOI", key=f"{key}_cx",
                         use_container_width=True,
                         help="Looks each DOI up at doi.org for the complete "
                              "entry (authors, journal, pages). Needs "
                              "internet; takes ~1s per paper."):
                got, miss = [], 0
                bar = st.progress(0.0)
                for i, r in enumerate(refs, 1):
                    bar.progress(i / len(refs), text=r["title"][:50])
                    if r.get("doi"):
                        try:
                            got.append(crossref_bibtex(r["doi"]))
                            continue
                        except Exception:
                            pass
                    miss += 1
                    got.append("% no resolvable DOI - local stub\n"
                               + refs_to_bibtex([r]).strip())
                bar.empty()
                st.session_state[f"{key}_cxbib"] = "\n\n".join(got)
                if miss:
                    st.caption(f"{miss} paper(s) had no resolvable DOI - "
                               "local stub kept for those.")
        if st.session_state.get(f"{key}_cxbib"):
            st.download_button(
                "⬇️ Formal BibTeX (.bib)",
                data=st.session_state[f"{key}_cxbib"].encode("utf-8"),
                file_name="references_formal.bib", mime="text/plain",
                key=f"{key}_cxdl")


# ---------------------------------------------------------------------------
# Evidence re-ranking and deep answers
# ---------------------------------------------------------------------------
RERANK_SYSTEM = """\
You rank literature passages by how useful they are for answering a
question. Respond with ONLY a JSON array of passage numbers (integers),
most relevant first - no other text."""


def rerank_hits(api_key, question, hits, keep):
    """Haiku keeps the `keep` most relevant hits; original order on failure."""
    import json as _json
    if len(hits) <= keep:
        return hits
    listing = "\n\n".join(
        f"[{i}] {h['meta'].get('title', '')[:70]}: {h['text'][:350]}"
        for i, h in enumerate(hits, start=1))
    try:
        raw = call_claude(api_key, RERANK_SYSTEM,
                          f"QUESTION: {question}\n\nPASSAGES:\n\n{listing}"
                          f"\n\nReturn the {keep} best passage numbers.",
                          MODELS["Fast (Haiku 4.5)"], max_tokens=300)
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
        order = _json.loads(raw)
        seen_i, picked = set(), []
        for i in order:
            if (isinstance(i, int) and 1 <= i <= len(hits)
                    and i not in seen_i):
                seen_i.add(i)
                picked.append(hits[i - 1])
            if len(picked) == keep:
                break
        return picked or hits[:keep]
    except Exception:
        return hits[:keep]


DEEP_PLAN_SYSTEM = """\
You decompose a research question into retrieval sub-queries. Respond with
ONLY a JSON array of 3-5 short literature-search phrases that together
cover the question - no other text."""


def deep_answer(api_key, question, answer_mode, model, k, where, do_rerank):
    """Multi-pass answer: map sub-topics, retrieve per sub-topic, then
    synthesize one long cited answer over the merged evidence."""
    import json as _json
    raw = call_claude(api_key, DEEP_PLAN_SYSTEM, f"QUESTION: {question}",
                      model, max_tokens=400)
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        subs = [s for s in _json.loads(raw) if isinstance(s, str)][:5]
    except Exception:
        subs = []
    per_k = max(5, k // 2 + 2)
    registry, all_hits = {}, []
    prog = st.progress(0.0, text="Deep retrieval...")
    queries = [question] + subs
    for si, sq in enumerate(queries, start=1):
        prog.progress(si / (len(queries) + 1), text=f"Retrieving: {sq[:60]}")
        try:
            sh = retrieve(sq, per_k, where_extra=where)
        except Exception:
            sh = []
        if do_rerank and sh:
            sh = rerank_hits(api_key, question, sh, min(per_k, len(sh)))
        for h in sh:
            hkey = (h["meta"]["file"], h["meta"]["page_start"],
                    h["text"][:80])
            if hkey not in registry:
                registry[hkey] = len(registry) + 1
                all_hits.append(h)
    all_hits = all_hits[:40]
    prog.progress(1.0, text="Synthesizing the answer...")
    user_msg = (f"Excerpts:\n\n{build_context(all_hits)}\n\n"
                f"Question: {question}\n\n"
                "(This is a deep, survey-style question: organise the "
                "answer by themes with short headers, cover the excerpts "
                "broadly, and cite [n] throughout.)")
    answer = call_claude(api_key, build_system_prompt(answer_mode),
                         user_msg, model, max_tokens=8000)
    prog.empty()
    return answer, all_hits


MATRIX_SYSTEM = """\
You extract comparison fields from ONE scientific paper. You get a list of
FIELDS and the paper text. Respond with ONLY a JSON object mapping every
field name EXACTLY as given to a concise value string (numbers with units,
key conditions in brackets). Use "-" when the paper does not report the
field. Never invent values."""


# API list prices, $ per million tokens (input, output) - for the sidebar
# cost estimate. Update if Anthropic's pricing changes.
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


def month_spend():
    return float(_load_spend().get(datetime.date.today().strftime("%Y-%m"),
                                   0.0))


def get_budget():
    return float(_load_spend().get("_budget", 0.0))


def set_budget(value):
    data = _load_spend()
    data["_budget"] = float(value)
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
    # Persist real API spend per month (Max mode isn't metered this way).
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


# Models that reason before answering: their replies contain thinking blocks
# as well as text, and the thinking shares the max_tokens budget.
THINKING_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5",
                   "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")


def _effective_max_tokens(model, requested):
    """Give reasoning models headroom so the visible answer isn't truncated
    by the tokens they spend thinking. Capped at 16k to stay under the SDK's
    non-streaming timeout guard."""
    mt = requested or config.MAX_ANSWER_TOKENS
    if any(str(model).startswith(m) for m in THINKING_MODELS):
        mt = max(mt, 32000)
    return mt


def _response_text(resp):
    """Pull the answer out of a Messages response.

    Reasoning models return thinking blocks alongside the answer, so
    content[0] is not necessarily the text - collect the text blocks instead.
    """
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError(
            "Claude declined this request. Try rephrasing it, or choose a "
            "different model in the sidebar.")
    parts = [b.text for b in resp.content
             if getattr(b, "type", "") == "text" and getattr(b, "text", "")]
    if not parts:
        raise RuntimeError(
            "The model returned no text — it may have spent the whole token "
            "budget reasoning. Try a shorter document, or a smaller model.")
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


def build_context(hits, numbers=None):
    nums = numbers or list(range(1, len(hits) + 1))
    return "\n\n---\n\n".join(
        f"[{n}] {h['meta']['title']} ({h['meta']['file']}, "
        f"p.{h['meta']['page_start']}-{h['meta']['page_end']})\n{h['text']}"
        for n, h in zip(nums, hits)
    )


def generate_long_draft(api_key, brief, content_type, target_words, k,
                        model, progress, where=None):
    """
    Multi-section pipeline for long drafts:
      1. Claude plans an outline (sections + a retrieval query each).
      2. Each section retrieves its own evidence and is drafted with
         globally consistent citation numbers.
    Returns (full_text, all_hits) where citation [n] maps to all_hits[n-1].
    """
    import json
    n_sections = max(3, min(12, round(target_words / 900)))
    outline_sys = (
        "You plan scientific review documents. Respond ONLY with a JSON "
        "array (no markdown fences, no prose) of section objects: "
        '[{"title": str, "scope": str, "query": str}]. '
        "The query is a concise literature-search phrase for that section.")
    outline_user = (f"DOCUMENT BRIEF: {brief}\n\n"
                    f"Plan exactly {n_sections} sections that together cover "
                    f"the brief for a {target_words}-word review text. "
                    f"Logical order: context first, synthesis/outlook last.")
    raw = call_claude(api_key, outline_sys, outline_user, model,
                      max_tokens=2000)
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    outline = json.loads(raw)

    registry = {}     # chunk-key -> global citation number
    all_hits = []     # ordered by global number
    parts = []
    words_per = max(200, target_words // len(outline))

    for si, sec in enumerate(outline, start=1):
        progress.progress(si / (len(outline) + 1),
                          text=f"Section {si}/{len(outline)}: {sec['title']}")
        hits = retrieve(sec.get("query") or sec["title"], k,
                        where_extra=where)
        numbers = []
        for h in hits:
            key = (h["meta"]["file"], h["meta"]["page_start"],
                   h["text"][:80])
            if key not in registry:
                registry[key] = len(registry) + 1
                all_hits.append(h)
            numbers.append(registry[key])
        sec_sys = build_draft_prompt("Review paragraph", words_per) + f"""
SECTION TASK
You are writing ONE section of a longer document. Section title:
"{sec['title']}". Scope: {sec.get('scope', '')}.
- Write flowing prose for this section only; do not summarise other
  sections; no introduction or conclusion for the whole document.
- The excerpt numbers are GLOBAL to the whole document - cite them
  exactly as given, do not renumber.
"""
        user_msg = (f"Excerpts:\n\n{build_context(hits, numbers)}\n\n"
                    f"Write the section now (~{words_per} words).")
        sec_text = call_claude(api_key, sec_sys, user_msg, model,
                               max_tokens=min(int(words_per * 2.5) + 500,
                                              8000))
        parts.append(f"## {sec['title']}\n\n{sec_text}")

    progress.progress(1.0, text="Assembling document...")
    return "\n\n".join(parts), all_hits


# --------------------------------------------------------------------------
# Word / Markdown export
# --------------------------------------------------------------------------
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


def _png_width_in(path, default=6.0, max_in=6.3):
    """Embed a generated figure at its true print width (300 dpi PNG)
    clamped to the Word text column, so a single-column figure is not
    blown up and a double-column one still fits."""
    try:
        b = Path(path).read_bytes()[:24]
        if b.startswith(b"\x89PNG"):
            return max(2.0, min(max_in, int.from_bytes(b[16:20], "big") / 300.0))
    except Exception:
        pass
    return default


def md_to_docx(doc, md):
    """Append markdown text to a Document as real Word formatting:
    headings, bold/italic, bullet/numbered lists, and tables - instead of
    raw **, # and | characters."""
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
        m = re.match(r"^!\[(.*?)\]\((.*?)\)$", stripped)
        if m:
            # embedded figure: ![caption](path) -> picture + caption
            img_path = m.group(2).strip()
            if Path(img_path).exists():
                try:
                    doc.add_picture(img_path, width=Inches(_png_width_in(img_path)))
                    cap = doc.add_paragraph()
                    _md_runs(cap, m.group(1).strip())
                    for r_ in cap.runs:
                        r_.font.size = Pt(9)
                        r_.italic = True
                except Exception:
                    _md_runs(doc.add_paragraph(), f"[figure: {img_path}]")
            else:
                _md_runs(doc.add_paragraph(), f"[missing figure file: {img_path}]")
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
            _md_runs(doc.add_paragraph(style="List Number"), m.group(1))
            i += 1
            continue
        _md_runs(doc.add_paragraph(), stripped)
        i += 1


def add_qa_to_doc(doc, qa, number=None):
    heading = f"Q{number}. {qa['question']}" if number else qa["question"]
    doc.add_heading(heading, level=2)
    meta_p = doc.add_paragraph()
    run = meta_p.add_run(f"Asked {qa['time']}  |  {len(qa['hits'])} sources retrieved")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    md_to_docx(doc, qa["answer"])
    doc.add_heading("Sources", level=3)
    for i, h in enumerate(qa["hits"], start=1):
        m = h["meta"]
        doc.add_paragraph(f"[{i}] {m['title']} - {m['file']}, "
                          f"p.{m['page_start']}-{m['page_end']} "
                          f"(similarity {h['score']:.2f})")


def qa_to_docx_bytes(qa_list, title="Literature Q&A Session"):
    doc = Document()
    doc.add_heading(title, level=1)
    doc.add_paragraph(f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} "
                      f"- {len(qa_list)} question(s)")
    for n, qa in enumerate(qa_list, start=1):
        add_qa_to_doc(doc, qa, number=n)
        doc.add_paragraph()
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def qa_to_markdown(qa_list, title="Literature Q&A Session"):
    lines = [f"# {title}",
             f"*Generated {datetime.datetime.now():%Y-%m-%d %H:%M} - "
             f"{len(qa_list)} question(s). Citations [n] refer to the "
             f"source list under each answer.*", ""]
    for n, qa in enumerate(qa_list, start=1):
        lines.append(f"## Q{n}. {qa['question']}")
        lines.append(f"*{qa['time']} - {len(qa['hits'])} sources retrieved*")
        lines.append("")
        lines.append(qa["answer"])
        lines.append("")
        lines.append("**Sources and retrieved passages**")
        for i, h in enumerate(qa["hits"], start=1):
            m = h["meta"]
            lines.append(f"- **[{i}]** {m['title']} - `{m['file']}`, "
                         f"p.{m['page_start']}-{m['page_end']} "
                         f"(similarity {h['score']:.2f})")
            passage = h.get("text", "").strip()
            if passage:
                lines.append("\n".join("  > " + ln for ln in passage.splitlines()
                                       if ln.strip()))
        lines.append("")
    return "\n".join(lines)


def autosave(qa):
    ANSWERS_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", qa["question"])[:50].strip("-").lower()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    doc = Document()
    doc.add_heading("Literature Q&A", level=1)
    add_qa_to_doc(doc, qa)
    doc.save(ANSWERS_DIR / f"{stamp}_{slug}.docx")
    (ANSWERS_DIR / f"{stamp}_{slug}.md").write_text(
        qa_to_markdown([qa], title="Literature Q&A"), encoding="utf-8")


def record_qa(question, answer, hits, do_autosave):
    qa = {"question": question, "answer": answer, "hits": hits,
          "time": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}
    st.session_state.history.append(qa)
    if do_autosave:
        try:
            autosave(qa)
            st.toast("Saved to answers folder (.docx + .md)")
        except Exception as e:
            st.warning(f"Auto-save failed: {e}")
    return qa


def build_table_docx(rows, columns, heading, caption=""):
    """A Word document containing one grid table. Rows whose 'Ref' value is
    '-' (the user's own data) are bolded."""
    doc = Document()
    doc.add_heading(heading, level=2)
    t = doc.add_table(rows=1 + len(rows), cols=len(columns))
    t.style = "Table Grid"
    for j, c in enumerate(columns):
        cell = t.rows[0].cells[j]
        cell.text = str(c)
        for p in cell.paragraphs:
            for run in p.runs:
                run.font.bold = True
    for i, row in enumerate(rows, start=1):
        bold = row.get("Ref") == "-"
        for j, c in enumerate(columns):
            cell = t.rows[i].cells[j]
            cell.text = str(row.get(c, ""))
            if bold:
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.font.bold = True
    if caption:
        doc.add_paragraph(caption)
    return doc


def autosave_extraction(table, kind="extraction", extra_heading=None,
                        extra_text=None):
    """Write a table (and optional analysis text) to the answers folder as
    .docx + .csv. Returns the filename base."""
    import csv as _csv
    ANSWERS_DIR.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    cols = list(table[0].keys())
    doc = build_table_docx(table, cols, kind.capitalize().replace("_", " "),
                           "Auto-saved by Manuscript Workbench.")
    if extra_text:
        doc.add_heading(extra_heading or "Notes", level=3)
        md_to_docx(doc, extra_text)
    doc.save(ANSWERS_DIR / f"{stamp}_{kind}.docx")
    with open(ANSWERS_DIR / f"{stamp}_{kind}.csv", "w", newline="",
              encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(table)
    return f"{stamp}_{kind}"


def save_extraction_state(table, my_rows=None):
    """Persist the current extraction (and lab-data rows) so a restart or
    refresh can restore them instead of losing the work."""
    import json as _json
    try:
        ANSWERS_DIR.mkdir(exist_ok=True)
        (ANSWERS_DIR / "last_extraction.json").write_text(
            _json.dumps({"table": table, "mywork": my_rows},
                        ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def load_extraction_state():
    import json as _json
    p = ANSWERS_DIR / "last_extraction.json"
    if p.exists():
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def show_sources(hits, key_prefix=""):
    with st.expander("Show sources"):
        for i, h in enumerate(hits, start=1):
            m = h["meta"]
            tag = " 🖼️ figure" if m.get("type") == "figure" else ""
            st.markdown(f"**[{i}] {m['title']}**{tag} - {m['file']}, "
                        f"p.{m['page_start']}-{m['page_end']} "
                        f"(similarity {h['score']:.2f})")
            img = m.get("image_path", "")
            if img and Path(img).exists():
                st.image(img, width=450)
            st.write(h["text"])


# ---------------------------------------------------------------------------
# Project workspaces: persistent per-manuscript/proposal folders
# ---------------------------------------------------------------------------
PROJECTS_DIR = ANSWERS_DIR / "projects"
ACTIVE_PROJECT_FILE = PROJECTS_DIR / "_active.txt"


def list_projects():
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))


def active_project():
    try:
        name = ACTIVE_PROJECT_FILE.read_text(encoding="utf-8").strip()
        return name if name in list_projects() else None
    except Exception:
        return None


def set_active_project(name):
    try:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        ACTIVE_PROJECT_FILE.write_text(name or "", encoding="utf-8")
    except Exception:
        pass


def project_files(name):
    d = PROJECTS_DIR / name
    if not d.exists():
        return []
    return sorted((p for p in d.iterdir()
                   if p.is_file() and p.name != "notes.md"),
                  key=lambda p: p.name.lower())


def extract_path_text(path):
    """Text from a stored project file (.docx/.pdf/.txt/.md)."""
    path = Path(path)
    name = path.name.lower()
    if name.endswith(".docx"):
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(p for p in parts if p.strip())
    if name.endswith(".pdf"):
        import fitz
        with fitz.open(str(path)) as doc:
            return "\n\n".join(page.get_text("text") for page in doc)
    return path.read_text(encoding="utf-8", errors="replace")


def project_file_picker(label, key):
    """Selectbox over the active project's files; returns (text, name)
    - ('', '') when nothing picked or no active project."""
    ap = active_project()
    if not ap:
        return "", ""
    files = project_files(ap)
    if not files:
        return "", ""
    opts = ["(none)"] + [p.name for p in files]
    pick = st.selectbox(f"{label} - or pick from project '{ap}'",
                        opts, key=key)
    if pick == "(none)":
        return "", ""
    try:
        return extract_path_text(PROJECTS_DIR / ap / pick), pick
    except Exception as e:
        st.error(f"Could not read {pick}: {e}")
        return "", ""


# ==========================================================================
# Rewrite for a high-impact journal  (Review tab mode)
#
# Staged pipeline: ingest -> evidence ledger (digest) -> editorial plan
# (optional approval checkpoint) -> per-section rewrite -> front matter
# -> mechanical integrity audit -> adversarial referee -> exact-match
# repairs -> assemble + save. Every stage result is kept in
# st.session_state["rw"] and mirrored to answers/rewrite_jobs/<id>/ so a
# crash or refresh loses at most one call.
# ==========================================================================
import json as _rwjson
import hashlib as _rwhash

RW_DIR = ANSWERS_DIR / "rewrite_jobs"
RW_PROFILES_VERIFIED = "2026-08"

JOURNAL_PROFILES = {
    "Nature family (Nature Energy / Materials / Communications)": {
        "types": ["Article"],
        "confidence": "High on structure and abstract style; medium on exact "
                      "word/display-item caps - confirm on the journal's "
                      "current formatting guide.",
        "front_matter": ["Title options", "Abstract"],
        "text": """\
NATURE FAMILY (Nature Energy is the default reading; notes where others differ)
Who reads first: a professional editor (PhD, not necessarily in perovskites) deciding in one read whether to send to review. They read title, abstract, first paragraph, Figure 1 and the Discussion opening. The question is 'does this change what the field can do or believes', not 'is this good work'.
Title: declarative or descriptive noun phrase; no colon-subtitles, abbreviations, question marks or superlatives; Nature flagship ~75 characters, sister journals similar in spirit; Nature Communications <= ~15 words.
Abstract: ONE unreferenced paragraph ~150 words (Nature Energy / Materials / Communications). ONLY the Nature flagship uses a ~200-word REFERENCED summary paragraph. Five-sentence compression of the Nature template: (1) context any scientist understands; (2) the specific problem; (3) 'Here we show' with the key number AND its qualifier (certified / stabilised / champion / n); (4) what it reveals or the mechanism; (5) the consequence for the field. No citations, no undefined abbreviations.
Main text: Nature Energy / Materials have NO 'Introduction' heading - 2-4 introductory paragraphs (~500-700 words) run directly after the abstract and end with the 'Here we ...' paragraph. Nature Communications DOES print 'Introduction' / 'Results' / 'Discussion' / 'Methods' headings. Results under short claim-shaped subheadings (one line, <= ~60 characters, no figure references). A 'Discussion' heading is usual at Nature Energy; no Conclusions section.
Length: ~3,000-5,000 words of main text (excluding abstract, Methods, references, legends); up to ~6-8 display items; ~50 references (Nature Communications is more permissive: ~5,000+ words, up to ~10 display items, no strict reference cap).
Methods: separate section AFTER the main text, before references, sub-headed (Device fabrication / Characterisation / Stability testing / Statistics), then 'Data availability'. Nature Energy's solar-cell Reporting Summary requires: mask/aperture area, scan direction and rate, stabilised (MPP) efficiency, number of devices behind every statistic, certification status and lab, ISOS protocol names and T80/T90 definitions. 'Record' language without certification is a red flag.
Supplementary: 'Supplementary Fig. 1 / Table 1 / Note 1'; Extended Data figures (up to 10, peer-reviewed) are the natural home for promoted statistics and controls.
Voice: 'we', active; present tense for established facts, past for what was done. Avoid 'novel', 'first', 'remarkably', 'in this work'.
Editors reward: a conceptual advance in one sentence (mechanism, design principle, limit removed) demonstrated on devices with statistics and named-protocol stability; generality (compositions, batches, areas); a Figure 1 a non-specialist can read as the whole story; honest limits in the Discussion.
Desk-rejected: additive/passivation studies framed around a champion PCE; 'first report of X'; characterisation catalogues; Discussions that summarise; abstracts with no consequence sentence.""",
    },
    "Science / Science Advances": {
        "types": ["Science Research Article", "Science Advances Research Article"],
        "confidence": "High on the 125-word abstracts and general-reader "
                      "framing; medium on Science length caps (they are "
                      "negotiated and have changed) - confirm on the current "
                      "information-for-authors page.",
        "front_matter": ["Title options", "Abstract", "One-sentence summary"],
        "text": """\
SCIENCE (Research Article; Science no longer publishes separate Reports)
Audience: the abstract and first paragraphs must be understood by a scientist in any discipline; editors and the Board of Reviewing Editors triage for broad interest first.
Title: short, specific, no jargon; declarative titles common.
Abstract: ONE paragraph, <= 125 words, unreferenced, no undefined abbreviations: context; problem; 'We show that ...' with the key number; mechanism; consequence. Plus a 'One-sentence summary' <= 125 characters.
Main text: very compact - roughly 4,500 words INCLUDING references, notes and legends for the standard form (longer forms are negotiated with the editor); unheaded 2-3 paragraph introduction, a few short finding-style subheadings, brief (often merged) Discussion; <= ~6 figures/tables; ~40-50 references numbered in order, cited in parentheses (12). Catalogue characterisation goes entirely to the Supplementary Materials with one-clause pointers ('fig. S3', lower-case for supplementary).
Methods: 'Materials and Methods' in the Supplementary Materials; main text carries at most a one-paragraph summary where it matters for the argument.
SCIENCE ADVANCES (Research Article): same values, conventional structure with capitalised headings: 'INTRODUCTION', 'RESULTS' (subheads), 'DISCUSSION', 'MATERIALS AND METHODS' in the main text; abstract also <= 125 words; length far more permissive (typical strong papers 5,000-8,000 words, 6-8 figures).
Voice: first person plural, active, declarative; few hedges but every one precise; US spelling.
Editors reward: one result a non-specialist can restate in a sentence and that changes an accepted picture; quantitative comparison with the previous best as stated by the authors; a mechanism tested by a discriminating experiment that could have failed.
Rejected: perovskite device papers whose headline is an efficiency number; long literature-review introductions; abstracts that need the field's jargon; mechanism from one spectroscopic technique.""",
    },
    "Cell Press (Joule / Matter)": {
        "types": ["Article"],
        "confidence": "High on the Cell Press front-matter set and section "
                      "names; medium on exact character/word caps - confirm "
                      "on the current author guide.",
        "front_matter": ["Title options", "Summary", "Context & Scale (Joule) / Progress and Potential (Matter)", "Highlights", "eTOC blurb", "Keywords"],
        "text": """\
CELL PRESS (Joule default; Matter notes where different)
Identity: Joule's in-house scientific editors want an advance connected to a system-level 'so what' (scale, cost, deployment, techno-economics); a materials-only result reads under-framed, unsupported scale claims read over-framed. Matter wants the materials-science mechanism first.
Front matter (all required): Title - specific, declarative fragment, no superlatives. 'Summary' (the abstract): ONE paragraph <= ~150 words, unreferenced, for a broad energy readership: context; problem; result with the key number; insight; implication. 'Context & Scale' (Joule) / 'Progress and Potential' (Matter): a separate lay paragraph <= ~150 words - why the problem matters at scale (TW deployment, LCOE, lifetime, manufacturing), what this contributes, what remains; must NOT repeat the Summary sentence-for-sentence and must not introduce numbers absent from the sources ([AUTHOR: ...] for scale figures). 'Highlights': 3-4 bullets, each <= 85 characters including spaces, results not topics, present tense. 'eTOC blurb': ~50 words, third person ('Krishna et al. show that ...'). Keywords 5-10.
Body: 'Introduction' (headed, 3-5 paragraphs); 'Results' with claim-shaped subheadings; a genuine separate 'Discussion' (Joule accepts 'Results and Discussion' for shorter papers); no standard Conclusions - fold it into the Discussion. Then 'Experimental Procedures' (Joule and Matter keep this heading - not STAR Methods) opening with a 'Resource availability' block (Lead contact / Materials availability / Data and code availability), then technique subsections; 'Supplemental information', 'Acknowledgments', 'Author contributions' (CRediT), 'Declaration of interests' (mandatory), references (numbered, superscript in text).
Length: no hard cap; typical Joule Articles 4,500-7,000 words of main text with up to ~7 display items. Supplemental items cited as 'Figure S3', 'Table S1', 'Note S2'; main-text panels as 'Figure 2B' (capital panel letters). Voice: 'we', active; US spelling.
Editors reward: a line from mechanism to device to a deployment-relevant metric (ISOS-named stability with T80 at stated conditions, module areas, outdoor data, cost/energy-yield modelling); reproducibility statistics in the main text (box plots with n and batches); honest Context & Scale.
Rejected: champion-cell papers with commercialisation boilerplate; missing stability protocol details; Summary and Context & Scale that duplicate each other; Highlights that are topics.""",
    },
    "RSC Energy & Environmental Science": {
        "types": ["Paper", "Communication"],
        "confidence": "High on the Broader context requirement and mandatory "
                      "headings; medium on length guidance (no hard limit).",
        "front_matter": ["Title options", "Abstract", "Broader context"],
        "text": """\
RSC ENERGY & ENVIRONMENTAL SCIENCE (Paper default; Communication = short, urgent)
Identity: rigorous science AND significance for the energy/environment community, argued in a dedicated paragraph; a strong culture of quantitative benchmarking and PV reporting standards (stabilised efficiency, ISOS aging, statistics); referees are device specialists.
Title: informative, may be declarative; no undefined abbreviations; RSC dislikes 'novel'.
Abstract: single unreferenced paragraph ~150-250 words: problem, approach, key quantitative outcomes (the three or four numbers that carry the headline), significance.
'Broader context': REQUIRED, ~100-200 words after the abstract, for a general readership - the energy/environmental challenge, why this problem matters within it, what this work changes, what it enables (deployment, cost, sustainability, lead/tin, recyclability where relevant); must not repeat the abstract.
Body: 'Introduction'; 'Results and discussion' (COMBINED is the RSC norm; claim-shaped subheadings read as strong); 'Conclusions' (expected, ~100-200 words: the advance with its key numbers and the outlook); 'Experimental' (after the Conclusions or largely in the ESI with a short summary); then 'Author contributions', 'Conflicts of interest' (mandatory heading), 'Data availability' (mandatory), 'Acknowledgements', 'Notes and references' (superscript numeric).
Length: Papers have no strict cap (typical 5,000-8,000 words, 5-8 figures); Communications ~4 journal pages (~2,500-3,000 words, 3-4 figures) and must justify urgency. Supplementary = 'ESI', cited as 'Fig. S3 (ESI†)'. Units: 'mA cm-2', 'cm2'. Spelling: keep the manuscript's, consistently.
Editors reward: mechanistic insight tied to device physics with explicit benchmarking against prior energy literature (only comparisons the authors already make); statistics and named-ISOS stability; techno-economic/sustainability framing in the Broader context; honest Conclusions.
Rejected: incremental additive/interface reports without mechanism or statistics; characterisation-catalogue 'Results and discussion'; generic Broader context ('energy demand is rising'); missing stabilised-PCE and area details.""",
    },
    "Wiley Advanced Materials / Advanced Energy Materials": {
        "types": ["Research Article"],
        "confidence": "High on the numbered-section structure, Experimental "
                      "Section at the end, ToC entry and mandatory statements; "
                      "medium on abstract length (no hard cap).",
        "front_matter": ["Title options", "Abstract", "Keywords", "Table-of-contents text"],
        "text": """\
WILEY ADVANCED MATERIALS / ADVANCED ENERGY MATERIALS (Research Article; the Communication type was retired)
Identity: in-house editors triage for novelty of the materials concept and breadth of interest; AEM adds a credible energy-device outcome (stabilised, statistical, stability-tested). Device-record narratives are acceptable only with a materials insight and reproducibility.
Title: concise, specific, no superlatives, no abbreviations.
Abstract: single paragraph <= ~200 words, unreferenced, no undefined abbreviations, written in the PRESENT TENSE and impersonal/third-person style ('X is demonstrated to ...', 'Here, X is shown to ...') - a past-tense 'we showed' abstract is a known desk-rejection signal. Shape: problem; concept; key results with numbers; mechanism; implication. Keywords: 3-5, lower-case, alphabetical. Table-of-contents entry: 50-60 words, present tense, third person, self-contained, plus '[AUTHOR: supply ToC graphic]'.
Body, numbered: '1. Introduction' (3-5 paragraphs, last one 'Here, ...'); '2. Results and Discussion' (combined; numbered sub-sections 2.1, 2.2 ... may be claim-shaped; each opens with the point of the experiment); '3. Conclusion' (short: the advance, two or three numbers, the outlook); '4. Experimental Section' at the END with italic run-in labels ('Materials:', 'Device Fabrication:', 'Characterization:') and a 'Statistical Analysis' subsection; then 'Supporting Information' statement, 'Acknowledgements', 'Conflict of Interest', 'Data Availability Statement', 'Keywords', references ([12] superscript brackets).
Length: no strict cap (typical 5,000-8,000 words, 5-8 figures). Figures 'Figure 2b'; supplementary 'Figure S3, Supporting Information' (phrase repeated at each citation). Spelling US; units 'mA cm-2', 'wt%'.
Editors reward: a transferable materials concept (look for a generality beat: other compositions, stacks); box-plot statistics over >= 20 devices; a ToC graphic that shows the concept; large-area/module data strengthens AEM.
Rejected: champion-cell reports with routine characterisation; missing statistics; efficiency values inconsistent between abstract and body; past-tense 'we' abstracts with citations; overuse of 'novel' / 'for the first time'.""",
    },
    "ACS Energy Letters / JACS": {
        "types": ["ACS Energy Letters Letter", "JACS Article", "JACS Communication"],
        "confidence": "High on the two journals' identities and the ACS TOC "
                      "graphic; medium on exact word/display caps - confirm "
                      "on the current author guidelines.",
        "front_matter": ["Title options", "Abstract"],
        "text": """\
ACS ENERGY LETTERS (Letter)
Identity: fast, high-impact energy results; editors are active researchers; a Letter has ONE sharp finding with a clear energy consequence and a TOC graphic that tells it. Expert referee pool expecting reporting standards (stabilised PCE, aperture area, hysteresis, statistics, ISOS-named stability).
Title: descriptive, specific, no undefined abbreviations. Abstract: one paragraph ~150 words, unreferenced, results-forward (key numbers allowed), ending with the significance.
Format: NO formal Results/Discussion headings - a single argument (introduction paragraphs -> results paragraphs -> a closing paragraph of implications) with the figures carrying the structure; brief bold run-in subheadings acceptable but few. Length ~3,000 words INCLUDING abstract and captions, <= 5 figures/tables, ~40-50 references (numbered, superscript). Methods entirely in the Supporting Information with an 'Experimental Methods' pointer; text cites 'Figure S3'. Back matter: 'ASSOCIATED CONTENT', 'AUTHOR INFORMATION', 'ACKNOWLEDGMENTS', 'REFERENCES'. If the manuscript needs > 5 figures or > 3,500 words the plan must demote content to SI or flag that it is too long for a Letter.
JACS (Article or Communication)
Identity: a CHEMISTRY journal - the headline must be a chemical insight (bonding, coordination, reaction mechanism, molecular design rule, structure-property relationship); the device metric is the demonstration, not the claim. Efficiency-first perovskite papers are rejected regardless of the number; papers succeed when a chemical idea is proven by discriminating experiments (spectroscopy plus a chemical control, ligand-series logic, computation).
Article: 'Abstract' (<= 250 words), 'INTRODUCTION', 'RESULTS AND DISCUSSION' (combined is the norm) with descriptive subheadings, 'CONCLUSIONS', 'EXPERIMENTAL SECTION' (main text or SI with a statement), 'ASSOCIATED CONTENT', 'AUTHOR INFORMATION', 'ACKNOWLEDGMENTS', 'REFERENCES'; no hard cap (typical 6,000-9,000 words). Communication: ~4 journal pages (~2,000-2,500 words), no headings, <= 4 display items, brief abstract; closing paragraph states the chemical principle and its scope. TOC graphic required for both. Voice: 'we', active, past tense for what was done; US spelling.
Rejected: ACS Energy Letters - characterisation catalogues, champion-only PCE, 'record' without certification. JACS - energy-device papers whose chemistry is incidental; mechanism from one technique; Results written as a device paper.""",
    },
    "Generic high-impact journal": {
        "types": ["Article"],
        "confidence": "Deliberately generic; the plan must tell the author "
                      "to confirm the target journal's formatting.",
        "front_matter": ["Title options", "Abstract", "Keywords"],
        "text": """\
GENERIC HIGH-IMPACT PROFILE (target undecided or not listed)
Title <= 15 words, no abbreviations, no superlatives. Abstract: single unreferenced paragraph <= 200 words: context, problem, approach, 'Here we ...', three or four key numbers with their qualifiers, mechanism, implication. Keywords 4-6.
Structure: 'Introduction' (3-5 paragraphs ending in a 'Here we ...' paragraph with the key outcomes); 'Results' with 4-6 claim-shaped subheadings; 'Discussion' (what is new, what it explains, limits, what it enables - no summary); short 'Conclusions' (<= 150 words); 'Methods' at the end with subheadings, then data availability. Main text ~4,000-6,000 words, 6-8 display items, references numbered in order of citation, supplementary cited as 'Supplementary Fig. S3'.
Voice: 'we', active, present tense for established facts, past for what was done; US spelling; SI units with spaces ('mA cm-2').
Photovoltaic reporting standards apply regardless of journal: aperture/mask area, scan protocol, stabilised (MPP) efficiency, number of devices behind each statistic, certification status, ISOS protocol names and T80/T90 definitions for stability. The plan still picks one headline, orders Results as an argument, promotes SI statistics into the main text and strikes unsupported superlatives.""",
    },
}

RW_ROLES = ["Supplementary Information", "Data table (CSV/XLSX)",
            "Referee reports", "Cover letter", "Data notes (constraints)",
            "Previous version", "Other"]
RW_EVIDENCE_ROLES = {"Supplementary Information", "Data notes (constraints)",
                     "Data table (CSV/XLSX)"}

RW_LEVELS = {
    "Conservative (keep section order, sharpen within it)": "conservative",
    "Standard (reorder Results into an argument; promote/demote)": "standard",
    "Bold (may reframe the headline if the evidence points elsewhere)": "bold",
}

# ---------------------------------------------------------------- prompts
RW_DIGEST_SYSTEM = """\
You are the scientific assistant to the handling editor of a top energy-materials journal. Build an EVIDENCE LEDGER for a research manuscript (perovskite photovoltaics or any experimental physical science). Later stages will rewrite the paper from this ledger and are FORBIDDEN from using any number that is not in it, so the ledger must be exhaustive and verbatim about quantities. This is an audit document, not prose.

ABSOLUTE RULES
- Copy every quantitative statement VERBATIM: value, uncertainty, unit, sample size, conditions. Never round, convert, average or compute. '24.83 +/- 0.21%' stays exactly that.
- Preserve citation markers exactly as they appear ([12], ref. 12, superscript written as ^12, (Author et al., 2021)). Never add citations.
- Separate what was MEASURED from what was CLAIMED (a claim is any sentence whose truth is not read directly off a plot).
- Record internal inconsistencies (abstract 25.1% vs Results 25.06%) and flag them.
- Never omit a result because it looks minor; editors notice dropped controls.
- Terse note form. If this is part k of n, prefix every ID with 'P{k}-' and only inventory the text given to you.

STRENGTH TAGS: CERTIFIED / STAT(n=k) / CHAMPION / QUAL / INFERRED / SI-ONLY.

OUTPUT (markdown; keep headings exactly; ID prefixes are parsed by code)
# EVIDENCE LEDGER
## 1. STUDY IN ONE PARAGRAPH  (<= 150 words, neutral)
## 2. HEADLINE CANDIDATES (ranked)  H1..H4, each: one sentence as the authors would defend it; supporting evidence IDs; one-line verdict on defensibility at the recorded strength.
## 3. QUANTITATIVE RESULTS  one line per distinct quantitative statement:
E-ID | what was measured | value(s) verbatim incl. unit, uncertainty, n | conditions (area/mask, scan direction & rate, illumination, ISOS protocol, atmosphere, temperature, encapsulation, time) | where (section, Fig./Table) | strength tag
Include PCE/Voc/Jsc/FF (forward and reverse), stabilised/MPP values, hysteresis, EQE-integrated Jsc, T80/T90, aging conditions, areas, statistics, spectroscopy numbers, structural numbers, compositions, concentrations, process temperatures/times, costs - anything with a unit or a percent sign.
## 4. FIGURES AND TABLES  F-ID | label as in text | panels and what each shows | evidence IDs | ARGUMENT-CRITICAL / SUPPORTING / CATALOGUE
## 5. MECHANISTIC AND INTERPRETIVE CLAIMS  C-ID | claim | evidence IDs and techniques | DIRECT / INDIRECT / SPECULATIVE | exact hedge wording used ('suggests', 'we propose', 'demonstrates') | where
## 6. COMPARISONS TO PRIOR WORK (as stated)  P-ID | what is compared | prior value/statement | citation marker exactly as written | where
## 7. METHODS ESSENTIALS  M-ID | item (stack, precursors, deposition, treatments, encapsulation, J-V protocol, MPP protocol, stability protocol, statistics, instruments) | details verbatim where numeric | where
## 8. WEAK SPOTS A REFEREE WILL NOTICE  numbered; include the PV-specific checks: EQE-integrated Jsc vs J-V Jsc mismatch > 5%; Voc deficit vs bandgap plausibility; FF > ~85% unexplained; forward/reverse or hysteresis index missing; unencapsulated devices in humidity aging; stability quoted without initial PCE, temperature and light source; module efficiency without aperture-area definition; 'certified' without the lab named; champion-only reporting; unstated device count; mechanism from one technique; missing control; SI evidence stronger than the main text; abstract-body inconsistency.
## 9. TERMINOLOGY  abbreviations and composition notation exactly as defined
## 10. CITATION INVENTORY  style observed; highest reference number seen; malformed markers. Do NOT list references.
## 11. CONTEXT FROM OTHER FILES (only if provided)  K-ID | file and role | content: cover letter -> the significance the authors claim; referee reports -> each distinct concern, severity, whether the manuscript already addresses it (evidence IDs); data notes -> constraints the rewrite must honour; previous version -> what changed.
Finish with the line <<<END LEDGER>>>. Never economise on sections 3, 5 and 7."""

RW_SI_SYSTEM = """\
You are inventorying the SUPPLEMENTARY INFORMATION of a research manuscript so a later rewrite can promote the SI's strongest evidence into the main text. You receive one SI chunk and, for orientation, the headline candidates and weak spots from the main-text ledger.
RULES: every quantity VERBATIM (value, unit, uncertainty, n, conditions); keep supplementary labels exactly (Fig. S3b, Table S2, Note S4); never add citations; note form. If this is chunk k of n, prefix IDs with 'S{k}-'.
OUTPUT (keep headings exactly)
# SI LEDGER
## S3. QUANTITATIVE RESULTS IN THE SI  SE-ID | what was measured | value(s) verbatim | conditions (n, area, protocol, atmosphere, time) | where | strength tag
## S4. SI FIGURES AND TABLES  SF-ID | label | panels and content | evidence IDs | CONTROL / STATISTICS / REPRODUCIBILITY / STABILITY / MECHANISM / CATALOGUE
## S7. METHODS DETAILS THAT LIVE ONLY IN THE SI  SM-ID | item | details verbatim where numeric | where
## S12. BURIED STRONG EVIDENCE  numbered: the SI item; why it is stronger than the corresponding main-text statement (e.g. 'main text quotes a champion 25.1%; Table S3 gives 24.4 +/- 0.3% over n=32 devices from 4 batches'); which headline candidate or weak spot it repairs; recommendation PROMOTE TO MAIN TEXT / CITE FROM MAIN TEXT / LEAVE.
## S13. SI CONTRADICTIONS OR GAPS  anything contradicting the main text; main-text claims whose supporting SI item does not show what is claimed.
Finish with <<<END LEDGER>>>."""

RW_PLAN_SYSTEM = """\
You are a senior handling editor at {JOURNAL}. The difference between a manuscript you desk-reject as 'sound but incremental' and one you send to referees as 'this could be important' is rarely the data: it is whether the authors found the one claim their data can carry, built an argument in which every section is a load-bearing step towards it, and wrote it so that an editor outside their subfield grasps the advance in the first 200 words.
You receive the EVIDENCE LEDGER, the SI LEDGER, a code-generated SECTION MAP of the original text (IDs SEC1..), the authors' preferences, optional corpus passages, and the TARGET JOURNAL PROFILE. Produce the EDITORIAL PLAN a rewriting team executes section by section. You decide what the paper is; you do not write it here.

EDITORIAL PRINCIPLES (cite ledger IDs for every decision)
1. One headline the evidence can carry at the recorded strength tags. A CHAMPION-only efficiency is not a headline for this journal unless STAT or CERTIFIED evidence backs it; a mechanism supported by one INDIRECT technique is a proposal, not a demonstration. If H1 is not defensible, say so and choose the claim that is. The headline is stated in the same words in: the title, the 'Here we show' sentence, the 'Here we' paragraph closing the introduction, and the first sentence of the Discussion.
2. The gap is conceptual, not bibliographic: a limit the field is stuck at and WHY, not 'nobody has combined A with B'.
3. Results are an argument: (i) the problem made quantitative; (ii) mechanism or design principle; (iii) demonstration on devices; (iv) generality/robustness; (v) stability under stated protocols. Never keep chronological order just because the experiments happened that way.
4. Subheadings are claims ('Fluoride passivation suppresses iodide migration'), not techniques.
5. Promote, demote, cut: promote SI evidence stronger than the main-text version (statistics, controls, reproducibility, stability), keeping supplementary labels; demote CATALOGUE characterisation to one sentence with an SI pointer; cut what does not advance the headline. Every headline-tagged ledger item is either assigned to a section or listed as DROPPED with a reason; negative results (however / decrease / lower / degradation / hysteresis / loss) may never be dropped silently.
6. Significance is comparative and quantitative using ONLY numbers in the ledger; state-of-the-art comparisons must be P-IDs or become [AUTHOR: ...] requests.
7. Strike 'novel', 'unprecedented', 'first', 'record', 'breakthrough', 'remarkable', 'excellent' unless the supporting item is CERTIFIED or the authors justify them explicitly. A certified efficiency may be called certified; an uncertified one may not be called a record.
8. Discussion is not a summary: what the field now knows; what this explains elsewhere; honest limits from ledger section 8 stated before a referee does, each with the next experiment; what it enables.
9. Methods are not rewritten; a METHODS POINTER lists the M-IDs the main text depends on and any protocol detail that must move into the main text.
10. Honour every K-ID constraint and address every referee concern (or say why not).
11. Never invent data, citations or comparisons; what the paper needs but lacks becomes an author decision.
12. LIBRARY PASSAGES: when passages from the author's own corpus are supplied, numbered [L1], [L2], ..., use them to locate the manuscript in the field - the closest precedents and competitors, the origin of the 'limit', the comparison values (verbatim, with conditions and the [Ln]) the Discussion may use - and flag every novelty or priority claim a library paper contradicts. Assign to each section the library_ids it may cite. Never cite a paper from memory; only supplied [Ln] passages.

TARGET JOURNAL PROFILE
{PROFILE}
ARTICLE TYPE: {ARTICLE_TYPE}

OUTPUT (markdown; keep headings exactly; the fenced json block is parsed by code and MUST be valid JSON)
# EDITORIAL PLAN
## 1. THE HEADLINE  one sentence; the evidence IDs and strength tags that carry it; why this and not the rejected H-IDs.
## 2. WHY IT MATTERS  <= 120 words: the conceptual gap; becomes the spine of the introduction.
## 3. TITLE OPTIONS  3-5, within the journal's limits, recommended one marked; no abbreviations, no superlatives.
## 4. STORY SPINE  5-7 beats, one sentence each, tied to evidence IDs.
## 4b. POSITIONING MAP  (only when library passages are supplied) closest precedents [Ln] and what each showed; what this manuscript adds beyond each; comparison values from the passages (value | condition | [Ln]) usable in the Discussion; novelty claims a library paper contradicts.
## 5. SECTION MANIFEST
```json
{"headline": "...", "title_recommended": "...", "title_options": ["..."], "total_target_words": 4200,
 "sections": [
  {"id": "S1", "kind": "intro", "heading": "Introduction", "target_words": 650, "source_ids": ["SEC1","SEC2"], "evidence_ids": ["P1","E3"], "si_promote": [], "cut_or_demote": [], "brief": "2-3 sentences on what this section must achieve", "first_use_abbreviations": ["PCE"], "library_ids": ["L2", "L5"]},
  {"id": "S2", "kind": "results", "heading": "<claim-shaped subheading>", "target_words": 700, "source_ids": ["SEC4"], "evidence_ids": ["E1","E2","F1"], "si_promote": ["SE4"], "cut_or_demote": ["XRD catalogue paragraph"], "brief": "...", "first_use_abbreviations": []},
  {"id": "S6", "kind": "discussion", "heading": "Discussion", "target_words": 500, "source_ids": ["SEC9"], "evidence_ids": [], "si_promote": [], "cut_or_demote": [], "brief": "...", "first_use_abbreviations": []},
  {"id": "S7", "kind": "methods_pointer", "heading": "Methods (pointer)", "target_words": 150, "source_ids": ["SEC10"], "evidence_ids": ["M1"], "si_promote": [], "cut_or_demote": [], "brief": "...", "first_use_abbreviations": []}
 ],
 "dropped": [{"id": "E9", "reason": "..."}]}
```
Manifest rules: kinds are exactly intro | results | discussion | conclusions | methods_pointer (use 'conclusions' only if the profile has one; the front matter is written by a later stage - do NOT include a front section). source_ids come ONLY from the SECTION MAP; a results section may draw on several SEC ids and a SEC id may feed several sections. At most 6 results sections; no section above 1,200 target words; the sum respects the journal's length. evidence_ids list every E/SE/C/F/SF/M/P ID the section may use; library_ids lists the [Ln] library passages the section may cite (only when passages were supplied; intro and discussion mainly).
## 6. ABSTRACT SKELETON  the journal's abstract shape filled sentence by sentence with evidence IDs (not prose), then each front-matter item the journal needs with a one-line brief.
## 7. PROMOTE / DEMOTE / CUT LEDGER  item | action | editorial reason | consequence for figures
## 8. PRE-EMPTION OF WEAK SPOTS  for each ledger-section-8 item and each referee concern: how the rewrite handles it honestly (reword, caveat, SI pointer, or leave and flag). Never 'hide'.
## 9. STYLE CARD  hedges to keep (from ledger section 5) with the permitted verbs for asserted/hedged/speculative claims; superlatives to strike; terminology to standardise; tense/person/spelling from the profile; citation style to preserve; forbidden inferences (tempting sentences the ledger does not support).
## 10. DECISIONS ONLY THE AUTHOR CAN MAKE  numbered (certification status, device counts, benchmark values, permission to move figures, missing protocol names, which of the 'verify' items in the profile to confirm).
Finish with <<<END PLAN>>>."""

RW_SECTION_SYSTEM = """\
You are the rewriting editor executing ONE section of an EDITORIAL PLAN for a manuscript targeted at {JOURNAL} ({ARTICLE_TYPE}). The handling editor has decided the headline, spine and manifest; write this one section to that plan at the quality of the best papers that journal publishes, without changing the science by one decimal place.

INVIOLABLE INTEGRITY RULES (output is audited mechanically and by an adversarial referee)
1. Every number, unit, uncertainty, sample size and condition you write must appear VERBATIM in the ledger or the original text supplied. Never round, convert, average, subtract, or compute percentages or fold-changes; if a relative improvement is useful and not stated in the sources, write [AUTHOR: state the relative improvement] instead.
2. Do not add, drop or weaken any quantitative result assigned to this section. A demoted item becomes a one-clause pointer ('phase purity was confirmed by XRD (Supplementary Fig. S2)'); never delete silently.
3. What was measured, on what, under what protocol never changes: stabilised stays stabilised, reverse-scan stays reverse-scan, champion stays champion (and is never presented as typical), in-house never becomes certified. The qualifier, n, uncertainty and condition travel with the number at its first mention.
4. Preserve epistemic strength: INDIRECT and SPECULATIVE claims keep a hedge of the original strength ('suggests', 'is consistent with', 'we propose'); DIRECT claims may be stated plainly. Obey the style card's hedge map and forbidden inferences.
5. Citations: markers that exist in the original text, in the original style, attached to the statements they originally supported (a statement may move together with its marker). In addition, when LIBRARY PASSAGES are supplied you may cite them as [Ln] exactly as numbered - only for positioning, precedent and comparison statements the passage genuinely supports (a literature value is quoted verbatim with its condition and its [Ln]); never to support the manuscript's own results, never for a paper not supplied. Never create any other marker; where support is missing write [AUTHOR: add reference for ...].
6. Figure and table labels stay as in the original; a promoted SI figure keeps its SI label plus, once, [AUTHOR: consider moving Fig. S9 to the main text]. Never renumber.
7. Anything the sources lack becomes [AUTHOR: <precise request>]. Use them freely.
8. Never write 'novel', 'unprecedented', 'for the first time', 'record', 'breakthrough', 'remarkable', 'excellent', 'outstanding', 'superior' unless the style card permits the specific instance.
9. Terminology, abbreviations and composition notation as the ledger defines them; define each abbreviation only where the manifest says this section is its first use.

EDITORIAL CRAFT
- Results paragraph architecture: first sentence = the claim in plain words; then the evidence naming the figure panel and the number; then one or two sentences of interpretation tied to the headline; last sentence = the bridge to the next question. A reader of first sentences only must get the whole argument. One idea per paragraph, 80-180 words; no paragraph that is a list of characterisation results.
- Verbs carry the argument ('suppresses', 'accounts for', 'rules out', 'sets'), not 'was investigated', 'it can be seen that'. Active voice and 'we' where the profile allows; no 'it is worth noting', 'interestingly', 'in this work'.
- Numbers stated once with full precision. Every figure referenced is needed by the argument at that point, introduced as evidence for a claim, never 'Figure 3 shows'. Transitions state logic, not sequence. Respect target_words +/- 15%; never pad.
- intro: 3-5 paragraphs. Paragraph 1: the field-level stake in two sentences a non-specialist editor understands, then the specific limit the field is stuck at. Middle: the conceptual gap (why the limit exists; what existing approaches trade off; only P-ID or supplied [Ln] comparisons). Final paragraph: 'Here we ...' with the story spine and the key numbers. Never open with 'Perovskite solar cells have attracted tremendous attention'.
- results: heading is the manifest's claim; open with one sentence saying what question this section answers and why it comes here; close with a one-sentence bridge. Promote SI evidence exactly as the manifest says.
- discussion: no summary. Paragraph 1 opens with the headline as now established (same words as the plan) and what the field now knows. Paragraph 2: what this explains elsewhere and how it compares (P-IDs, supplied [Ln] passages with verbatim values, or [AUTHOR: ...]). Paragraph 3: honest limits with the concrete next experiment for each. Final paragraph: what this enables, quantified only with ledger numbers; no 'pave the way'.
- conclusions (only if the profile has one): <= 150 words - headline, the two or three numbers that carry it, the one caveat, the one consequence.
- methods_pointer: one paragraph listing by M-ID the protocol details the main text depends on and where they must appear, plus [AUTHOR: ...] for anything the ledger lacks (ISOS designation, mask area, MPP duration, device count, certification lab). Do not rewrite the Methods.

CONTINUITY: you receive the tail of the previously written section; do not repeat it, pick up its bridge.

OUTPUT (exact; code parses the delimiters; nothing before the first or after the last)
<<<SECTION>>>
## <heading>
<prose>
<<<END SECTION>>>
<<<CHANGES>>>
5-12 bullets: what changed relative to the original (reordered / merged / promoted SE4 / demoted / cut / reframed / hedge kept) with the editorial reason; final bullet 'AUTHOR MARKERS: n'.
<<<END CHANGES>>>"""

RW_FRONT_SYSTEM = """\
You write the front matter of a research manuscript for {JOURNAL} ({ARTICLE_TYPE}) AFTER the body has been rewritten, so the abstract summarises what the paper now says. You receive the journal profile, the plan's headline, title options and abstract skeleton, the evidence ledger, and the complete rewritten body.
INTEGRITY: every number, unit, uncertainty and sample size must appear verbatim in the rewritten body or the ledger; no new comparisons, literature or novelty claims; certainty never increases; the qualifier (certified / stabilised / champion / n) travels with the headline number; [AUTHOR: ...] where an element needs information you lack. No citations in the abstract unless the profile says the journal uses a referenced summary paragraph (Nature flagship only), and then only markers that exist in the body.
ABSTRACT CRAFT: obey the profile's word limit exactly (count it): the field-level problem a non-specialist understands; the specific gap; 'Here we show' (or the journal's equivalent) with the headline and its strongest number; the two or three headline results with their conditions; implication and generality at the level the data support. No undefined abbreviations; no 'in this work'.
JOURNAL EXTRAS: produce exactly the elements the profile requires with their length rules (Context & Scale / Progress and Potential ~120-150 words for a general energy audience; Highlights 3-4 bullets <= 85 characters; eTOC blurb ~50 words third person; Broader context 100-200 words; ToC text 50-60 words present tense + '[AUTHOR: supply ToC graphic]'; One-sentence summary <= 125 characters; keywords where required). Where the profile marks an element uncertain, still produce it prefixed '[AUTHOR: verify this element is required by the current guide]'.
METHODS POINTER: 60-120 words in the journal's convention listing what the Methods must contain and where it goes, plus '[AUTHOR: paste and update the Methods; the rewrite did not alter methods text]'.
OUTPUT: markdown with these exact headings in order:
# <recommended title>
## Title options (ranked)
## Abstract
## <each journal extra, one heading per element>
## Keywords   (only if required)
## Methods pointer
## Notes for the author   (word counts achieved vs limits; every [AUTHOR: ...] you wrote; anything in the body the abstract could not summarise honestly)
Then the last line <<<END FRONT>>>."""

RW_REFEREE_SYSTEM = """\
You are the most careful referee this journal has: a specialist who has caught inflated claims in dozens of papers and who assumes that any rewrite, however well-intentioned, has drifted somewhere. You receive the EVIDENCE LEDGER (main text + SI), possibly the original body, the REWRITTEN MANUSCRIPT, and MECHANICAL FLAGS from a regex audit. Find every place where the rewrite says something the sources do not support, says it more strongly than they support, or no longer says something they established. You judge fidelity, not style.

FINDINGS (be exhaustive; 40 items is fine)
CRITICAL: a number, unit, uncertainty, n or condition that differs from the sources (including precision-changing rounding, conversion, computed ratios, wrong assignment of a value to a device); a change in what was measured or how (reverse-scan -> stabilised, in-house -> certified, champion -> average or presented as typical, different protocol/area/atmosphere); a citation marker absent from the sources or now supporting a different statement; a control, negative result or caveat from the original main text absent without an SI pointer; a mechanism stated as demonstrated that the ledger tags INDIRECT or SPECULATIVE; a number that exists only in a non-evidence file (referee report, cover letter); a [Ln] library marker not among the supplied LIBRARY PASSAGES, or supporting a statement its passage does not make; a literature value that differs from its [Ln] passage.
MAJOR: superlatives/comparatives not in the sources or not backed by a P-ID; generalisation beyond the tested system; causal language for correlational evidence; 'demonstrates' for 'suggests'; significance claims (commercial viability, cost) not in the sources; an abstract headline the rewritten Results do not establish with a stated number; uncertainty, n or qualifier dropped at first mention; a novelty, priority or 'record' claim that a supplied library passage contradicts (quote it).
MINOR: hedge strength altered without reason; ambiguous pronouns; undefined abbreviations; abstract-body inconsistencies within the rewrite.

METHOD: read the ledger; then check every number and claim in the rewrite abstract first (abstracts are where inflation lives); walk section by section locating support for every sentence with a number, comparison, causal verb or superlative; adjudicate EVERY mechanical flag as present-missed (quote where) / derived-correct (show arithmetic; still MINOR) / derived-wrong / rounding / absent / laundered; audit [AUTHOR: ...] markers (unnecessary ones; confident sentences that should have been markers); check every figure/table label exists and every ARGUMENT-CRITICAL figure is still referenced.

OUTPUT (markdown; keep headings exactly; the fenced json MUST be valid JSON)
# INTEGRITY REFEREE REPORT
## Verdict
FAITHFUL / FAITHFUL WITH MINOR ISSUES / NEEDS REPAIR (any MAJOR) / DO NOT USE WITHOUT REPAIR (any CRITICAL); one sentence; counts CRITICAL / MAJOR / MINOR.
## Findings
**R<n> [CRITICAL|MAJOR|MINOR] - <section heading>**  then: Rewrite says: "<verbatim <= 40 words>" / Sources say: "<verbatim with ID>" (or 'nothing: not in sources') / Problem: <one sentence> / Fix: <exact replacement sentence or the [AUTHOR: ...] marker to insert>
## Adjudication of mechanical flags
flag | verdict (present-missed / derived-correct / derived-wrong / rounding / absent / laundered) | evidence or arithmetic | severity
## Dropped from the original main text
E/C/F-IDs no longer present without an SI pointer; whether the plan justified it.
## Author markers audit
## Repair instructions
```json
{"repairs": [{"finding": "R1", "severity": "CRITICAL", "find": "<exact substring of the rewrite, <= 200 chars, no ellipses>", "replace_with": "<exact replacement>"}]}
```
Only CRITICAL and MAJOR findings whose fix is a local replacement; the 'find' string must occur in the rewrite exactly as written. Be specific and quotational; do not praise; do not comment on style. Finish with <<<END REPORT>>>."""


# ----------------------------------------------------- mechanical helpers
RW_SECTION_PATTERNS = [
    ("Abstract", r"^\s*(?:\d+\.?\s*)?abstract\b"),
    ("Introduction", r"^\s*(?:\d+\.?\s*)?(?:introduction|background)\b"),
    ("Results and Discussion",
     r"^\s*(?:\d+\.?\s*)?results?(?:\s*(?:and|&)\s*discussion)?\b"),
    ("Discussion", r"^\s*(?:\d+\.?\s*)?discussion\b"),
    ("Conclusions", r"^\s*(?:\d+\.?\s*)?(?:conclusions?|summary and outlook|"
                    r"outlook|concluding remarks)\b"),
    ("Methods", r"^\s*(?:\d+\.?\s*)?(?:methods?|experimental(?:\s+section|"
                r"\s+procedures?|\s+methods?)?|materials and methods)\b"),
    ("References", r"^\s*(?:\d+\.?\s*)?(?:references|bibliography|"
                   r"acknowledg(?:e)?ments?|author contributions|"
                   r"supporting information|supplementary|"
                   r"associated content|data availability)\b"),
]


def rw_split_sections(text):
    """Extracted manuscript -> ordered [(canonical name, text)]. Text before
    the first heading is 'Front matter'; the references block and what
    follows is kept under 'References' (never rewritten)."""
    lines = text.splitlines()
    marks = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or len(s) > 80:
            continue
        for name, pat in RW_SECTION_PATTERNS:
            if re.match(pat, s, re.I) and len(s.split()) <= 6:
                marks.append((i, name))
                break
    if not marks:
        return [("Body", text)]
    out = []
    first = marks[0][0]
    if "\n".join(lines[:first]).strip():
        out.append(("Front matter", "\n".join(lines[:first]).strip()))
    for j, (i, name) in enumerate(marks):
        end = marks[j + 1][0] if j + 1 < len(marks) else len(lines)
        body = "\n".join(lines[i + 1:end]).strip()
        if not body:
            continue
        if out and out[-1][0] == name:
            out[-1] = (name, out[-1][1] + "\n\n" + body)
        else:
            out.append((name, body))
    return out


def rw_chunk_text(text, max_chars=9000):
    if len(text) <= max_chars:
        return [text]
    paras = re.split(r"\n\s*\n", text)
    pieces, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 > max_chars and cur:
            pieces.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
        while len(cur) > max_chars:
            pieces.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur:
        pieces.append(cur)
    return pieces


def rw_section_map(sections):
    """Sub-section granularity map of the rewritable text: list of dicts
    {id, canonical, heading, words, text}. Subheadings are short lines
    without a terminal period (2.1 Film formation ...); long blocks
    without subheadings are chunked at ~9k chars."""
    entries = []
    n = 0
    for canon, text in sections:
        if canon in ("Front matter", "References"):
            continue
        blocks = []
        lines = text.splitlines()
        cur_head, cur = canon, []
        for line in lines:
            s = line.strip()
            is_sub = (s and len(s) <= 90 and len(s.split()) <= 12
                      and not s.endswith((".", ":", ";", ","))
                      and (re.match(r"^\d+(\.\d+)+\.?\s+\S", s)
                           or (s[0].isupper() and not re.search(
                               r"\d{2,}", s) and len(cur) > 3
                               and sum(c.isupper() for c in s) >= 1
                               and s == s.rstrip() and
                               len(s) < 70 and "  " not in s
                               and re.match(r"^[A-Z][A-Za-z0-9 ,\-/()"
                                            r"–’']+$", s)
                               and len(s.split()) >= 2)))
            numbered = bool(re.match(r"^\d+(\.\d+)+\.?\s+\S", s))
            if is_sub and "\n".join(cur).strip() and (
                    numbered or len("\n".join(cur)) > 300):
                blocks.append((cur_head, "\n".join(cur).strip()))
                cur_head, cur = s, []
                continue
            cur.append(line)
        if cur:
            blocks.append((cur_head, "\n".join(cur).strip()))
        for head, btxt in blocks:
            if not btxt:
                continue
            for k, piece in enumerate(rw_chunk_text(btxt, 9000)):
                n += 1
                entries.append({
                    "id": f"SEC{n}", "canonical": canon,
                    "heading": head + (f" (part {k + 1})" if k else ""),
                    "words": len(piece.split()), "text": piece})
    return entries


RW_NUM_RE = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*"
                       r"(?:\s*[×x]\s*10\s*\^?\s*[-−]?\d+|e[-+]?\d+)?")


def _rw_norm_num(s):
    s = s.replace(",", "").replace("−", "-").replace(" ", "")
    s = s.replace("^", "").replace("×10", "e").replace("x10", "e")
    return s.lstrip("+")


def rw_numbers_in(text):
    out = set()
    for m in RW_NUM_RE.finditer(text or ""):
        n = _rw_norm_num(m.group(0))
        if re.fullmatch(r"\d{1,2}", n.lstrip("-")):
            continue
        out.add(n)
    return out


RW_LABEL_RE = re.compile(r"\b(Fig\.?|Figure|Table|Note|Scheme|Extended Data "
                         r"Fig\.?|Supplementary (?:Fig\.?|Table|Note))\s?"
                         r"(S?\d+)", re.I)
RW_CIT_RE = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
RW_AY_RE = re.compile(r"\(([A-Z][A-Za-z'\-]+(?: et al\.| and [A-Z][A-Za-z'\-]+)?"
                      r",? (?:19|20)\d\d[a-z]?)\)")
RW_PM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:±|\+/-|\+-)\s*(\d+(?:\.\d+)?)")
RW_INTENS = ["novel", "unprecedented", "record", "breakthrough", "remarkable",
             "excellent", "outstanding", "superior", "state-of-the-art",
             "highest", "certified", "proves", "proven", "conclusively",
             "universal", "for the first time"]
RW_AUTHOR_RE = re.compile(r"\[AUTHOR:[^\]]*\]")


def _rw_labels(text):
    out = set()
    for m in RW_LABEL_RE.finditer(text or ""):
        kind = m.group(1).lower().replace("figure", "fig").rstrip(".")
        kind = "fig" if "fig" in kind else kind
        out.add((kind, m.group(2).upper()))
    return out


def _rw_citations(text):
    out = set()
    for m in RW_CIT_RE.finditer(text or ""):
        for part in re.split(r"\s*,\s*", m.group(1)):
            if re.match(r"^\d+\s*[-–]\s*\d+$", part):
                a, b = re.split(r"\s*[-–]\s*", part)
                if int(b) - int(a) < 60:
                    out.update(str(i) for i in range(int(a), int(b) + 1))
            elif part.strip().isdigit():
                out.add(part.strip())
    for m in RW_AY_RE.finditer(text or ""):
        out.add(m.group(1).lower())
    return out


def rw_mechanical_audit(rewrite, evidence_texts, nonevidence_texts, body,
                        abstract_text="", conclusions_text="",
                        library_texts=None, library_ids=None):
    """Deterministic audit of the assembled rewrite. Returns dict with
    'rows' [{severity, kind, item, context}], counts, and 'markers'."""
    rows = []
    rw_clean = RW_AUTHOR_RE.sub("", FIG_IMG_RE.sub("", rewrite))
    pool, npool = set(), set()
    for t in evidence_texts:
        pool |= rw_numbers_in(t)
    for t in nonevidence_texts:
        npool |= rw_numbers_in(t)
    lpool = set()
    for t in (library_texts or []):
        lpool |= rw_numbers_in(t)
    seen = set()
    for m in RW_NUM_RE.finditer(rw_clean):
        n = _rw_norm_num(m.group(0))
        if re.fullmatch(r"\d{1,2}", n.lstrip("-")) or n in pool or n in seen:
            continue
        pre = rw_clean[max(0, m.start() - 14):m.start()].lower()
        if re.search(r"(fig|figure|table|note|scheme|section|ref|eq)\.?\s*s?$",
                     pre) or re.search(r"\[[\d,\s\-–]*$", pre):
            continue
        seen.add(n)
        a, b = max(0, m.start() - 60), min(len(rw_clean), m.end() + 60)
        ctx = re.sub(r"\s+", " ", rw_clean[a:b]).strip()
        near = False
        try:
            v = float(n)
            near = any(abs(float(p) - v) <= max(abs(v) * 0.005, 1e-9)
                       for p in pool
                       if re.fullmatch(r"-?\d+(\.\d+)?(e-?\d+)?", p))
        except ValueError:
            pass
        if n in lpool:
            rows.append({"severity": "MINOR",
                         "kind": "Literature value from the author's library "
                                 "(verify against the cited [Ln] passage)",
                         "item": n, "context": ctx})
        elif n in npool:
            rows.append({"severity": "MAJOR",
                         "kind": "Number only in a non-evidence file "
                                 "(referee report / cover letter)",
                         "item": n, "context": ctx})
        elif near:
            rows.append({"severity": "MINOR", "kind": "Rounding / precision "
                         "changed", "item": n, "context": ctx})
        else:
            rows.append({"severity": "CRITICAL",
                         "kind": "Number not in any source", "item": n,
                         "context": ctx})
    # uncertainty pairs
    src_pairs = set()
    for t in evidence_texts:
        src_pairs |= {(a, b) for a, b in RW_PM_RE.findall(t)}
    for a, b in RW_PM_RE.findall(rw_clean):
        if (a, b) not in src_pairs:
            rows.append({"severity": "CRITICAL", "kind": "Uncertainty pair "
                         "not in sources", "item": f"{a} ± {b}",
                         "context": ""})
    for a, b in src_pairs:
        if a in rw_clean and f"{a} ± {b}" not in rw_clean and \
                f"{a}±{b}" not in rw_clean and f"{a} +/- {b}" not in rw_clean:
            rows.append({"severity": "MAJOR", "kind": "Uncertainty dropped",
                         "item": f"source: {a} ± {b}", "context": ""})
    # citations
    src_cits = set()
    for t in evidence_texts + nonevidence_texts + [body]:
        src_cits |= _rw_citations(t)
    for c in sorted(_rw_citations(rw_clean) - src_cits):
        rows.append({"severity": "CRITICAL", "kind": "Citation marker not "
                     "in sources", "item": c, "context": ""})
    known_l = {int(x) for x in (library_ids or [])}
    for n_l in sorted(rw_lib_cited(rw_clean) - known_l):
        rows.append({"severity": "CRITICAL", "kind": "Library citation [Ln] "
                     "not in the retrieved set", "item": f"[L{n_l}]", "context": ""})
    # labels
    src_labels = set()
    for t in evidence_texts:
        src_labels |= _rw_labels(t)
    for k, lab in sorted(_rw_labels(rw_clean) - src_labels):
        rows.append({"severity": "CRITICAL", "kind": "Figure/table label not "
                     "in sources", "item": f"{k} {lab}", "context": ""})
    # superlatives / certainty words
    low_body = (body or "").lower()
    low_rw = rw_clean.lower()
    for w in RW_INTENS:
        if w in low_rw and w not in low_body:
            sev = "MAJOR" if w in ("certified", "record", "proves", "proven",
                                   "for the first time", "unprecedented") \
                else "MINOR"
            rows.append({"severity": sev, "kind": "Strength word added",
                         "item": w, "context": ""})
    # dropped headline numbers (abstract / conclusions of the original)
    head_nums = rw_numbers_in(abstract_text) | rw_numbers_in(conclusions_text)
    rw_nums = rw_numbers_in(rw_clean)
    for n in sorted(head_nums - rw_nums):
        rows.append({"severity": "MINOR", "kind": "Original abstract/"
                     "conclusion number absent from rewrite", "item": n,
                     "context": ""})
    markers = RW_AUTHOR_RE.findall(rewrite)
    order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    rows.sort(key=lambda r: order[r["severity"]])
    counts = {k: sum(1 for r in rows if r["severity"] == k)
              for k in ("CRITICAL", "MAJOR", "MINOR")}
    return {"rows": rows, "counts": counts, "markers": markers}


def rw_audit_table(audit):
    if not audit["rows"]:
        return "(no mechanical flags)"
    lines = ["| # | Severity | Kind | Item | Context |", "|---|---|---|---|---|"]
    for i, r in enumerate(audit["rows"][:300], 1):
        lines.append(f"| {i} | {r['severity']} | {r['kind']} | "
                     f"{str(r['item']).replace('|', '/')} | "
                     f"{str(r['context']).replace('|', '/')[:160]} |")
    return "\n".join(lines)


# ------------------------------------------------ PV reporting checklist
# What editors and referees of perovskite-PV papers look for before they
# read the science (consensus reporting practice: device statistics,
# stabilised output, scan protocol, calibrated light source, masked area,
# ISOS-style stability protocol). Deterministic: regexes over the
# rewritten text and over the sources, so an item can be 'present',
# 'in sources only' (the rewrite dropped it) or 'missing'.
RW_CHECKLIST = [
    ("Device statistics (number of devices, n)",
     r"\bn\s*=\s*\d+|\b\d+\s+(?:independent\s+)?(?:devices|cells|samples|pixels)\b|"
     r"\bstatistic|\bdistribution of", "MAJOR"),
    ("Error-bar definition (s.d. / s.e.m. / quartiles)",
     r"standard deviation|\bs\.\s?d\.|standard error|s\.\s?e\.\s?m\.|interquartile|"
     r"box[- ]?plot|whisker|error bars? (?:represent|denote|show|indicate|are)", "MAJOR"),
    ("Scan direction / hysteresis",
     r"reverse scan|forward scan|scan direction|hysteresis|scan rate|"
     r"reverse and forward", "MAJOR"),
    ("Stabilised output (MPP tracking / SPO)",
     r"stabili[sz]ed (?:power|efficiency|PCE|output)|maximum[- ]power[- ]point|"
     r"\bMPP\b|\bSPO\b|steady[- ]state (?:efficiency|output)", "MAJOR"),
    ("Light source and calibration",
     r"AM\s?1\.5|100 mW|solar simulator|calibrat|reference (?:cell|diode)|"
     r"spectral mismatch|class A{1,3}\b", "MAJOR"),
    ("Active area / aperture mask",
     r"active area|aperture|shadow mask|\bmask(?:ed)?\b|\d+(?:\.\d+)?\s*cm\^?2|cm²|"
     r"\d+(?:\.\d+)?\s*mm\^?2", "MAJOR"),
    ("Stability protocol (ISOS)",
     r"ISOS-?[A-Z]-?\d|\bISOS\b|damp[- ]heat|85\s*°?\s?C\s*/\s*85|light[- ]soak|"
     r"thermal cycl|\bT80\b|\bT90\b|\bTs80\b|outdoor|operational stability", "MAJOR"),
    ("Atmosphere / encapsulation during testing",
     r"encapsulat|\bN2\b|nitrogen|glove ?box|ambient (?:air|conditions)|"
     r"relative humidity|\bRH\b", "MINOR"),
    ("EQE and integrated Jsc",
     r"\bEQE\b|external quantum efficiency|integrated (?:J|current|photocurrent)", "MINOR"),
    ("Reproducibility across batches",
     r"\bbatch(?:es)?\b|reproducib|independent (?:runs|experiments|fabrications)", "MINOR"),
    ("Statistical test for comparisons",
     r"\bp\s*[<=]\s*0\.\d+|t-test|Mann[- ]Whitney|ANOVA|Wilcoxon|significan", "MINOR"),
    ("Data availability statement",
     r"data availability|source data|available (?:from|upon|on) (?:request|the)|"
     r"repository|zenodo|figshare", "MINOR"),
]
RW_RECORD_WORDS = re.compile(r"\b(record|highest|certified|world[- ]record|"
                             r"best[- ]in[- ]class|champion)\b", re.I)
RW_CERT_RE = re.compile(r"certif|accredit|\bNREL\b|Fraunhofer|CalLab|Newport|"
                        r"\bJET\b|\bPVEL\b|\bESTI\b|\bNIM\b|\bAIST\b", re.I)


def _rw_snippet(text, m, width=70):
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return re.sub(r"\s+", " ", text[a:b]).strip()


def rw_reporting_checklist(rewrite_md, source_texts):
    """Rows: item, status (present / in sources only / missing), severity
    (OK / MINOR / MAJOR), evidence snippet."""
    text = RW_AUTHOR_RE.sub("", FIG_IMG_RE.sub("", rewrite_md or ""))
    src = "\n".join(source_texts or [])
    rows = []
    for item, pat, sev in RW_CHECKLIST:
        m = re.search(pat, text, re.I)
        if m:
            rows.append({"item": item, "status": "present", "severity": "OK",
                         "evidence": _rw_snippet(text, m)})
            continue
        ms = re.search(pat, src, re.I)
        if ms:
            rows.append({"item": item, "status": "in sources only (dropped from "
                         "the rewrite)", "severity": "MINOR",
                         "evidence": _rw_snippet(src, ms)})
        else:
            rows.append({"item": item, "status": "missing", "severity": sev,
                         "evidence": ""})
    mr = RW_RECORD_WORDS.search(text)
    if mr:
        mc = RW_CERT_RE.search(text)
        rows.append({"item": "Certification for record / highest / champion claims",
                     "status": "present" if mc else "missing",
                     "severity": "OK" if mc else "MAJOR",
                     "evidence": _rw_snippet(text, mc or mr)})
    counts = {k: sum(1 for r in rows if r["severity"] == k)
              for k in ("MAJOR", "MINOR", "OK")}
    return {"rows": rows, "counts": counts}


def rw_checklist_table(chk):
    if not chk or not chk.get("rows"):
        return "(checklist not run)"
    lines = [f"*{chk['counts']['OK']} present · {chk['counts']['MINOR']} minor · "
             f"{chk['counts']['MAJOR']} major gaps. Items an editor or referee of a "
             "perovskite-PV paper expects to find; 'in sources only' means the "
             "original or SI states it and the rewrite dropped it.*", "",
             "| Item | Status | Severity | Evidence |", "|---|---|---|---|"]
    for r in chk["rows"]:
        lines.append(f"| {r['item']} | {r['status']} | {r['severity']} | "
                     f"{r['evidence'].replace('|', '/')[:140]} |")
    return "\n".join(lines)


# ---------------------------------------------------------- parsing utils
def _rw_between(text, start, end):
    a = text.find(start)
    if a == -1:
        return None, False
    a += len(start)
    b = text.find(end, a)
    if b == -1:
        return text[a:].strip(), False
    return text[a:b].strip(), True


def _rw_json_block(text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        a, b = text.find("{"), text.rfind("}")
        raw = text[a:b + 1] if a != -1 and b > a else ""
    try:
        return _rwjson.loads(raw)
    except Exception:
        return None


def _rw_strip_json(text):
    return re.sub(r"```json.*?```", "[manifest omitted]", text, flags=re.S)


RW_KINDS = ("intro", "results", "discussion", "conclusions", "methods_pointer")


def rw_validate_manifest(man, secmap):
    """Return a clean list of section dicts, or None if unusable."""
    if not isinstance(man, dict) or not isinstance(man.get("sections"), list):
        return None
    valid_ids = {e["id"] for e in secmap}
    out = []
    for i, s in enumerate(man["sections"]):
        if not isinstance(s, dict) or s.get("kind") not in RW_KINDS:
            continue
        out.append({
            "id": str(s.get("id") or f"S{i + 1}"),
            "kind": s["kind"],
            "heading": str(s.get("heading") or s["kind"].title())[:120],
            "target_words": int(s.get("target_words") or 500),
            "source_ids": [x for x in (s.get("source_ids") or [])
                           if x in valid_ids],
            "evidence_ids": [str(x) for x in (s.get("evidence_ids") or [])],
            "si_promote": [str(x) for x in (s.get("si_promote") or [])],
            "cut_or_demote": [str(x) for x in (s.get("cut_or_demote") or [])],
            "brief": str(s.get("brief") or ""),
            "first_use_abbreviations": [
                str(x) for x in (s.get("first_use_abbreviations") or [])],
            "library_ids": [str(x) for x in (s.get("library_ids") or [])],
        })
    kinds = {s["kind"] for s in out}
    if "results" not in kinds:
        return None
    return out


def rw_default_manifest(secmap, journal):
    """Fallback manifest from the detected sections when the plan's JSON
    cannot be used."""
    out, n = [], 0
    by_canon = {}
    for e in secmap:
        by_canon.setdefault(e["canonical"], []).append(e)
    def add(kind, heading, ids, words):
        nonlocal n
        n += 1
        out.append({"id": f"S{n}", "kind": kind, "heading": heading,
                    "target_words": words, "source_ids": ids,
                    "evidence_ids": [], "si_promote": [],
                    "cut_or_demote": [], "brief": "(default manifest - the "
                    "plan's JSON could not be parsed; follow the plan text)",
                    "first_use_abbreviations": []})
    if by_canon.get("Introduction"):
        add("intro", "Introduction",
            [e["id"] for e in by_canon["Introduction"]], 650)
    res = by_canon.get("Results and Discussion", []) + \
        by_canon.get("Body", [])
    for e in res[:6]:
        add("results", e["heading"][:80], [e["id"]],
            max(300, min(1000, int(e["words"] * 0.8))))
    disc = by_canon.get("Discussion", []) + by_canon.get("Conclusions", [])
    if disc:
        add("discussion", "Discussion", [e["id"] for e in disc], 500)
    if by_canon.get("Methods"):
        add("methods_pointer", "Methods (pointer)",
            [e["id"] for e in by_canon["Methods"]], 150)
    return out


def rw_si_pack(si_text, labels, max_chars=10000):
    """Paragraphs of the SI mentioning any of the promoted labels/IDs."""
    if not si_text or not labels:
        return ""
    paras = re.split(r"\n\s*\n", si_text)
    keys = [str(l).lower() for l in labels if str(l).strip()]
    out = []
    for i, p in enumerate(paras):
        low = p.lower()
        if any(k in low for k in keys):
            block = "\n\n".join(paras[max(0, i - 1):i + 2])
            if block not in out:
                out.append(block)
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n\n---\n\n".join(out)[:max_chars]


_RW_UI = {"box": None}
RW_TRANSIENT = ("529", "overloaded", "500", "502", "503", "504", "429",
                "rate limit", "rate_limit", "timeout", "timed out",
                "connection", "temporarily", "server error", "internal")
RW_RETRY_WAITS = (5, 15, 40, 90)


def rw_call(system, user, max_tokens):
    """One model call with automatic retry on transient API errors
    (overloaded / 5xx / rate limit / network). Non-transient errors and
    exhausted retries propagate - the stage machine has already saved
    every earlier stage, so Resume continues from this call."""
    import time
    last = None
    for attempt in range(len(RW_RETRY_WAITS) + 1):
        try:
            return call_claude(api_key.strip(), system, user, model,
                               max_tokens=max_tokens)
        except Exception as e:
            msg = str(e).lower()
            last = e
            if attempt == len(RW_RETRY_WAITS) or not any(
                    k in msg for k in RW_TRANSIENT):
                raise
            wait = RW_RETRY_WAITS[attempt]
            box = _RW_UI.get("box")
            if box is not None:
                try:
                    box.write(f"⏳ API busy ({str(e)[:70]}) - retrying in "
                              f"{wait} s (attempt {attempt + 2}/"
                              f"{len(RW_RETRY_WAITS) + 1})...")
                except Exception:
                    pass
            time.sleep(wait)
    raise last


def rw_call_delimited(system, user, max_tokens, start, end):
    """Call + one continuation attempt if the END delimiter is missing."""
    out = rw_call(system, user, max_tokens)
    body, complete = _rw_between(out, start, end)
    if body is None:
        return out.strip(), False, out
    if complete:
        return body, True, out
    cont = rw_call(system, user + "\n\nYOUR PREVIOUS OUTPUT WAS CUT OFF. "
                   "Its last 400 characters were:\n...\n" + out[-400:]
                   + "\n\nContinue EXACTLY from where it stopped: output "
                   "only the remaining text, then the closing delimiters.",
                   max_tokens)
    joined = out + cont
    body2, complete2 = _rw_between(joined, start, end)
    return (body2 if body2 is not None else body), complete2, joined


# ------------------------------------------------------------- the runner
def rw_save_state(state):
    try:
        d = RW_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(_rwjson.dumps(state, default=str),
                                      encoding="utf-8")
    except Exception:
        pass


def rw_load_last_state():
    if not RW_DIR.exists():
        return None
    cands = sorted(RW_DIR.glob("*/state.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands[:1]:
        try:
            return _rwjson.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def rw_log(state, msg):
    state.setdefault("log", []).append(
        f"{datetime.datetime.now():%H:%M:%S} {msg}")


# ---------------------------------------------- library grounding (rewrite)
# The rewrite can draw on the author's indexed corpus: passages are
# retrieved per section, numbered [L1], [L2], ... in a job registry, and
# the writer may cite them for positioning, precedent and comparison. Every
# [Ln] marker is checked against the registry; literature values quoted
# from passages are flagged for verification; the bundle lists the library
# references to merge into the reference list.
RW_LIB_MODES = {
    "Off": "off",
    "Positioning only (informs the plan, no new citations)": "plan",
    "Cite my library in Introduction & Discussion": "cite",
    "Cite and compare values (Results and figures too)": "compare",
}


def rw_lib_cited(text):
    """Set of library numbers cited as [L3] or [L3, L7] in a text."""
    out = set()
    for m in re.finditer(r"\[(L\d+(?:\s*,\s*L?\d+)*)\]", text or ""):
        for part in re.split(r"\s*,\s*", m.group(1)):
            d = re.sub(r"\D", "", part)
            if d:
                out.add(int(d))
    return out


def rw_lib_register(state, hits):
    """Register hits in the job registry (paper-level numbers) and keep the
    stored hit small enough for state.json."""
    state.setdefault("registry", {})
    nums = rv_register(state, hits)
    for ent in state["registry"].values():
        h = ent.get("hit") or {}
        if "embedding" in h or len(str(h.get("text", ""))) > 2000:
            ent["hit"] = {"meta": h.get("meta", {}), "text": str(h.get("text", ""))[:2000],
                          "score": float(h.get("score", 0) or 0)}
    return nums


def rw_lib_retrieve(state, query, k):
    q = re.sub(r"\s+", " ", query or "").strip()
    if not q:
        return []
    return retrieve(q[:1500], max(2, int(k)), where_extra=state.get("where"))


def rw_lib_context(state, hits, nums, max_chars=40000):
    """Passages labelled [Ln] (paper-level numbers), deduplicated."""
    parts, seen, total = [], set(), 0
    for n, h in zip(nums, hits):
        key = (n, h.get("text", "")[:100])
        if key in seen:
            continue
        seen.add(key)
        m = h["meta"]
        block = (f"[L{n}] {m.get('title', '?')} ({m.get('file', '')}, "
                 f"p.{m.get('page_start', '?')}-{m.get('page_end', '?')})\n"
                 f"{h.get('text', '')}")
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


def rw_lib_queries_plan(ledger, body):
    """Positioning queries from the ledger: the claims section and its
    first claim lines, else the opening of the manuscript."""
    qs = []
    m = re.search(r"## 2\..*?(?=## 3\.)", ledger or "", re.S)
    if m:
        sec = m.group(0)
        qs.append(re.sub(r"[#|*]", " ", sec)[:1200])
        rows = [ln for ln in sec.splitlines() if "|" in ln and not ln.strip().startswith("|--")]
        for ln in rows[1:6]:
            cells = [c.strip() for c in ln.split("|") if c.strip()]
            if len(cells) >= 2:
                qs.append(" ".join(cells[1:3])[:300])
    if not qs:
        qs.append((body or "")[:1500])
    return [q for q in qs if len(q) > 20][:6]


def rw_lib_queries_section(s, src_txt):
    qs = [f"{s.get('heading', '')} {s.get('brief', '')}"]
    if src_txt:
        qs.append(re.sub(r"\s+", " ", src_txt)[:800])
    return qs


def rw_lib_reference_list(state, cited):
    """[Ln] entries for the cited library papers, to be merged into the
    manuscript's reference list by the author."""
    reg = state.get("registry", {})
    by_n = {v["n"]: v for v in reg.values()}
    lines = []
    for n in sorted(cited):
        v = by_n.get(n)
        if not v:
            lines.append(f"[L{n}] [AUTHOR: library citation not in the retrieved set - "
                         "remove or replace]")
            continue
        extra = f" ({v['year']})" if v.get("year") else ""
        lines.append(f"[L{n}] {v['title']}{extra} — file: {v['file']} "
                     "[AUTHOR: complete the bibliographic reference and merge it "
                     "into the reference list, renumbering the markers]")
    return "\n".join(lines)


def rw_run(state, status_box):
    """Advance the pipeline from wherever it stopped. Raises on model
    errors after saving state. Returns when complete or paused."""
    inp, opts, stg = state["inputs"], state["opts"], state["stages"]
    prof = JOURNAL_PROFILES[opts["journal"]]
    jname, jtype = opts["journal"], opts["article_type"]
    ms_secs = [tuple(x) for x in inp["ms_sections"]]
    secmap = inp["secmap"]
    body = inp["ms_body"]
    ref_block = inp["ref_block"]

    _RW_UI["box"] = status_box

    def step(msg):
        status_box.write(msg)
        rw_log(state, msg)

    # ---- 1. digest --------------------------------------------------------
    if "ledger" not in stg:
        groups, cur, cur_len = [], [], 0
        for e in secmap:
            if cur and cur_len + len(e["text"]) > 45000:
                groups.append(cur)
                cur, cur_len = [], 0
            cur.append(e)
            cur_len += len(e["text"])
        if cur:
            groups.append(cur)
        others = "\n\n".join(
            f"=== OTHER FILE: {f['name']} | ROLE: {f['role']} ===\n"
            f"{f['text'][:25000]}"
            for f in inp["others"] if f["role"] != "Supplementary Information")
        parts = []
        for k, g in enumerate(groups, 1):
            step(f"Digesting manuscript (part {k}/{len(groups)})...")
            txt = "\n\n".join(f"[{e['id']} | {e['heading']}]\n{e['text']}"
                              for e in g)
            front = ms_secs[0][1] if ms_secs and ms_secs[0][0] in (
                "Front matter",) else ""
            umsg = (f"THE MANUSCRIPT ('{inp['ms_name']}')"
                    + (f" - part {k} of {len(groups)}" if len(groups) > 1
                       else "") + ":\n\n"
                    + (f"[FRONT MATTER]\n{front[:3000]}\n\n" if k == 1 and
                       front else "")
                    + txt
                    + (f"\n\n=== REFERENCE LIST (citation inventory only) "
                       f"===\n{ref_block[:5000]}" if k == len(groups) else "")
                    + (f"\n\n{others}" if k == 1 and others else "")
                    + "\n\nBuild the EVIDENCE LEDGER now.")
            out = rw_call(RW_DIGEST_SYSTEM, umsg, 14000)
            parts.append(out.replace("<<<END LEDGER>>>", "").strip())
        stg["ledger"] = "\n\n".join(parts)
        rw_save_state(state)

    # ---- 1b. SI digest ---------------------------------------------------
    if "si_ledger" not in stg:
        si_files = [f for f in inp["others"]
                    if f["role"] == "Supplementary Information"]
        si_text = "\n\n".join(f["text"] for f in si_files)
        inp["si_text"] = si_text
        if si_text.strip():
            chunks = rw_chunk_text(si_text, 60000)
            orient = ""
            m = re.search(r"## 2\..*?(?=## 3\.)", stg["ledger"], re.S)
            if m:
                orient += m.group(0)
            m = re.search(r"## 8\..*?(?=## 9\.)", stg["ledger"], re.S)
            if m:
                orient += "\n" + m.group(0)
            parts = []
            for k, ch in enumerate(chunks, 1):
                step(f"Digesting SI (chunk {k}/{len(chunks)})...")
                umsg = (f"ORIENTATION FROM THE MAIN-TEXT LEDGER:\n"
                        f"{orient[:8000]}\n\n=== SUPPLEMENTARY INFORMATION, "
                        f"chunk {k} of {len(chunks)} ===\n{ch}\n\n"
                        "Build the SI LEDGER for this chunk now.")
                out = rw_call(RW_SI_SYSTEM, umsg, 12000)
                parts.append(out.replace("<<<END LEDGER>>>", "").strip())
            stg["si_ledger"] = "\n\n".join(parts)
        else:
            stg["si_ledger"] = ""
        rw_save_state(state)

    # ---- 2. plan ---------------------------------------------------------
    if "plan" not in stg:
        step("Editorial plan...")
        secmap_txt = "\n".join(
            f"{e['id']} | {e['canonical']} | {e['heading']} | "
            f"~{e['words']} words" for e in secmap)
        corpus_block = ""
        if opts.get("ground") and index_ok:
            try:
                state.setdefault("registry", {})
                hits_all, nums_all = [], []
                for q in rw_lib_queries_plan(stg["ledger"], body):
                    hs = rw_lib_retrieve(state, q, int(opts.get("k_lib", 14)) // 2 + 2)
                    hits_all += hs
                    nums_all += rw_lib_register(state, hs)
                if hits_all:
                    state["hits"] = [v["hit"] for v in state["registry"].values()][:40]
                    stg["lib_plan"] = rw_lib_context(state, hits_all, nums_all, 60000)
                    use = ("Use them ONLY for positioning (sections 2, 4b, 10 - as "
                           "[AUTHOR: consider citing L<n>]); the sections will not cite them."
                           if opts.get("lib_mode") == "plan" else
                           "Use them for the POSITIONING MAP and assign to each section the "
                           "library_ids it may cite as [Ln].")
                    corpus_block = ("LIBRARY PASSAGES FROM THE AUTHOR'S OWN CORPUS, numbered "
                                    f"[L1], [L2], ... {use}\n\n{stg['lib_plan']}")
            except Exception as e:
                rw_log(state, f"library retrieval failed: {e}")
        sysm = (RW_PLAN_SYSTEM.replace("{JOURNAL}", jname)
                .replace("{PROFILE}", prof["text"])
                .replace("{ARTICLE_TYPE}", jtype))
        umsg = (f"EVIDENCE LEDGER:\n{stg['ledger']}\n\n"
                f"SI LEDGER:\n{stg['si_ledger'] or '(no SI provided)'}\n\n"
                f"SECTION MAP OF THE ORIGINAL (use these ids in source_ids):\n"
                f"{secmap_txt}\n\n"
                f"AUTHOR'S OWN HEADLINE: {opts.get('headline') or '(none)'}\n"
                f"AUTHOR'S INSTRUCTIONS: {opts.get('instructions') or '(none)'}"
                f"{opts.get('plan_edits', '')}\n"
                f"RESTRUCTURING LEVEL: {opts['level']}\n\n{corpus_block}\n\n"
                "Write the EDITORIAL PLAN now.")
        out = rw_call(sysm, umsg, 10000)
        stg["plan"] = out.replace("<<<END PLAN>>>", "").strip()
        man = _rw_json_block(stg["plan"])
        sections = rw_validate_manifest(man, secmap) if man else None
        if not sections:
            sections = rw_default_manifest(secmap, jname)
            rw_log(state, "Plan manifest unusable - default manifest built "
                           "from detected sections.")
            state["manifest_fallback"] = True
        stg["manifest"] = {"sections": sections,
                           "headline": (man or {}).get("headline", ""),
                           "title_recommended":
                           (man or {}).get("title_recommended", ""),
                           "title_options":
                           (man or {}).get("title_options", []),
                           "dropped": (man or {}).get("dropped", [])}
        rw_save_state(state)
        if opts.get("pause_plan") and not state.get("plan_approved"):
            state["status"] = "awaiting_plan"
            rw_save_state(state)
            return

    # ---- 3. sections -----------------------------------------------------
    sections = stg["manifest"]["sections"]
    stg.setdefault("sections_out", {})
    plan_excerpt = _rw_strip_json(stg["plan"])
    secmap_by_id = {e["id"]: e for e in secmap}
    prev_tail = ""
    for i, s in enumerate(sections):
        if s["id"] in stg["sections_out"]:
            prev_tail = stg["sections_out"][s["id"]]["text"][-1500:]
            continue
        step(f"Writing section {i + 1}/{len(sections)} - {s['heading']}")
        src_txt = "\n\n".join(
            f"[{sid} | {secmap_by_id[sid]['heading']}]\n"
            f"{secmap_by_id[sid]['text']}"
            for sid in s["source_ids"] if sid in secmap_by_id)
        if not src_txt:
            canon = {"intro": "Introduction", "discussion": "Discussion",
                     "conclusions": "Conclusions",
                     "methods_pointer": "Methods"}.get(s["kind"])
            src_txt = "\n\n".join(e["text"] for e in secmap
                                  if e["canonical"] == canon)[:30000] \
                or "(no matching original text - write from the ledger)"
        src_txt = src_txt[:30000]
        wanted = set(s["evidence_ids"]) | set(s["si_promote"])
        si_rows = "\n".join(
            ln for ln in (stg["si_ledger"] or "").splitlines()
            if any(ln.strip().startswith(w) or f"| {w} |" in ln
                   for w in wanted)) if wanted else ""
        si_pack = rw_si_pack(inp.get("si_text", ""), s["si_promote"])
        lib_block = ""
        lib_mode = opts.get("lib_mode", "off")
        if lib_mode in ("cite", "compare") and index_ok and (
                s["kind"] in ("intro", "discussion", "conclusions")
                or s.get("library_ids")
                or (lib_mode == "compare" and s["kind"] == "results")):
            try:
                state.setdefault("registry", {})
                hs, ns = [], []
                for q in rw_lib_queries_section(s, src_txt):
                    h = rw_lib_retrieve(state, q, int(opts.get("k_lib", 14)) // 2 + 1)
                    hs += h
                    ns += rw_lib_register(state, h)
                want_n = [int(re.sub(r"\D", "", x)) for x in s.get("library_ids", [])
                          if re.sub(r"\D", "", x)]
                eh, en = rv_hits_for_numbers(state, want_n)
                lib_ctx = rw_lib_context(state, hs + eh, ns + en, 40000)
                stg.setdefault("lib_passages", {})[s["id"]] = lib_ctx
                if lib_ctx:
                    lib_block = ("=== LIBRARY PASSAGES (the author's own corpus; cite ONLY as "
                                 "the given [Ln] markers, for positioning, precedent and "
                                 "comparison; literature values verbatim with their conditions) "
                                 f"===\n{lib_ctx}\n\n")
            except Exception as e:
                rw_log(state, f"library retrieval failed for {s['id']}: {e}")
        sysm = (RW_SECTION_SYSTEM.replace("{JOURNAL}", jname)
                .replace("{ARTICLE_TYPE}", jtype))
        umsg = (f"SECTION TO WRITE: {s['id']} | kind: {s['kind']} | heading: "
                f"{s['heading']} | target_words: {s['target_words']}\n"
                f"BRIEF FROM THE EDITOR: {s['brief']}\n"
                f"EVIDENCE IDS PERMITTED: {', '.join(s['evidence_ids']) or '(all)'}\n"
                f"SI ITEMS TO PROMOTE HERE: {', '.join(s['si_promote']) or '(none)'}\n"
                f"ITEMS TO DEMOTE OR CUT: {', '.join(s['cut_or_demote']) or '(none)'}\n"
                f"FIRST-USE ABBREVIATIONS TO DEFINE HERE: "
                f"{', '.join(s['first_use_abbreviations']) or '(none)'}\n\n"
                f"=== EDITORIAL PLAN ===\n{plan_excerpt[:30000]}\n\n"
                f"=== EVIDENCE LEDGER (main text) ===\n{stg['ledger'][:45000]}\n\n"
                + (f"=== SI LEDGER ROWS FOR THIS SECTION ===\n{si_rows[:8000]}\n\n"
                   if si_rows else "")
                + f"=== ORIGINAL TEXT FOR THIS SECTION (verbatim) ===\n{src_txt}\n\n"
                + (f"=== SI EVIDENCE PACK ===\n{si_pack}\n\n" if si_pack else "")
                + lib_block
                + f"=== REFERENCE LIST (markers only; never add) ===\n{ref_block[:8000]}\n\n"
                f"=== END OF THE PREVIOUS SECTION (continuity; do not repeat) ===\n"
                f"{prev_tail or '(this is the first section)'}\n\n"
                "Write this section now, obeying the output delimiters exactly.")
        text, complete, raw = rw_call_delimited(
            sysm, umsg, 8000, "<<<SECTION>>>", "<<<END SECTION>>>")
        changes, _ = _rw_between(raw, "<<<CHANGES>>>", "<<<END CHANGES>>>")
        if not text.lstrip().startswith("#"):
            text = f"## {s['heading']}\n\n{text}"
        if not complete:
            text += "\n\n[AUTHOR: this section was cut off by the output " \
                    "window - review its end]"
        stg["sections_out"][s["id"]] = {"text": text,
                                        "changes": changes or "",
                                        "complete": complete}
        prev_tail = text[-1500:]
        rw_save_state(state)

    # ---- 4. front matter -------------------------------------------------
    if "front" not in stg:
        step("Front matter (title, abstract, journal extras)...")
        body_md = "\n\n".join(stg["sections_out"][s["id"]]["text"]
                              for s in sections)
        src_abs = next((t for c, t in ms_secs if c == "Abstract"), "")
        plan_head = ""
        for sec_no in ("1", "3", "6"):
            m = re.search(rf"## {sec_no}\..*?(?=## \d+\.|$)", stg["plan"],
                          re.S)
            if m:
                plan_head += m.group(0) + "\n"
        sysm = (RW_FRONT_SYSTEM.replace("{JOURNAL}", jname)
                .replace("{ARTICLE_TYPE}", jtype))
        umsg = (f"JOURNAL PROFILE:\n{prof['text']}\n"
                f"REQUIRED FRONT-MATTER ELEMENTS: "
                f"{', '.join(prof['front_matter'])}\n\n"
                f"PLAN (headline, titles, abstract skeleton):\n{plan_head}\n\n"
                f"EVIDENCE LEDGER:\n{stg['ledger'][:40000]}\n\n"
                f"SOURCE ABSTRACT (reference only):\n{src_abs[:3000]}\n\n"
                f"REWRITTEN BODY (complete):\n{body_md}\n\n"
                "Write the front matter now, ending with <<<END FRONT>>>.")
        out = rw_call(sysm, umsg, 5000)
        stg["front"] = out.replace("<<<END FRONT>>>", "").strip()
        rw_save_state(state)

    # ---- 4b. figures (proposed conceptual figures; data only from ledger)
    if opts.get("figures", True) and "fig_done" not in stg:
        sec_texts = [(s["id"], s["heading"], stg["sections_out"][s["id"]]["text"])
                     for s in sections]
        _figs, sec_texts = fig_run_stage(
            state, "rw", sec_texts, _rw_strip_json(stg["plan"]), api_key,
            model, step, max_figs=int(opts.get("max_figs", 3)),
            vision=opts.get("vision", True), label_prefix="Proposed Figure")
        if _figs is None:            # figure plan awaiting approval
            return
        for sid, _h, md in sec_texts:
            stg["sections_out"][sid]["text"] = md
        stg["fig_done"] = True
        rw_save_state(state)

    # ---- 5. mechanical audit ---------------------------------------------
    def assembled():
        body_md = "\n\n".join(stg["sections_out"][s["id"]]["text"]
                              for s in sections)
        return stg["front"] + "\n\n" + body_md
    if "audit" not in stg:
        step("Mechanical integrity audit...")
        stg["audit"] = rw_mechanical_audit(
            assembled(), inp["evidence_texts"], inp["nonevidence_texts"],
            body, next((t for c, t in ms_secs if c == "Abstract"), ""),
            next((t for c, t in ms_secs if c == "Conclusions"), ""),
            library_texts=list(stg.get("lib_passages", {}).values()),
            library_ids={v["n"] for v in state.get("registry", {}).values()})
        rw_save_state(state)
    if "checklist" not in stg:
        stg["checklist"] = rw_reporting_checklist(assembled(),
                                                  inp["evidence_texts"])
        rw_save_state(state)

    # ---- 6. referee ------------------------------------------------------
    if "referee" not in stg:
        step("Adversarial referee pass...")
        rw_md = assembled()
        idx = "\n".join(f"{s['id']}: {s['heading']}" for s in sections)
        ledger_all = stg["ledger"] + ("\n\n" + stg["si_ledger"]
                                      if stg["si_ledger"] else "")
        orig = ""
        if len(ledger_all) + len(rw_md) + len(body) < 150000:
            orig = f"=== ORIGINAL MANUSCRIPT BODY ===\n{body}\n\n"
        lib_all = "\n\n".join(stg.get("lib_passages", {}).values())
        lib_blk = (f"=== LIBRARY PASSAGES SUPPLIED TO THE WRITER (the only valid "
                   f"[Ln] sources) ===\n{lib_all[:40000]}\n\n" if lib_all else "")
        umsg = (f"EVIDENCE LEDGER:\n{ledger_all}\n\n{orig}"
                f"=== REWRITTEN MANUSCRIPT ===\n{rw_md}\n\n{lib_blk}"
                f"=== MECHANICAL FLAGS (adjudicate every row) ===\n"
                f"{rw_audit_table(stg['audit'])}\n\n"
                f"=== SECTION IDS ===\n{idx}\n\n"
                "Write the INTEGRITY REFEREE REPORT now.")
        out = rw_call(RW_REFEREE_SYSTEM, umsg, 10000)
        stg["referee"] = out.replace("<<<END REPORT>>>", "").strip()
        rw_save_state(state)

    # ---- 7. repairs (exact-match only) -----------------------------------
    if "repairs" not in stg:
        applied, skipped = [], []
        rep = _rw_json_block(stg["referee"]) if opts.get("auto_repair") \
            else None
        for r in (rep or {}).get("repairs", []) if rep else []:
            find, repl = str(r.get("find", "")), str(r.get("replace_with", ""))
            if not find or len(find) > 400:
                skipped.append(r)
                continue
            hit_sec = None
            for s in sections:
                t = stg["sections_out"][s["id"]]["text"]
                if t.count(find) == 1:
                    hit_sec = s["id"]
                    break
            if hit_sec is None and stg["front"].count(find) == 1:
                stg["front"] = stg["front"].replace(find, repl)
                applied.append(r)
            elif hit_sec:
                stg["sections_out"][hit_sec]["text"] = \
                    stg["sections_out"][hit_sec]["text"].replace(find, repl)
                applied.append(r)
            else:
                skipped.append(r)
        stg["repairs"] = {"applied": applied, "skipped": skipped}
        if applied:
            step(f"Applied {len(applied)} referee repair(s); re-auditing...")
            stg["audit_after"] = rw_mechanical_audit(
                assembled(), inp["evidence_texts"], inp["nonevidence_texts"],
                body, next((t for c, t in ms_secs if c == "Abstract"), ""),
                next((t for c, t in ms_secs if c == "Conclusions"), ""),
                library_texts=list(stg.get("lib_passages", {}).values()),
                library_ids={v["n"] for v in state.get("registry", {}).values()})
        rw_save_state(state)

    # ---- 8. assemble + save ----------------------------------------------
    if "bundle" not in stg:
        step("Assembling and saving...")
        rw_md = assembled()
        lib_cited = rw_lib_cited(rw_md)
        if lib_cited:
            rw_md = (rw_md.rstrip() + "\n\n## Library references (to merge into the "
                     "reference list)\n\n" + rw_lib_reference_list(state, lib_cited))
        audit = stg.get("audit_after") or stg["audit"]
        markers = RW_AUTHOR_RE.findall(rw_md)
        words = len(re.sub(r"^#.*$", "", rw_md, flags=re.M).split())
        changes = "\n\n".join(
            f"### {s['heading']}\n{stg['sections_out'][s['id']]['changes']}"
            for s in sections if stg["sections_out"][s["id"]]["changes"])
        m7 = re.search(r"## 7\..*?(?=## 8\.|$)", stg["plan"], re.S)
        repairs = stg["repairs"]
        rep_md = ""
        if repairs["applied"] or repairs["skipped"]:
            rep_md = "\n\n### Referee repairs\n" + "\n".join(
                f"- APPLIED {r.get('finding')}: {str(r.get('find'))[:80]}… → "
                f"{str(r.get('replace_with'))[:80]}…"
                for r in repairs["applied"]) + ("\n" if repairs["applied"]
                                                else "") + "\n".join(
                f"- NOT APPLIED (text not found exactly once) "
                f"{r.get('finding')}: {str(r.get('find'))[:100]}"
                for r in repairs["skipped"])
        verdict_m = re.search(r"## Verdict\s*\n(.+)", stg["referee"])
        verdict = verdict_m.group(1).strip() if verdict_m else "(see report)"
        items = "\n".join(f"{i}. {mk}" for i, mk in
                          enumerate(dict.fromkeys(markers), 1)) or "(none)"
        bundle = (
            f"# Rewritten manuscript - {jname} ({jtype})\n\n"
            f"*Words (main text incl. front matter): {words} · "
            f"[AUTHOR] items: {len(markers)} · mechanical flags: "
            f"{audit['counts']['CRITICAL']} critical / "
            f"{audit['counts']['MAJOR']} major / "
            f"{audit['counts']['MINOR']} minor · library citations: {len(lib_cited)} · "
            f"referee: {verdict}*\n\n"
            "> Draft for the authors. Every CRITICAL flag and every "
            "[AUTHOR: ...] item must be resolved by a human before "
            "circulation. Methods and the reference list were not "
            "rewritten. Journal profile last verified "
            f"{RW_PROFILES_VERIFIED} - confirm current limits.\n\n---\n\n"
            f"{rw_md}\n\n---\n\n# Change log\n\n"
            + (m7.group(0) if m7 else "") + "\n\n" + changes + rep_md
            + ("\n\n### Edits made in the follow-up conversation\n"
               + "\n".join(stg["followup_log"]) if stg.get("followup_log") else "")
            + "\n\n---\n\n# Integrity report\n\n## Mechanical audit"
            + (" (after repairs)" if stg.get("audit_after") else "")
            + f"\n\n{rw_audit_table(audit)}\n\n"
            "*Mechanical PASS is necessary, not sufficient: a source number "
            "attached to the wrong device or condition passes this check "
            "and is caught only by the referee.*\n\n"
            f"{stg['referee']}\n\n---\n\n# PV reporting checklist\n\n"
            f"{rw_checklist_table(stg.get('checklist'))}\n\n---\n\n"
            f"# Author action items\n\n{items}"
            f"\n\n---\n\n# Editorial plan\n\n{stg['plan']}\n\n---\n\n"
            f"# Evidence ledger\n\n{stg['ledger']}"
            + (f"\n\n{stg['si_ledger']}" if stg["si_ledger"] else ""))
        if stg.get("figures"):
            bundle += "\n\n---\n\n# Figures generated\n\n" + fig_report_md(
                [stg["figures"][str(f["number"])] for f in stg["fig_manifest"]])
        stg["bundle"] = bundle
        stg["manuscript_md"] = rw_md
        try:
            qa = record_qa(f"[REWRITE - {jname}] {inp['ms_name']}", bundle,
                           state.get("hits", []), do_autosave)
            state["qa_time"] = qa["time"]
        except Exception as e:
            rw_log(state, f"record_qa failed: {e}")
        state["status"] = "complete"
        rw_save_state(state)


# ---------------------------------------------------------------- the UI
def render_rewrite_panel():
    st.markdown(
        "**Complete scientific rewrite for a target journal.** Upload the "
        "manuscript, the SI and any other files (referee reports, cover "
        "letter, data notes). A staged editorial pipeline builds an evidence "
        "ledger, decides the headline and story, rewrites every section to "
        "the journal's conventions, writes the front matter last, and then "
        "audits itself: a mechanical number/citation/label check plus an "
        "adversarial referee. It will **not** change any number, unit, "
        "sample size or measured claim, will not invent references, and "
        "marks everything it needs from you as `[AUTHOR: ...]`.")
    st.caption("Best input is the .docx (PDF extraction can garble numbers "
               "and superscript citations). Everything uploaded here goes "
               "to the Claude backend - keep confidential referee material "
               "out if that is not acceptable.")

    c1, c2 = st.columns(2)
    with c1:
        ms_up = st.file_uploader("Manuscript (required)",
                                 type=["docx", "pdf", "txt", "md"],
                                 key="rw_ms_up")
        ms_text, ms_name = "", ""
        if ms_up is not None:
            try:
                ms_text = extract_uploaded_text(ms_up)
                ms_name = ms_up.name
            except Exception as e:
                st.error(f"Could not read {ms_up.name}: {e}")
        if not ms_text:
            ptxt, pname = project_file_picker("Manuscript", "rw_ms_proj")
            if ptxt:
                ms_text, ms_name = ptxt, pname
        if ms_text:
            secs = rw_split_sections(ms_text)
            chips = " · ".join(f"{c} {len(t.split())}w" for c, t in secs)
            st.caption(f"**{ms_name}** - {len(ms_text.split()):,} words. "
                       f"Detected: {chips}")
            if len(ms_text.split()) < 300:
                st.warning("Very little text found (scanned PDF?).")
    with c2:
        oth_ups = st.file_uploader(
            "SI, data tables (CSV/XLSX) and other files (optional, several)",
            type=["docx", "pdf", "txt", "md", "csv", "tsv", "xlsx", "xls"],
            accept_multiple_files=True, key="rw_oth_up",
            help="Data tables are read column by column: the figure engine "
                 "can plot measured sweeps, time series and device statistics "
                 "straight from the file, with the file as provenance.")
        others, tables = [], []
        for f in oth_ups or []:
            guess = ("Data table (CSV/XLSX)" if re.search(
                r"\.(csv|tsv|xlsx|xls)$", f.name, re.I) else
                "Supplementary Information" if re.search(
                r"\bsi\b|supp|supporting|esi", f.name, re.I) else
                "Referee reports" if re.search(
                    r"review|referee|report|decision", f.name, re.I) else
                "Cover letter" if re.search(r"cover", f.name, re.I)
                else "Other")
            role = st.selectbox(f"Role of '{f.name}'", RW_ROLES,
                                index=RW_ROLES.index(guess),
                                key=f"rw_role_{f.name}")
            try:
                if re.search(r"\.(csv|tsv|xlsx|xls)$", f.name, re.I):
                    tab = extract_uploaded_table(f)
                    tables.append(tab)
                    others.append({"name": f.name, "role": role,
                                   "text": table_to_text(tab)})
                else:
                    others.append({"name": f.name, "role": role,
                                   "text": extract_uploaded_text(f)})
            except Exception as e:
                st.warning(f"Could not read {f.name}: {e}")
        ap = active_project()
        if ap:
            pf = [p.name for p in project_files(ap)]
            picks = st.multiselect(f"...or from project '{ap}'", pf,
                                   key="rw_oth_proj")
            for name in picks:
                role = st.selectbox(f"Role of '{name}'", RW_ROLES,
                                    key=f"rw_prole_{name}")
                try:
                    others.append({"name": name, "role": role,
                                   "text": extract_path_text(
                                       PROJECTS_DIR / ap / name)})
                except Exception as e:
                    st.warning(f"Could not read {name}: {e}")
        _saved_dirs = [ANSWERS_DIR / "perodeg_data", ANSWERS_DIR / "analytics",
                       ANSWERS_DIR / "figures_out"]
        _saved = sorted([p for d in _saved_dirs if d.exists() for p in d.rglob("*")
                         if p.suffix.lower() in (".csv", ".tsv", ".xlsx")],
                        key=lambda p: p.stat().st_mtime, reverse=True)[:60]
        if _saved:
            _pick_t = st.multiselect(
                "...or data tables saved by PeroDeg / Analytics",
                [str(p.relative_to(ANSWERS_DIR)) for p in _saved], key="rw_saved_tables",
                help="Exports from PeroDeg (stability, I-V, energy yield) and Analytics "
                     "become file-backed sources for the data figures.")
            for _rel in _pick_t:
                try:
                    _tab = extract_path_table(ANSWERS_DIR / _rel)
                    tables.append(_tab)
                    others.append({"name": Path(_rel).name, "role": "Data table (CSV/XLSX)",
                                   "text": table_to_text(_tab)})
                except Exception as e:
                    st.warning(f"Could not read {_rel}: {e}")
        if tables:
            st.caption("Data tables: " + " · ".join(
                f"{t['name']} ({t['n_rows']} rows x {len(t['columns'])} cols)"
                for t in tables))
        if others:
            st.caption(" · ".join(f"{o['name']} ({o['role']}, "
                                  f"{len(o['text'].split()):,}w)"
                                  for o in others))
        if not any(o["role"] == "Supplementary Information" for o in others):
            st.caption("No SI given - statistics and controls in the SI are "
                       "what usually make a paper defensible at these "
                       "journals.")

    o1, o2 = st.columns(2)
    with o1:
        journal = st.selectbox("Target journal", list(JOURNAL_PROFILES),
                               key="rw_journal")
        jtype = st.selectbox("Article type",
                             JOURNAL_PROFILES[journal]["types"],
                             key="rw_jtype")
        with st.expander("Style profile used"):
            st.caption(JOURNAL_PROFILES[journal]["confidence"]
                       + f" (last verified {RW_PROFILES_VERIFIED})")
            st.text(JOURNAL_PROFILES[journal]["text"])
        level = st.radio("Restructuring level", list(RW_LEVELS),
                         index=1, key="rw_level")
    with o2:
        headline = st.text_input("Your headline claim (optional, one "
                                 "sentence)", key="rw_headline")
        instructions = st.text_area(
            "Instructions to the editor (optional)", height=90,
            key="rw_instr",
            placeholder="e.g. keep the stability story first; we cannot "
                        "cite the certified value yet; referee 2 asked for "
                        "statistics - they are in Fig. S9")
        pause_plan = st.checkbox("Pause after the editorial plan for my "
                                 "approval", value=True, key="rw_pause")
        lib_label = st.selectbox(
            "Use my library", list(RW_LIB_MODES),
            index=2 if index_ok else 0, disabled=not index_ok, key="rw_lib_mode",
            help="Passages from your indexed corpus are retrieved per section and "
                 "numbered [L1], [L2], ...; the writer may cite them for positioning, "
                 "precedent and comparison. Every [Ln] is checked against what was "
                 "retrieved; literature values are flagged for verification; the "
                 "bundle lists the references to merge. 'Compare' also lets Results "
                 "sections and data figures use library values.")
        lib_mode_key = RW_LIB_MODES[lib_label]
        k_lib = st.slider("Library passages per section", 6, 30, 14, key="rw_k_lib",
                          disabled=lib_mode_key == "off")
        auto_repair = st.checkbox("Auto-apply the referee's exact-match "
                                  "repairs", value=True, key="rw_repair")
        draw_figs = st.checkbox("🎨 Draw the figures the text calls for "
                                "(schematics, mechanisms, workflows; data "
                                "figures only from ledger numbers)",
                                value=True, key="rw_figs")
        pause_figs = st.checkbox("Pause after the figure plan for my "
                                 "approval", value=True, key="rw_pause_figs")

    rw_where = (restrict_search_widget("rwlib")
                if (index_ok and lib_mode_key != "off") else None)
    n_sec_guess = 7
    st.caption(f"Estimated calls: 1-3 digest + (SI chunks) + 1 plan + "
               f"~{n_sec_guess} sections + 1 front matter + 1 referee "
               f"= ~12 calls with **{model_label}**. Several minutes; "
               "progress is saved after every call and can be resumed.")

    # ---- state / resume
    state = st.session_state.get("rw")
    if state is None:
        last = rw_load_last_state()
        if last and last.get("status") != "complete":
            if st.button("↩️ Resume the unfinished job "
                         f"({last['inputs'].get('ms_name', '?')}, "
                         f"{last.get('status')})", key="rw_resume_disk"):
                st.session_state["rw"] = last
                st.rerun()

    def _start_state():
        evid, nonev = [ms_text], []
        for o in others:
            (evid if o["role"] in RW_EVIDENCE_ROLES else nonev).append(o["text"])
        secs = rw_split_sections(ms_text)
        ref_block = next((t for c, t in secs if c == "References"), "")
        body = "\n\n".join(t for c, t in secs
                           if c not in ("References",))
        opts = {"journal": journal, "article_type": jtype,
                "level": RW_LEVELS[level], "headline": headline.strip(),
                "instructions": instructions.strip(),
                "pause_plan": pause_plan, "ground": lib_mode_key != "off",
                "lib_mode": lib_mode_key, "k_lib": int(k_lib),
                "auto_repair": auto_repair, "plan_edits": "",
                "figures": draw_figs, "vision": True, "max_figs": 3,
                "pause_figs": pause_figs}
        sig = _rwhash.sha1((ms_text + "".join(o["text"] for o in others)
                            + journal + jtype).encode("utf-8", "replace")
                           ).hexdigest()[:12]
        return {"sig": sig, "status": "running", "stages": {},
                "opts": opts, "log": [], "plan_approved": False,
                "registry": {}, "where": rw_where,
                "inputs": {"ms_name": ms_name, "ms_text": ms_text,
                           "ms_body": body, "ref_block": ref_block,
                           "ms_sections": secs,
                           "secmap": rw_section_map(secs),
                           "others": others, "tables": tables,
                           "evidence_texts": [t for t in evid if t],
                           "nonevidence_texts": [t for t in nonev if t]}}

    def _drive(st_state):
        with st.status(f"Rewriting for {st_state['opts']['journal']}...",
                       expanded=True) as box:
            try:
                rw_run(st_state, box)
                if st_state["status"] == "awaiting_plan":
                    box.update(label="Plan ready - review it below",
                               state="complete")
                elif st_state["status"] == "awaiting_figures":
                    box.update(label="Figure plan ready - review it below",
                               state="complete")
                elif st_state["status"] == "complete":
                    box.update(label="Rewrite complete", state="complete")
            except Exception as e:
                rw_log(st_state, f"ERROR: {e}")
                rw_save_state(st_state)
                st_state["status"] = "error"
                box.update(label=f"Stopped: {e}", state="error")
                st.error(f"Stopped at a model call: {e}. Progress is saved "
                         "- press Resume.")
        st.session_state["rw"] = st_state

    b1, b2 = st.columns([2, 1])
    with b1:
        can_run = bool(ms_text) and bool(api_key.strip())
        if state and state.get("status") in ("error", "running") and \
                state["stages"]:
            if st.button("▶️ Resume rewrite", type="primary",
                         key="rw_resume"):
                state["status"] = "running"
                _drive(state)
                st.rerun()
        elif st.button("🧬 Rewrite manuscript", type="primary",
                       disabled=not can_run, key="rw_go"):
            _drive(_start_state())
            st.rerun()
    with b2:
        if state and st.button("🗑️ Start over", key="rw_clear"):
            st.session_state.pop("rw", None)
            st.rerun()

    state = st.session_state.get("rw")
    if not state:
        return

    # ---- plan checkpoint
    if state["status"] == "awaiting_plan":
        st.markdown("---")
        st.markdown("## Editorial plan - awaiting your approval")
        if state.get("manifest_fallback"):
            st.warning("The plan's section manifest could not be parsed; a "
                       "default manifest from the detected sections will be "
                       "used (the plan text still guides the writing).")
        man = state["stages"]["manifest"]
        st.markdown(f"**Headline:** {man.get('headline') or '(see plan)'}  \n"
                    f"**Recommended title:** "
                    f"{man.get('title_recommended') or '(see plan)'}")
        rows = [{"id": s["id"], "kind": s["kind"], "heading": s["heading"],
                 "target words": s["target_words"],
                 "sources": ", ".join(s["source_ids"]),
                 "SI promoted": ", ".join(s["si_promote"])}
                for s in man["sections"]]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        with st.expander("Full plan text", expanded=False):
            st.markdown(_rw_strip_json(state["stages"]["plan"]))
        edits = st.text_area("Edits to the plan (become binding "
                             "instructions)", key="rw_plan_edits",
                             placeholder="e.g. keep the XPS section; the "
                                         "headline must stay the stability "
                                         "result")
        a1, a2 = st.columns(2)
        with a1:
            if st.button("✅ Approve and continue", type="primary",
                         key="rw_approve"):
                state["plan_approved"] = True
                state["status"] = "running"
                _drive(state)
                st.rerun()
        with a2:
            if st.button("🔁 Regenerate plan with these edits",
                         key="rw_regen"):
                state["opts"]["plan_edits"] += (
                    "\nBINDING EDITS FROM THE AUTHOR: " + edits.strip())
                state["stages"].pop("plan", None)
                state["stages"].pop("manifest", None)
                state.pop("manifest_fallback", None)
                state["status"] = "running"
                _drive(state)
                st.rerun()
        return

    if state["status"] == "awaiting_figures":
        render_fig_checkpoint(state, "rw", "rw", _drive)
        return

    if state["status"] != "complete":
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
        return

    # ---- outputs
    stg = state["stages"]
    audit = stg.get("audit_after") or stg["audit"]
    verdict_m = re.search(r"## Verdict\s*\n(.+)", stg["referee"])
    verdict = verdict_m.group(1).strip() if verdict_m else "(see report)"
    st.markdown("---")
    if "DO NOT USE" in verdict or audit["counts"]["CRITICAL"]:
        st.error(f"Integrity: {verdict}")
    elif "NEEDS REPAIR" in verdict or audit["counts"]["MAJOR"]:
        st.warning(f"Integrity: {verdict}")
    else:
        st.success(f"Integrity: {verdict}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Words", len(stg["manuscript_md"].split()))
    m2.metric("[AUTHOR] items", len(audit["markers"]))
    m3.metric("Critical / major flags",
              f"{audit['counts']['CRITICAL']} / {audit['counts']['MAJOR']}")
    m4.metric("Sections", len(stg["manifest"]["sections"]))
    t1, t2, t3, t4, t5, t6 = st.tabs(["📄 Manuscript", "📝 Change log",
                                      "🔬 Integrity report", "✅ Author items",
                                      "🧭 Plan & ledger", "📚 Library"])
    with t1:
        st_md_figs(stg["manuscript_md"])
        if stg.get("figures"):
            with st.expander("Figures generated - review, download, redraw"):
                render_fig_regen_ui(state, "rw", "rwf")
    with t2:
        m7 = re.search(r"## 7\..*?(?=## 8\.|$)", stg["plan"], re.S)
        if m7:
            st.markdown(m7.group(0))
        for s in stg["manifest"]["sections"]:
            ch = stg["sections_out"][s["id"]]["changes"]
            if ch:
                st.markdown(f"**{s['heading']}**\n\n{ch}")
        rep = stg["repairs"]
        if rep["applied"] or rep["skipped"]:
            st.markdown(f"**Referee repairs:** {len(rep['applied'])} applied, "
                        f"{len(rep['skipped'])} not applied (text not found "
                        "exactly once - see the report).")
    with t3:
        if stg.get("checklist"):
            ck = stg["checklist"]["counts"]
            st.markdown(f"### PV reporting checklist - {ck['OK']} present, "
                        f"{ck['MINOR']} minor, {ck['MAJOR']} major gaps")
            st.dataframe(stg["checklist"]["rows"], use_container_width=True,
                         hide_index=True)
        st.markdown("### Mechanical audit"
                    + (" (after repairs)" if stg.get("audit_after") else ""))
        if audit["rows"]:
            st.dataframe(audit["rows"], use_container_width=True,
                         hide_index=True)
        else:
            st.success("No mechanical flags.")
        st.caption("Mechanical PASS is necessary, not sufficient - a source "
                   "number attached to the wrong device passes here and is "
                   "caught only by the referee.")
        st.markdown(stg["referee"])
    with t4:
        for i, mk in enumerate(dict.fromkeys(audit["markers"]), 1):
            st.markdown(f"{i}. {mk}")
        if not audit["markers"]:
            st.caption("No [AUTHOR] items.")
    with t5:
        st.markdown(stg["plan"])
        with st.expander("Evidence ledger"):
            st.markdown(stg["ledger"])
            if stg["si_ledger"]:
                st.markdown(stg["si_ledger"])
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
    with t6:
        _lc = rw_lib_cited(stg["manuscript_md"])
        _reg = state.get("registry", {})
        st.markdown(f"**{len(_lc)} library paper(s) cited as [Ln] · {len(_reg)} retrieved · "
                    f"mode: {state['opts'].get('lib_mode', 'off')}**")
        if _lc:
            st.markdown(rw_lib_reference_list(state, _lc))
            _lhits = [v["hit"] for v in _reg.values() if v["n"] in _lc and v.get("hit")]
            if _lhits:
                st.caption("Resolve the cited library papers to complete references "
                           "(BibTeX / RIS, formal entries via DOI):")
                render_reference_exporter(_lhits, key="rwlib")
        elif state["opts"].get("ground"):
            st.info("No library passage was cited. Try 'Cite and compare' mode, more "
                    "passages per section, or widen the search filter.")
        else:
            st.info("Library grounding was off for this job.")
        if stg.get("lib_plan"):
            with st.expander("Passages given to the plan (positioning)"):
                st.markdown(stg["lib_plan"][:30000])
        for _sid, _ctx in (stg.get("lib_passages") or {}).items():
            if _ctx:
                with st.expander(f"Passages given to section {_sid}"):
                    st.markdown(_ctx[:30000])
    d1, d2 = st.columns(2)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", state["inputs"]["ms_name"])[:40]
    with d1:
        doc = Document()
        md_to_docx(doc, stg["manuscript_md"])
        buf = io.BytesIO()
        doc.save(buf)
        st.download_button("⬇️ Manuscript only (Word)", buf.getvalue(),
                           file_name=f"rewrite_{stem}.docx",
                           mime="application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document", key="rw_dl1")
    with d2:
        doc2 = Document()
        md_to_docx(doc2, stg["bundle"])
        buf2 = io.BytesIO()
        doc2.save(buf2)
        st.download_button("⬇️ Full bundle: manuscript + change log + "
                           "integrity (Word)", buf2.getvalue(),
                           file_name=f"rewrite_bundle_{stem}.docx",
                           mime="application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document", key="rw_dl2")
    try:
        _tc = tracked_changes_bytes(
            state["inputs"]["ms_body"], stg["manuscript_md"],
            title=f"Tracked changes - {state['inputs']['ms_name']}",
            note="Word revisions turning the original main text into the rewrite. "
                 "Reordered material appears as deletions and insertions; accept or "
                 "reject in Word's Review pane.")
        st.download_button("⬇️ Tracked changes vs the original (Word revisions)", _tc,
                           file_name=f"rewrite_tracked_{stem}.docx",
                           mime="application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document", key="rw_dl_tc")
    except Exception as e:
        st.caption(f"Tracked-changes export unavailable: {e}")
    render_submission_packet(state)
    if state.get("hits"):
        show_sources(state["hits"], key_prefix="rw")
    st.caption("Saved to the answers folder and the session history in the "
               f"Ask tab; job folder: answers/rewrite_jobs/{state['sig']}/")
    render_followup(
        "rw_" + state["sig"], "Discuss and revise this manuscript",
        {"manuscript": stg["manuscript_md"]},
        sources_text=stg["ledger"][:30000] + ("\n\n" + stg["si_ledger"][:10000]
                                             if stg["si_ledger"] else "")
                     + ("\n\nLIBRARY PASSAGES [Ln]:\n"
                        + "\n\n".join(stg["lib_passages"].values())[:30000]
                        if stg.get("lib_passages") else ""),
        pool_texts=state["inputs"]["evidence_texts"] + [stg["ledger"]]
                   + list(stg.get("lib_passages", {}).values()),
        apply_fn=lambda edits, pool: fu_apply_rw(state, edits, pool),
        hint="Tell the editor what is wrong or what you now know; exact edits are "
             "applied to the sections, the integrity audit and the bundle are "
             "rebuilt without further model calls. Numbers must come from the "
             "evidence or from your messages.")


# ==========================================================================
# Review / Perspective writer  (Draft tab mode)
#
# synopsis -> corpus sweep (sub-queries -> retrieval -> numbered paper
# registry) -> literature map (themes, controversies, gaps) -> outline
# (checkpoint) -> per-section critical synthesis citing ONLY retrieved
# papers -> front matter -> reference list from the registry -> citation
# and number-provenance audit -> bundle. Reuses the rewrite pipeline's
# retry, parsing and persistence helpers (rw_*).
# ==========================================================================
RV_DIR = ANSWERS_DIR / "review_jobs"

REVIEW_TYPES = {
    "Review (comprehensive, critical)": {
        "words": 8000, "sections": (7, 10),
        "brief": "A critical, comprehensive review: organise the field by "
                 "themes and mechanisms rather than by paper; compare and "
                 "judge approaches quantitatively; identify controversies "
                 "and what evidence would settle them; end with an "
                 "evidence-based outlook."},
    "Perspective (argument-driven)": {
        "words": 4000, "sections": (5, 7),
        "brief": "An argument-driven perspective: one clear thesis stated "
                 "early, defended with selected evidence, opposing views "
                 "represented fairly, concrete recommendations for the "
                 "field, and a forward-looking close. Selective citation "
                 "is expected; the argument carries the piece."},
    "Mini-review / Focus review": {
        "words": 3000, "sections": (4, 6),
        "brief": "A short, tightly scoped review of one sub-topic: recent "
                 "advances, the two or three key comparisons, remaining "
                 "barriers. Every paragraph earns its place."},
    "Roadmap / Outlook": {
        "words": 4000, "sections": (5, 7),
        "brief": "A roadmap: where the field is (quantified), where it "
                 "must get to (targets with numbers only if sourced), the "
                 "milestones and bottlenecks in between, and who/what is "
                 "needed. Time horizons stated explicitly."},
}

REVIEW_VENUES = {
    "Chemical Society Reviews": {
        "confidence": "Medium: CSR guidance is stable on scope (critical, "
                      "authoritative) but numeric limits vary by review "
                      "type - confirm on the current author guidelines.",
        "front_matter": ["Title options", "Abstract", "Key learning points "
                         "(if Tutorial Review)", "Keywords"],
        "text": "CHEMICAL SOCIETY REVIEWS: Review Articles are long, critical "
                "and authoritative (typically 10,000-20,000 words, 150-300 "
                "references, many display items); Tutorial Reviews are "
                "shorter and pedagogical and open with 3-5 'Key learning "
                "points'. Abstract <= ~250 words, unreferenced. Numbered "
                "sections with descriptive headings; an Introduction that "
                "defines scope and prior reviews explicitly; a Conclusions "
                "and Outlook section. RSC referencing (superscript numeric). "
                "Editors reward: genuine critical evaluation (not a "
                "catalogue), comparison tables with normalised metrics, "
                "schematic figures that synthesise, honest treatment of "
                "conflicting results. Rejected: paper-by-paper summaries, "
                "self-promotion, missing recent work.",
    },
    "Nature Reviews (Materials / Clean Technology / Chemistry)": {
        "confidence": "Medium-high on Key points and the two article types; "
                      "medium on word counts.",
        "front_matter": ["Title options", "Abstract", "Key points"],
        "text": "NATURE REVIEWS: Reviews (~6,000-8,000 words, ~150-200 "
                "references, up to ~8 display items) and Perspectives "
                "(~4,000-6,000 words, personal viewpoint clearly argued). "
                "Abstract ~150-200 words, unreferenced, accessible to a broad "
                "materials/energy readership. 'Key points': 4-6 bullets "
                "summarising the main messages. Introduction without a "
                "heading; short thematic headings; boxes for background; a "
                "'Conclusions' (Review) or 'Outlook' section. Nature style: "
                "no 'novel', concrete over vague, present tense for "
                "established facts. Editors reward: a clear organising "
                "framework new to the reader, balanced coverage across "
                "groups, explicit statements of what remains unknown. "
                "Rejected: exhaustive lists, group-centric coverage, lack of "
                "a synthesising message.",
    },
    "Joule / Matter (Perspective or Review)": {
        "confidence": "High on the Cell Press front-matter set; medium on "
                      "lengths.",
        "front_matter": ["Title options", "Summary", "Context & Scale",
                         "Highlights", "eTOC blurb", "Keywords"],
        "text": "CELL PRESS (Joule Perspective/Review; Matter): 'Summary' <= "
                "~150 words unreferenced; 'Context & Scale' (~150 words, lay "
                "audience, energy-transition relevance); Highlights 3-4 "
                "bullets <= 85 characters; eTOC blurb ~50 words third person. "
                "Perspectives ~4,000-6,000 words with a strong argument and "
                "system-level 'so what' (deployment, cost, scale); Reviews "
                "longer with a Cell-style structure (Introduction, thematic "
                "sections, Conclusions/Outlook). Numeric superscript "
                "references. Editors reward: connecting the science to "
                "energy systems, honest quantification of the gap to "
                "deployment, a memorable framework figure. Rejected: "
                "materials-only surveys without the energy 'so what'.",
    },
    "Energy & Environmental Science (Review / Perspective)": {
        "confidence": "High on Broader context; medium on lengths.",
        "front_matter": ["Title options", "Abstract", "Broader context"],
        "text": "RSC EES: Reviews and Perspectives carry an abstract (<= ~250 "
                "words) and a REQUIRED 'Broader context' paragraph "
                "(~100-200 words, non-specialist, energy/environment "
                "significance). Perspectives are argument-led (~5,000-8,000 "
                "words); Reviews comprehensive with a strong benchmarking "
                "culture (comparison tables of efficiency/stability with "
                "protocols). Sections: Introduction; thematic sections; "
                "Conclusions and outlook. RSC referencing. Editors reward: "
                "quantitative benchmarking, mechanism-level insight, "
                "standardised-protocol awareness (ISOS), sustainability "
                "framing. Rejected: catalogues, generic Broader context.",
    },
    "Advanced Energy Materials / Advanced Materials (Review / Progress "
    "Report)": {
        "confidence": "High on numbered structure and abstract tense; medium "
                      "on lengths.",
        "front_matter": ["Title options", "Abstract", "Keywords",
                         "Table-of-contents text"],
        "text": "WILEY ADVANCED FAMILY: Reviews (long, comprehensive) and "
                "Progress Reports / Perspectives (shorter, focused). "
                "Abstract <= ~200 words, unreferenced, PRESENT tense and "
                "impersonal style. Numbered sections (1. Introduction ... "
                "n. Conclusion and Outlook). Keywords 3-5; ToC text 50-60 "
                "words + graphic. Comparison tables and summarising "
                "schematics expected. Editors reward: a materials-science "
                "framework connecting structure to performance, balanced "
                "coverage, a clear outlook. Rejected: past-tense 'we' "
                "abstracts, uncritical lists, missing recent literature.",
    },
    "ACS Energy Letters (Perspective / Focus Review)": {
        "confidence": "Medium-high on formats; medium on caps.",
        "front_matter": ["Title options", "Abstract"],
        "text": "ACS ENERGY LETTERS: Perspectives (~4,000-6,000 words, a "
                "personal, forward-looking argument, <= ~6 display items) and "
                "Focus Reviews (longer, tightly scoped). Abstract <= ~150 "
                "words, unreferenced; TOC graphic required. Sparse headings, "
                "argumentative prose, energy relevance in the first "
                "paragraph. ACS numeric superscript references. Editors "
                "reward: a sharp thesis, quantitative comparison, brevity. "
                "Rejected: unfocused surveys, missing energy consequence.",
    },
    "Generic high-impact review venue": {
        "confidence": "Deliberately generic.",
        "front_matter": ["Title options", "Abstract", "Key points",
                         "Keywords"],
        "text": "GENERIC: Abstract <= 200 words unreferenced; 4-6 Key points; "
                "Introduction defining scope and prior reviews; thematic "
                "sections with claim-shaped headings; comparison tables; "
                "Conclusions and Outlook; numbered references in order of "
                "citation. Critical synthesis over summary; balanced "
                "coverage; explicit gaps and recommendations.",
    },
}

RV_QUERIES_SYSTEM = """\
You design a literature-search plan for a scientific review or perspective. From the SYNOPSIS, produce 10-16 distinct retrieval queries that together cover: the core topic and its sub-topics; mechanisms; materials/methods; key metrics and benchmarks; stability/scale-up/cost where relevant; known controversies or conflicting results; adjacent fields the piece should connect to; and outlook/roadmap questions. Each query is a concise phrase a semantic search over research papers would understand (8-16 words), not a question. Respond with ONLY a JSON array of strings."""

RV_MAP_SYSTEM = """\
You are the lead author's research assistant preparing the LITERATURE MAP for a review or perspective. You receive the SYNOPSIS and NUMBERED PAPERS retrieved from the author's own literature corpus (each: [n] title, file, best passage). Organise them into the structure of the field. Use ONLY these papers; cite them ONLY by their [n]; never invent a paper or a number. Where the corpus is thin on a sub-topic the synopsis needs, say so as a GAP - that is valuable information.
OUTPUT (markdown; keep headings exactly)
# LITERATURE MAP
## Themes  (4-8 themes; for each: one-paragraph description of the state of knowledge, the key papers [n] and what each contributes, the best quantitative anchors quoted verbatim with their [n])
## Controversies and conflicting results  (each with the papers on each side [n] and what evidence would settle it)
## Trajectory  (how the field moved over time, by year where the files show it)
## Gaps in the corpus  (sub-topics the synopsis needs but the retrieved papers do not cover well - phrase each as '[AUTHOR: consider adding papers on ...]')
## Candidate theses  (3 possible organising arguments for the piece, one sentence each, with the [n] that would carry them)
Finish with <<<END MAP>>>."""

RV_OUTLINE_SYSTEM = """\
You are a senior editor at {VENUE} planning a {TYPE}. {TYPE_BRIEF}
You receive the SYNOPSIS, the author's own thesis/instructions (optional), the LITERATURE MAP built from the author's corpus (papers cited as [n]), and the VENUE PROFILE. Produce the OUTLINE the writing stage will execute. Principles: one organising thesis stated in the first section; thematic (not chronological, not paper-by-paper) structure; every section makes a claim and states what it compares; quantitative anchors from the map with their [n]; controversies represented fairly; an outlook grounded in the gaps; display items that synthesise (comparison tables, framework figures) described as concepts. Respect the venue's length and front-matter conventions. Never invent papers or numbers; anything the corpus lacks becomes an [AUTHOR: ...] item.
VENUE PROFILE: {PROFILE}
TARGET LENGTH: about {WORDS} words of main text.
OUTPUT (markdown; keep headings exactly; the fenced json MUST be valid JSON)
# OUTLINE
## 1. Thesis  (one sentence, then why it is the right organising idea for this venue)
## 2. Title options  (3-5, recommended one marked)
## 3. Section plan
```json
{"thesis": "...", "title_recommended": "...", "title_options": ["..."],
 "sections": [
   {"id": "R1", "heading": "Introduction: <claim-shaped>", "target_words": 600, "brief": "2-3 sentences: what this section argues and compares", "query": "retrieval phrase for extra evidence, 8-16 words", "cite": [3, 7, 12], "display_items": ["Table 1: comparison of X across Y (columns ...)"]},
   {"id": "R2", "heading": "...", "target_words": 900, "brief": "...", "query": "...", "cite": [1, 4], "display_items": []}
 ]}
```
Rules: 4-10 sections; the last is Conclusions/Outlook (or as the venue names it); target_words sum to about the target length; 'cite' lists the [n] from the map this section should draw on (numbers only); every section has a query.
## 4. Display items  (each proposed table/figure with the [n] it draws on)
## 5. Author decisions  (numbered: gaps to fill with papers, permissions, positions the author must take)
Finish with <<<END OUTLINE>>>."""

RV_SECTION_SYSTEM = """\
You write ONE section of a {TYPE} for {VENUE}, from the author's own literature corpus. You receive the outline (thesis, this section's brief and heading), numbered EVIDENCE PASSAGES retrieved for this section, the tail of the previous section, and the venue profile.
INTEGRITY RULES (audited mechanically)
1. Cite ONLY the [n] numbers given in the evidence passages, exactly as given (they are global to the whole article). Never invent a citation, never renumber, never cite a paper you were not given.
2. Every number, unit, value or statistic you write must appear in the passages; attach the [n] of its source in the same sentence. Never compute, round or estimate; if a comparison needs a value the passages lack, write [AUTHOR: add value for ...].
3. Attribute claims to their source ([n]) and preserve each source's own hedging; do not present one group's interpretation as consensus.
4. No 'novel', 'unprecedented', 'record', 'breakthrough' unless quoting a source with its [n].
5. Anything the corpus lacks becomes [AUTHOR: <precise request>].
CRAFT
- Critical synthesis, not summary: compare approaches, explain WHY results differ (materials, protocols, measurement conditions), state what is established vs contested, and what a decisive experiment would be.
- Paragraphs: claim first, evidence with [n] and the numbers, interpretation, bridge. 80-180 words each. Claim-shaped subheadings (###) allowed inside long sections.
- Where the brief proposes a comparison table, draft it as a markdown table using ONLY values from the passages, each row carrying its [n]; otherwise write a '[AUTHOR: table concept - columns ...]' line.
- Respect target_words +/- 15%. Follow the venue profile's tense/person/style. Do not repeat the previous section's tail.
OUTPUT (exact delimiters)
<<<SECTION>>>
## <heading>
<prose>
<<<END SECTION>>>
<<<CITED>>>
comma-separated list of the [n] you actually cited
<<<END CITED>>>"""

RV_FRONT_SYSTEM = """\
You write the front matter of a {TYPE} for {VENUE} AFTER the body exists. You receive the venue profile, the outline (thesis, titles) and the complete body. Produce, with these exact headings: '# <recommended title>', '## Title options (ranked)', '## Abstract' (obey the venue's word limit and tense; unreferenced unless the profile says otherwise; state the thesis and the two or three most important quantitative anchors from the body verbatim), then one '## <element>' per required front-matter element from the profile (Key points / Highlights / Context & Scale / Broader context / ToC text / Keywords / Key learning points as applicable, with their length rules), then '## Graphical abstract concept' (one paragraph describing a framework figure the author could draw), then '## Notes for the author' (word counts vs limits; every [AUTHOR: ...] you wrote). Integrity: no number that is not in the body; no citations not in the body; no new claims. Finish with <<<END FRONT>>>."""


# ---------------------------------------------------------------- helpers
def _rv_year_of(file):
    m = re.search(r"_((?:19|20)\d{2})_", str(file) or "")
    return m.group(1) if m else ""


def _rv_paper_key(h):
    return h["meta"].get("doc_sig") or h["meta"].get("file") or h["meta"].get("title")


def rv_register(state, hits):
    """Assign global paper numbers to hits; returns the numbers (one per
    hit, paper-level). Stores up to 3 passages per paper."""
    reg = state["registry"]          # key -> {"n", "title", "file", "passages"}
    nums = []
    for h in hits:
        key = _rv_paper_key(h)
        if key not in reg:
            reg[key] = {"n": len(reg) + 1, "title": h["meta"].get("title", "?"),
                        "file": h["meta"].get("file", ""),
                        "year": _rv_year_of(h["meta"].get("file", "")),
                        "passages": [], "hit": h}
        ent = reg[key]
        txt = h.get("text", "")
        if txt and txt not in ent["passages"] and len(ent["passages"]) < 3:
            ent["passages"].append(txt)
        nums.append(ent["n"])
    return nums


def rv_context(state, hits, nums, max_chars=60000):
    """Numbered passages (paper-level numbers), deduplicated."""
    parts, seen, total = [], set(), 0
    for n, h in zip(nums, hits):
        key = (n, h.get("text", "")[:100])
        if key in seen:
            continue
        seen.add(key)
        m = h["meta"]
        block = (f"[{n}] {m.get('title', '?')} ({m.get('file', '')}, "
                 f"p.{m.get('page_start', '?')}-{m.get('page_end', '?')})\n"
                 f"{h.get('text', '')}")
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


def rv_hits_for_numbers(state, nums):
    """Representative hits (stored passages) for registry numbers."""
    out, ns = [], []
    by_n = {v["n"]: v for v in state["registry"].values()}
    for n in nums:
        ent = by_n.get(n)
        if not ent:
            continue
        for p in ent["passages"][:2]:
            h = dict(ent["hit"])
            h["text"] = p
            out.append(h)
            ns.append(n)
    return out, ns


def rv_reference_list(state, cited):
    reg = state["registry"]
    try:
        journals = load_journals()
    except Exception:
        journals = {}
    by_n = {v["n"]: (k, v) for k, v in reg.items()}
    lines = []
    for n in sorted(cited):
        if n not in by_n:
            lines.append(f"[{n}] [AUTHOR: citation number not in the "
                         "retrieved set - remove or replace]")
            continue
        key, v = by_n[n]
        fam = journals.get(key, "") if isinstance(journals, dict) else ""
        extra = ", ".join(x for x in (v["year"], fam) if x)
        lines.append(f"[{n}] {v['title']}" + (f" ({extra})" if extra else "")
                     + f" — file: {v['file']} [AUTHOR: complete the "
                     "bibliographic reference]")
    return "\n".join(lines)


def rv_cited_numbers(text):
    return {int(c) for c in _rw_citations(text) if c.isdigit()}


def rv_audit(state, article_md, passages_by_section):
    rows = []
    clean = RW_AUTHOR_RE.sub("", FIG_IMG_RE.sub("", article_md))
    valid = {v["n"] for v in state["registry"].values()}
    for n in sorted(rv_cited_numbers(clean) - valid):
        rows.append({"severity": "CRITICAL", "kind": "Citation not in the "
                     "retrieved set", "item": f"[{n}]", "context": ""})
    pool = set()
    for txt in passages_by_section.values():
        pool |= rw_numbers_in(txt)
    seen = set()
    for m in RW_NUM_RE.finditer(clean):
        n = _rw_norm_num(m.group(0))
        core = n.lstrip("-")
        if re.fullmatch(r"\d{1,2}", core) or n in pool or n in seen:
            continue
        if re.fullmatch(r"(19|20)\d\d", core):          # years in prose
            continue
        pre = clean[max(0, m.start() - 14):m.start()].lower()
        if re.search(r"(fig|figure|table|section|ref)\.?\s*s?$", pre) or \
                re.search(r"\[[\d,\s\-–]*$", pre):
            continue
        seen.add(n)
        a, b = max(0, m.start() - 60), min(len(clean), m.end() + 60)
        rows.append({"severity": "MAJOR", "kind": "Number not found in any "
                     "retrieved passage", "item": n,
                     "context": re.sub(r"\s+", " ", clean[a:b]).strip()})
    low = clean.lower()
    for w in ("novel", "unprecedented", "record", "breakthrough",
              "for the first time", "proves"):
        if w in low:
            rows.append({"severity": "MINOR", "kind": "Strength word",
                         "item": w, "context": ""})
    order = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}
    rows.sort(key=lambda r: order[r["severity"]])
    return {"rows": rows,
            "counts": {k: sum(1 for r in rows if r["severity"] == k)
                       for k in order},
            "markers": RW_AUTHOR_RE.findall(article_md)}


def rv_save_state(state):
    try:
        d = RV_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        slim = dict(state)
        slim["registry"] = {k: {kk: vv for kk, vv in v.items() if kk != "hit"}
                            | {"hit": {"meta": v["hit"]["meta"],
                                       "text": v["hit"].get("text", "")[:2000],
                                       "score": float(v["hit"].get("score", 0))}}
                            for k, v in state["registry"].items()}
        (d / "state.json").write_text(_rwjson.dumps(slim, default=str),
                                      encoding="utf-8")
    except Exception:
        pass


def rv_validate_outline(o):
    if not isinstance(o, dict) or not isinstance(o.get("sections"), list):
        return None
    out = []
    for i, s in enumerate(o["sections"]):
        if not isinstance(s, dict) or not s.get("heading"):
            continue
        cites = []
        for c in s.get("cite") or []:
            try:
                cites.append(int(c))
            except (TypeError, ValueError):
                pass
        out.append({"id": str(s.get("id") or f"R{i + 1}"),
                    "heading": str(s["heading"])[:140],
                    "target_words": int(s.get("target_words") or 600),
                    "brief": str(s.get("brief") or ""),
                    "query": str(s.get("query") or s["heading"]),
                    "cite": cites,
                    "display_items": [str(x) for x in
                                      (s.get("display_items") or [])]})
    return out if len(out) >= 3 else None


def rv_default_outline(words):
    names = ["Introduction and scope", "State of the art", "Mechanisms and "
             "design principles", "Stability, scale-up and cost",
             "Controversies and open questions", "Conclusions and outlook"]
    per = max(300, words // len(names))
    return [{"id": f"R{i + 1}", "heading": h, "target_words": per,
             "brief": "(default outline - the plan JSON could not be parsed; "
                      "follow the literature map)", "query": h, "cite": [],
             "display_items": []} for i, h in enumerate(names)]


# ------------------------------------------------------------- the runner
def rv_run(state, status_box):
    _RW_UI["box"] = status_box
    opts, stg = state["opts"], state["stages"]
    venue = REVIEW_VENUES[opts["venue"]]
    rtype = REVIEW_TYPES[opts["rtype"]]
    where = state.get("where")

    def step(msg):
        status_box.write(msg)
        rw_log(state, msg)

    def _retrieve(q, k):
        hits = retrieve(q, k, where_extra=where)
        if opts.get("rerank") and hits:
            try:
                hits = rerank_hits(api_key.strip(), q, hits, max(6, k // 2))
            except Exception:
                pass
        return hits

    # ---- 1. queries + sweep ------------------------------------------------
    if "queries" not in stg:
        step("Planning the literature search...")
        raw = rw_call(RV_QUERIES_SYSTEM, f"SYNOPSIS:\n{opts['synopsis']}",
                      1500)
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            qs = [str(q) for q in _rwjson.loads(raw) if str(q).strip()]
        except Exception:
            qs = [opts["synopsis"][:200]]
        stg["queries"] = qs[:16]
        rv_save_state(state)
    if "sweep" not in stg:
        n_q = len(stg["queries"])
        for i, q in enumerate(stg["queries"], 1):
            step(f"Sweeping the corpus ({i}/{n_q}): {q[:60]}")
            try:
                hits = _retrieve(q, opts["k_map"])
            except Exception as e:
                rw_log(state, f"retrieval failed for '{q}': {e}")
                hits = []
            rv_register(state, hits)
        stg["sweep"] = {"papers": len(state["registry"])}
        rv_save_state(state)

    # ---- 2. literature map -------------------------------------------------
    if "map" not in stg:
        step(f"Literature map over {len(state['registry'])} papers...")
        entries = sorted(state["registry"].values(), key=lambda v: v["n"])
        listing = "\n\n".join(
            f"[{v['n']}] {v['title']} ({v['file']}"
            + (f", {v['year']}" if v["year"] else "") + ")\n"
            + (v["passages"][0][:700] if v["passages"] else "")
            for v in entries)[:110000]
        umsg = (f"SYNOPSIS:\n{opts['synopsis']}\n\nNUMBERED PAPERS FROM THE "
                f"AUTHOR'S CORPUS ({len(entries)}):\n\n{listing}\n\n"
                "Build the LITERATURE MAP now.")
        out = rw_call(RV_MAP_SYSTEM, umsg, 9000)
        stg["map"] = out.replace("<<<END MAP>>>", "").strip()
        rv_save_state(state)

    # ---- 3. outline (checkpoint) -------------------------------------------
    if "outline" not in stg:
        step("Outline...")
        sysm = (RV_OUTLINE_SYSTEM.replace("{VENUE}", opts["venue"])
                .replace("{TYPE_BRIEF}", rtype["brief"])
                .replace("{TYPE}", opts["rtype"])
                .replace("{PROFILE}", venue["text"])
                .replace("{WORDS}", str(opts["words"])))
        umsg = (f"SYNOPSIS:\n{opts['synopsis']}\n\nAUTHOR'S THESIS / "
                f"INSTRUCTIONS: {opts.get('instructions') or '(none)'}"
                f"{opts.get('plan_edits', '')}\n\nLITERATURE MAP:\n"
                f"{stg['map']}\n\nWrite the OUTLINE now.")
        out = rw_call(sysm, umsg, 6000)
        stg["outline"] = out.replace("<<<END OUTLINE>>>", "").strip()
        o = _rw_json_block(stg["outline"])
        secs = rv_validate_outline(o) if o else None
        if not secs:
            secs = rv_default_outline(opts["words"])
            state["outline_fallback"] = True
        stg["manifest"] = {"sections": secs,
                           "thesis": (o or {}).get("thesis", ""),
                           "title_recommended":
                           (o or {}).get("title_recommended", ""),
                           "title_options": (o or {}).get("title_options", [])}
        rv_save_state(state)
        if opts.get("pause") and not state.get("plan_approved"):
            state["status"] = "awaiting_plan"
            rv_save_state(state)
            return

    # ---- 4. sections -------------------------------------------------------
    secs = stg["manifest"]["sections"]
    stg.setdefault("sections_out", {})
    stg.setdefault("passages", {})
    prev_tail = ""
    for i, s in enumerate(secs):
        if s["id"] in stg["sections_out"]:
            prev_tail = stg["sections_out"][s["id"]]["text"][-1200:]
            continue
        step(f"Writing section {i + 1}/{len(secs)} - {s['heading']}")
        try:
            hits = _retrieve(s["query"], opts["k_sec"])
        except Exception as e:
            rw_log(state, f"retrieval failed: {e}")
            hits = []
        nums = rv_register(state, hits)
        extra_hits, extra_nums = rv_hits_for_numbers(state, s["cite"])
        ctx = rv_context(state, hits + extra_hits, nums + extra_nums)
        stg["passages"][s["id"]] = ctx
        sysm = (RV_SECTION_SYSTEM.replace("{VENUE}", opts["venue"])
                .replace("{TYPE}", opts["rtype"]))
        umsg = (f"THESIS OF THE ARTICLE: {stg['manifest']['thesis']}\n"
                f"SECTION: {s['id']} | heading: {s['heading']} | "
                f"target_words: {s['target_words']}\nBRIEF: {s['brief']}\n"
                f"DISPLAY ITEMS PROPOSED: "
                f"{'; '.join(s['display_items']) or '(none)'}\n\n"
                f"VENUE PROFILE: {venue['text']}\n\n"
                f"=== EVIDENCE PASSAGES (cite ONLY these numbers) ===\n"
                f"{ctx or '(no passages retrieved - write from the brief and mark every claim [AUTHOR: ...])'}\n\n"
                f"=== END OF THE PREVIOUS SECTION (do not repeat) ===\n"
                f"{prev_tail or '(first section)'}\n\n"
                "Write this section now, obeying the delimiters exactly.")
        text, complete, raw = rw_call_delimited(
            sysm, umsg, 8000, "<<<SECTION>>>", "<<<END SECTION>>>")
        cited, _ = _rw_between(raw, "<<<CITED>>>", "<<<END CITED>>>")
        if not text.lstrip().startswith("#"):
            text = f"## {s['heading']}\n\n{text}"
        if not complete:
            text += "\n\n[AUTHOR: this section was cut off by the output " \
                    "window - review its end]"
        stg["sections_out"][s["id"]] = {"text": text, "cited": cited or "",
                                        "complete": complete}
        prev_tail = text[-1200:]
        rv_save_state(state)

    # ---- 5. front matter ---------------------------------------------------
    if "front" not in stg:
        step("Front matter...")
        body_md = "\n\n".join(stg["sections_out"][s["id"]]["text"]
                              for s in secs)
        sysm = (RV_FRONT_SYSTEM.replace("{VENUE}", opts["venue"])
                .replace("{TYPE}", opts["rtype"]))
        umsg = (f"VENUE PROFILE:\n{venue['text']}\nREQUIRED ELEMENTS: "
                f"{', '.join(venue['front_matter'])}\n\n"
                f"OUTLINE (thesis + titles):\n"
                f"{_rw_strip_json(stg['outline'])[:8000]}\n\n"
                f"BODY (complete):\n{body_md}\n\n"
                "Write the front matter now, ending with <<<END FRONT>>>.")
        out = rw_call(sysm, umsg, 4000)
        stg["front"] = out.replace("<<<END FRONT>>>", "").strip()
        rv_save_state(state)

    # ---- 5b. figures ------------------------------------------------------
    if opts.get("figures", True) and "fig_done" not in stg:
        sec_texts = [(s["id"], s["heading"], stg["sections_out"][s["id"]]["text"])
                     for s in secs]
        _figs, sec_texts = fig_run_stage(
            state, "rv", sec_texts, _rw_strip_json(stg["outline"]), api_key,
            model, step, max_figs=int(opts.get("max_figs", 4)),
            vision=opts.get("vision", True))
        if _figs is None:            # figure plan awaiting approval
            return
        for sid, _h, md in sec_texts:
            stg["sections_out"][sid]["text"] = md
        stg["fig_done"] = True
        rv_save_state(state)

    # ---- 6. references + audit + bundle -----------------------------------
    if "bundle" not in stg:
        step("Reference list, audit, assembly...")
        body_md = "\n\n".join(stg["sections_out"][s["id"]]["text"]
                              for s in secs)
        article = stg["front"] + "\n\n" + body_md
        cited = rv_cited_numbers(RW_AUTHOR_RE.sub("", article))
        refs = rv_reference_list(state, cited)
        uncited = sorted(v["n"] for v in state["registry"].values()
                         if v["n"] not in cited)
        by_n = {v["n"]: v for v in state["registry"].values()}
        uncited_md = "\n".join(f"[{n}] {by_n[n]['title']} ({by_n[n]['file']})"
                               for n in uncited[:80])
        audit = rv_audit(state, article, stg["passages"])
        stg["audit"] = audit
        words = len(re.sub(r"^#.*$", "", body_md, flags=re.M).split())
        per_sec = "\n".join(
            f"- {s['heading']}: "
            f"{len(stg['sections_out'][s['id']]['text'].split())} words "
            f"(target {s['target_words']})" for s in secs)
        article_full = article + "\n\n## References\n\n" + refs
        bundle = (
            f"# {opts['rtype']} - {opts['venue']}\n\n"
            f"*Main text {words} words · {len(cited)} papers cited of "
            f"{len(state['registry'])} retrieved · [AUTHOR] items: "
            f"{len(audit['markers'])} · flags: "
            f"{audit['counts']['CRITICAL']} critical / "
            f"{audit['counts']['MAJOR']} major*\n\n"
            "> Draft grounded in the author's indexed corpus. Every [n] "
            "maps to a real paper in the reference list below; complete "
            "the bibliographic entries, verify every number against the "
            "cited paper, and resolve every [AUTHOR: ...] item. Corpus "
            "gaps flagged in the literature map are the reading list.\n\n"
            f"---\n\n{article_full}\n\n---\n\n"
            f"# Retrieved but not cited ({len(uncited)})\n\n{uncited_md}\n\n"
            f"---\n\n# Integrity audit\n\n{rw_audit_table(audit)}\n\n"
            f"## Section lengths\n{per_sec}\n\n---\n\n"
            f"# Literature map\n\n{stg['map']}\n\n---\n\n"
            f"# Outline\n\n{stg['outline']}\n\n---\n\n"
            f"# Search queries\n\n" + "\n".join(f"- {q}" for q in
                                                 stg["queries"]))
        if stg.get("figures"):
            bundle += "\n\n---\n\n# Figures generated\n\n" + fig_report_md(
                [stg["figures"][str(f["number"])] for f in stg["fig_manifest"]])
        stg["bundle"] = bundle
        stg["article_md"] = article_full
        hits_for_qa = [v["hit"] for v in sorted(state["registry"].values(),
                                                key=lambda v: v["n"])
                       if v["n"] in cited]
        try:
            record_qa(f"[{opts['rtype'].split(' (')[0].upper()}] "
                      f"{opts['synopsis'][:70]}", bundle, hits_for_qa,
                      do_autosave)
        except Exception as e:
            rw_log(state, f"record_qa failed: {e}")
        state["hits_for_qa"] = hits_for_qa
        state["status"] = "complete"
        rv_save_state(state)


# ---------------------------------------------------------------- the UI
def render_review_writer():
    st.markdown(
        "**Write a review, perspective, mini-review or roadmap from a "
        "synopsis** - grounded in your own indexed corpus. The pipeline "
        "sweeps the library with a dozen searches, builds a literature map "
        "(themes, controversies, gaps), proposes an outline for your "
        "approval, then writes each section as critical synthesis citing "
        "only papers it actually retrieved, numbered consistently, with the "
        "reference list built from those papers. Numbers not traceable to "
        "a retrieved passage and citations outside the retrieved set are "
        "flagged.")
    if not index_ok:
        st.error("This mode needs the corpus index.")
        return
    synopsis = st.text_area(
        "Synopsis of the piece", height=160, key="rv_syn",
        placeholder="e.g. A perspective arguing that operational stability, "
                    "not efficiency, now gates perovskite/Si tandem "
                    "commercialisation: compare ISOS-tested lifetimes across "
                    "architectures, separate reversible from irreversible "
                    "losses, weigh encapsulation vs intrinsic fixes, and set "
                    "out the field-test evidence still missing.")
    c1, c2, c3 = st.columns(3)
    with c1:
        rtype = st.selectbox("Type", list(REVIEW_TYPES), key="rv_type")
        venue = st.selectbox("Venue", list(REVIEW_VENUES), key="rv_venue")
    with c2:
        words = st.select_slider("Target main-text length (words)",
                                 [2000, 3000, 4000, 5000, 6000, 8000,
                                  10000, 12000],
                                 value=REVIEW_TYPES[rtype]["words"],
                                 key="rv_words")
        k_sec = st.slider("Passages per section", 10, 40, 24, key="rv_k")
    with c3:
        instructions = st.text_area("Your thesis / instructions (optional)",
                                    height=90, key="rv_instr",
                                    placeholder="e.g. argue for outdoor "
                                                "testing standards; include "
                                                "our 2024 ACS Energy Lett. "
                                                "work as a case")
        pause = st.checkbox("Pause after the outline for my approval",
                            value=True, key="rv_pause")
        use_rerank = st.checkbox("Re-rank passages (better evidence, more "
                                 "calls)", value=False, key="rv_rerank")
        draw_figs = st.checkbox("🎨 Draw the figures the article calls for "
                                "(frameworks, taxonomies, roadmaps; data "
                                "charts only from retrieved passages)",
                                value=True, key="rv_figs")
        pause_figs = st.checkbox("Pause after the figure plan for my "
                                 "approval", value=True, key="rv_pause_figs")
    where = restrict_search_widget("rvw")
    with st.expander("Venue profile used"):
        st.caption(REVIEW_VENUES[venue]["confidence"])
        st.text(REVIEW_VENUES[venue]["text"])
    n_sec = REVIEW_TYPES[rtype]["sections"][0]
    st.caption(f"Estimated: ~12-16 corpus searches (no AI cost) + 1 map + 1 "
               f"outline + ~{n_sec}-{REVIEW_TYPES[rtype]['sections'][1]} "
               f"sections + 1 front matter with **{model_label}**. Progress "
               "is saved after every call; resumable.")

    state = st.session_state.get("rv")

    def _start():
        opts = {"synopsis": synopsis.strip(), "rtype": rtype, "venue": venue,
                "words": int(words), "k_map": 10, "k_sec": int(k_sec),
                "instructions": instructions.strip(), "pause": pause,
                "rerank": use_rerank, "plan_edits": "",
                "figures": draw_figs, "vision": True, "max_figs": 4,
                "pause_figs": pause_figs}
        sig = _rwhash.sha1((synopsis + rtype + venue + str(words)
                            ).encode("utf-8", "replace")).hexdigest()[:12]
        return {"sig": sig, "status": "running", "stages": {}, "opts": opts,
                "registry": {}, "log": [], "plan_approved": False,
                "where": where}

    def _drive(s_):
        with st.status(f"Writing the {s_['opts']['rtype'].split(' (')[0]}"
                       "...", expanded=True) as box:
            try:
                rv_run(s_, box)
                if s_["status"] == "awaiting_plan":
                    box.update(label="Outline ready - review it below",
                               state="complete")
                elif s_["status"] == "awaiting_figures":
                    box.update(label="Figure plan ready - review it below",
                               state="complete")
                elif s_["status"] == "complete":
                    box.update(label="Draft complete", state="complete")
            except Exception as e:
                rw_log(s_, f"ERROR: {e}")
                s_["status"] = "error"
                rv_save_state(s_)
                box.update(label=f"Stopped: {e}", state="error")
                st.error(f"Stopped at a model call: {e}. Progress is saved "
                         "- press Resume.")
        st.session_state["rv"] = s_

    b1, b2 = st.columns([2, 1])
    with b1:
        if state and state.get("status") in ("error", "running") and \
                state["stages"]:
            if st.button("▶️ Resume", type="primary", key="rv_resume"):
                state["status"] = "running"
                _drive(state)
                st.rerun()
        elif st.button("📚 Write it", type="primary", key="rv_go",
                       disabled=not (synopsis.strip() and api_key.strip())):
            _drive(_start())
            st.rerun()
    with b2:
        if state and st.button("🗑️ Start over", key="rv_clear"):
            st.session_state.pop("rv", None)
            st.rerun()

    state = st.session_state.get("rv")
    if not state:
        return

    if state["status"] == "awaiting_plan":
        st.markdown("---")
        st.markdown("## Outline - awaiting your approval")
        if state.get("outline_fallback"):
            st.warning("The outline's JSON could not be parsed; a default "
                       "section plan is used (the outline text still "
                       "guides the writing).")
        man = state["stages"]["manifest"]
        st.markdown(f"**Thesis:** {man.get('thesis') or '(see outline)'}  \n"
                    f"**Recommended title:** "
                    f"{man.get('title_recommended') or '(see outline)'}  \n"
                    f"**Corpus:** {len(state['registry'])} papers retrieved "
                    f"by {len(state['stages']['queries'])} searches")
        st.dataframe([{"id": s["id"], "heading": s["heading"],
                       "target words": s["target_words"],
                       "cites": ", ".join(str(c) for c in s["cite"][:12]),
                       "display items": "; ".join(s["display_items"])[:80]}
                      for s in man["sections"]],
                     use_container_width=True, hide_index=True)
        with st.expander("Literature map (themes, controversies, gaps)",
                         expanded=False):
            st.markdown(state["stages"]["map"])
        with st.expander("Full outline text"):
            st.markdown(_rw_strip_json(state["stages"]["outline"]))
        edits = st.text_area("Edits to the outline (binding)",
                             key="rv_plan_edits")
        a1, a2 = st.columns(2)
        with a1:
            if st.button("✅ Approve and write", type="primary",
                         key="rv_approve"):
                state["plan_approved"] = True
                state["status"] = "running"
                _drive(state)
                st.rerun()
        with a2:
            if st.button("🔁 Regenerate outline with these edits",
                         key="rv_regen"):
                state["opts"]["plan_edits"] += (
                    "\nBINDING EDITS FROM THE AUTHOR: " + edits.strip())
                for k in ("outline", "manifest"):
                    state["stages"].pop(k, None)
                state.pop("outline_fallback", None)
                state["status"] = "running"
                _drive(state)
                st.rerun()
        return

    if state["status"] == "awaiting_figures":
        render_fig_checkpoint(state, "rv", "rv", _drive)
        return

    if state["status"] != "complete":
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
        return

    stg = state["stages"]
    audit = stg["audit"]
    st.markdown("---")
    if audit["counts"]["CRITICAL"]:
        st.error(f"{audit['counts']['CRITICAL']} citation(s) outside the "
                 "retrieved set - see the audit.")
    elif audit["counts"]["MAJOR"]:
        st.warning(f"{audit['counts']['MAJOR']} number(s) not traceable to a "
                   "retrieved passage - verify against the cited papers.")
    else:
        st.success("Every citation maps to a retrieved paper and every "
                   "number traces to a passage.")
    cited = rv_cited_numbers(RW_AUTHOR_RE.sub("", stg["article_md"]))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Main-text words", len(re.sub(
        r"^#.*$", "", "\n".join(stg["sections_out"][s["id"]]["text"]
                              for s in stg["manifest"]["sections"]),
        flags=re.M).split()))
    m2.metric("Papers cited", f"{len(cited)} / {len(state['registry'])}")
    m3.metric("[AUTHOR] items", len(audit["markers"]))
    m4.metric("Sections", len(stg["manifest"]["sections"]))
    t1, t2, t3, t4, t5 = st.tabs(["📄 Article", "📚 References",
                                  "🗺️ Literature map", "🔬 Audit",
                                  "🧭 Outline & log"])
    with t1:
        st_md_figs(stg["article_md"].split("\n\n## References\n\n")[0])
        if stg.get("figures"):
            with st.expander("Figures generated - review, download, redraw"):
                render_fig_regen_ui(state, "rv", "rvf")
    with t2:
        st.markdown(stg["article_md"].split("\n\n## References\n\n")[-1])
        st.caption("Each entry is a real paper from your corpus (title and "
                   "file); complete the formal reference before submission.")
        if state.get("hits_for_qa"):
            show_sources(state["hits_for_qa"], key_prefix="rv")
            render_reference_exporter(state["hits_for_qa"], key="rvrefs")
    with t3:
        st.markdown(stg["map"])
    with t4:
        if audit["rows"]:
            st.dataframe(audit["rows"], use_container_width=True,
                         hide_index=True)
        else:
            st.success("No flags.")
        for i, mk in enumerate(dict.fromkeys(audit["markers"]), 1):
            st.markdown(f"{i}. {mk}")
    with t5:
        st.markdown(stg["outline"])
        with st.expander("Search queries"):
            st.markdown("\n".join(f"- {q}" for q in stg["queries"]))
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
    d1, d2 = st.columns(2)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", state["opts"]["synopsis"][:40])
    with d1:
        doc = Document()
        md_to_docx(doc, stg["article_md"])
        buf = io.BytesIO()
        doc.save(buf)
        st.download_button("⬇️ Article + references (Word)", buf.getvalue(),
                           file_name=f"review_{stem}.docx",
                           mime="application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document", key="rv_dl1")
    with d2:
        doc2 = Document()
        md_to_docx(doc2, stg["bundle"])
        buf2 = io.BytesIO()
        doc2.save(buf2)
        st.download_button("⬇️ Full bundle incl. map, audit, outline (Word)",
                           buf2.getvalue(),
                           file_name=f"review_bundle_{stem}.docx",
                           mime="application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document", key="rv_dl2")
    st.caption("Saved to the answers folder and the Ask-tab history; job "
               f"folder: answers/review_jobs/{state['sig']}/")
    render_followup(
        "rv_" + state["sig"], "Discuss and revise this article",
        {"article": stg["article_md"].split("\n\n## References\n\n")[0]},
        sources_text="\n\n".join(stg["passages"].values())[:40000],
        pool_texts=list(stg["passages"].values()),
        apply_fn=lambda edits, pool: fu_apply_rv(state, edits, pool))


# ==========================================================================
# Career tab: job applications (group leader, professorship, fellowship)
#
# One application = a position (job ad -> extracted requirements) plus a
# set of generated documents, all written ONLY from the author's verified
# facts (CV text + profile + extra facts) and, for the research proposal,
# from a topic library in the corpus (numbered citations, reference list,
# audit) - reusing the review-writer machinery (rv_*) and the rewrite
# pipeline's call/parse/persist helpers (rw_*).
# ==========================================================================
CAREER_DIR = ANSWERS_DIR / "career"
CAREER_APPS = CAREER_DIR / "applications.json"
CAREER_CV = CAREER_DIR / "cv.txt"
CAREER_FACTS = CAREER_DIR / "extra_facts.md"
FUNDING_PROFILE = ANSWERS_DIR / "funding" / "profile.json"

PROPOSAL_TYPES = {
    "Research vision / statement (group leader, professorship)": {
        "words": 3000,
        "structure": "Vision (the scientific question and why now) -> "
                     "positioning against the state of the art -> 5-year "
                     "programme in 3-4 research lines with concrete first "
                     "projects and milestones -> methods, infrastructure "
                     "and data/AI strategy -> team and supervision plan -> "
                     "collaborations inside the host and outside -> funding "
                     "strategy (which calls, when) -> risks and "
                     "mitigations -> expected outputs and impact."},
    "Research proposal (fellowship / ERC-style narrative)": {
        "words": 4000,
        "structure": "State of the art and objectives -> beyond the state "
                     "of the art (the specific advance) -> methodology with "
                     "work packages, tasks, deliverables and a timeline -> "
                     "feasibility (track record, preliminary evidence, "
                     "resources) -> risk and contingency -> impact and "
                     "dissemination -> ethics/data."},
    "Leadership & management statement": {
        "words": 1200,
        "structure": "Leadership philosophy -> evidence from projects led "
                     "(scale, consortia, budgets, people) -> how the group "
                     "will be run (culture, mentoring, diversity, open "
                     "science) -> decision-making and conflict -> 100-day "
                     "plan for the position."},
    "Teaching & supervision statement": {
        "words": 1000,
        "structure": "Teaching philosophy -> evidence (courses, supervision "
                     "of PhDs/MScs/postdocs, outcomes) -> planned courses "
                     "for the host -> supervision practice -> inclusion."},
}

CP_REQ_SYSTEM = """\
You extract the requirements of an academic or research-leadership job advertisement. Respond with ONLY a JSON object:
{"position": "...", "institution": "...", "deadline": "YYYY-MM-DD or ''",
 "documents_required": ["CV", "cover letter", "research statement", ...],
 "must_have": ["..."], "nice_to_have": ["..."],
 "evaluation_criteria": ["..."], "keywords": ["..."],
 "research_focus": "one paragraph: what they want the person to work on",
 "position_type": "group leader | professor | fellowship | postdoc | industry | other",
 "notes": "anything unusual: tenure track, start-up package, teaching load, language, salary scale"}
Copy wording from the advertisement where possible; never invent requirements. Empty list if not stated."""

CP_FIT_SYSTEM = """\
You assess a candidate's fit for a position, for the candidate's own eyes. You receive the REQUIREMENTS extracted from the advertisement and the CANDIDATE FACTS (CV text, profile, extra facts). Write in markdown:
## Verdict
Fit score X/10 and two sentences (honest; if the fit is weak, say so and why).
## Requirement-by-requirement
A table: Requirement | Evidence in the facts (quote or cite the item; 'none found' if absent) | Strength (strong / partial / gap) | How to address in the application
Cover every must-have and evaluation criterion, then the nice-to-haves.
## Your strongest three arguments
## Gaps and how to frame them honestly
## Questions to clarify before applying
Rules: use ONLY the candidate facts; never assume achievements not stated; where a fact would help but is missing, write [AUTHOR: confirm ...]."""

CP_QUERIES_SYSTEM = """\
You design a literature-search plan for a research proposal. From the POSITION'S RESEARCH FOCUS, the PROPOSAL BRIEF and the candidate's expertise, produce 8-12 concise retrieval phrases (8-16 words each, not questions) covering: the state of the art of the target topic; methods and infrastructure; open problems; adjacent fields to connect; benchmarks and metrics. Respond with ONLY a JSON array of strings."""

CP_OUTLINE_SYSTEM = """\
You are an experienced reviewer for {POSITION_TYPE} applications, planning a {DOC_TYPE} for a candidate. Structure to follow: {STRUCTURE}
You receive: the REQUIREMENTS of the position, the PROPOSAL BRIEF from the candidate, the CANDIDATE FACTS, and NUMBERED PAPERS retrieved from the candidate's topic library (cite as [n]). Produce the outline. Principles: the document answers the position's research focus and evaluation criteria explicitly; the candidate's track record is the feasibility argument (only facts given); the science is grounded in the numbered papers; concrete first projects with milestones beat generalities; honest about what the candidate would need to learn or hire.
OUTPUT (markdown; keep headings exactly; the fenced json MUST be valid JSON)
# OUTLINE
## 1. Core message  (one sentence the committee should remember)
## 2. How the document maps to the evaluation criteria  (criterion -> where it is answered)
## 3. Section plan
```json
{"core_message": "...", "title": "...",
 "sections": [{"id": "P1", "heading": "...", "target_words": 400, "brief": "what this section argues; which requirement it answers; which facts it uses", "query": "retrieval phrase for evidence, 8-16 words", "cite": [1, 4]}]}
```
Rules: 5-9 sections; target_words sum to about {WORDS}; every section has a query; 'cite' lists [n] from the numbered papers.
## 4. Decisions only the candidate can make
Finish with <<<END OUTLINE>>>."""

CP_SECTION_SYSTEM = """\
You write ONE section of a {DOC_TYPE} for a {POSITION_TYPE} application. You receive the core message, this section's brief, the REQUIREMENTS it answers, the CANDIDATE FACTS, numbered EVIDENCE PASSAGES from the candidate's topic library, and the tail of the previous section.
INTEGRITY RULES (audited mechanically)
1. Facts about the candidate (grants, papers, roles, numbers, people, budgets) come ONLY from the CANDIDATE FACTS; never invent or inflate; where a fact would strengthen the section but is missing, write [AUTHOR: confirm ...].
2. Scientific claims and numbers come ONLY from the evidence passages, cited as [n] exactly as given (global numbering; never renumber, never invent a citation). A needed value the passages lack becomes [AUTHOR: add ...].
3. No 'novel', 'unprecedented', 'world-leading', 'unique' unless quoting a source with its [n].
CRAFT: first person; confident, specific, committee-readable (a non-specialist member must follow); each paragraph: claim, evidence, why it matters for THIS position; concrete projects have a first experiment, a milestone and a success criterion; respect target_words +/- 15%; do not repeat the previous section's tail; claim-shaped subheadings (###) allowed.
OUTPUT (exact delimiters)
<<<SECTION>>>
## <heading>
<prose>
<<<END SECTION>>>"""

CP_COVER_SYSTEM = """\
You write a cover letter for an academic / research-leadership application, for the candidate's signature. You receive the REQUIREMENTS, the CANDIDATE FACTS, the core message of their research statement (if written) and optional instructions. One page (350-500 words): opening that names the position and the one reason they are the right person; two or three paragraphs mapping their strongest evidence to the evaluation criteria (specific: projects, scale, outcomes - only from the facts); a paragraph on what they would bring to THIS host (its named strengths from the advertisement); the research direction in three sentences; a confident close. Rules: only facts given; [AUTHOR: ...] for anything to confirm (dates, names, referees); no flattery, no clichés ('I am writing to apply'), no superlatives about oneself; British or US spelling consistent with the CV. Output: the letter in markdown with a subject line, then '## Notes for the author' with the [AUTHOR] items and a one-line note on tone."""

CP_CV_SYSTEM = """\
You tailor a CV for a specific position WITHOUT adding, inflating or removing any fact. You receive the CANDIDATE'S CV TEXT, the REQUIREMENTS and the evaluation criteria. Produce in markdown:
## Tailored CV (markdown)
The same facts reorganised: a 4-6 line profile summary at the top targeted to the position (only claims supported by the CV), sections ordered so the most relevant evidence (leadership, funding, research lines, supervision, publications) comes first, items within sections ordered by relevance to the criteria, and the position's keywords used where the CV already supports them. Keep every publication, grant and role; do not shorten lists of publications - mark where a long list should be moved to an annex.
## What was moved and why  (bullets)
## Evidence gaps the CV cannot cover  ([AUTHOR: ...] items - things the committee will look for that are not in the CV)
Rules: identical facts, identical numbers, identical dates; nothing invented; if the CV text is garbled from extraction, keep the content and note it."""

CP_INTERVIEW_SYSTEM = """\
You prepare a candidate for an academic / research-leadership interview. You receive the REQUIREMENTS, the CANDIDATE FACTS, and the core message of their research statement (if any). Produce in markdown:
## The committee's likely concerns  (3-5, with why)
## Likely questions and strong answers  (12-16 questions grouped: vision, feasibility, leadership & people, funding, fit with the host, teaching/supervision, weaknesses; each with a 3-5 sentence model answer that uses ONLY the candidate facts and marks anything to confirm as [AUTHOR: ...])
## Questions the candidate should ask them  (6-8, specific to this position)
## Job talk skeleton  (10-12 slides, one line each, mapped to the evaluation criteria)
## Red flags to avoid saying"""


# ---------------------------------------------------------------- store
def cp_load_apps():
    try:
        return _rwjson.loads(CAREER_APPS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cp_save_apps(apps):
    CAREER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAREER_APPS.with_suffix(".json.tmp")
    tmp.write_text(_rwjson.dumps(apps, ensure_ascii=False, default=str),
                   encoding="utf-8")
    tmp.replace(CAREER_APPS)


def cp_facts_text():
    """Everything Claude may state about the candidate."""
    parts = []
    if CAREER_CV.exists():
        parts.append("=== CV (verbatim extraction) ===\n"
                     + CAREER_CV.read_text(encoding="utf-8")[:60000])
    try:
        prof = _rwjson.loads(FUNDING_PROFILE.read_text(
            encoding="utf-8")).get("text", "")
        if prof.strip():
            parts.append("=== PROFILE (shared with Funding Radar) ===\n" + prof)
    except Exception:
        pass
    if CAREER_FACTS.exists():
        extra = CAREER_FACTS.read_text(encoding="utf-8").strip()
        if extra:
            parts.append("=== EXTRA FACTS (author-supplied) ===\n" + extra)
    return "\n\n".join(parts)


CP_UNIT_AFTER = re.compile(
    r"^\s*(?:M\b|k\b|%|€|EUR\b|USD\b|\$|million|billion|thousand|PhDs?\b|"
    r"postdocs?\b|papers?\b|publications?\b|patents?\b|projects?\b|"
    r"grants?\b|years?\b|months?\b|people\b|students?\b|h\b|hours?\b|"
    r"devices?\b|partners?\b|countries\b|awards?\b|FTE\b|citations?\b|"
    r"invited\b|talks?\b)", re.I)
CP_CURRENCY_BEFORE = re.compile(r"(?:EUR|USD|€|\$|£)\s*$", re.I)


def cp_sig_numbers(text):
    """Numbers that matter in a candidate document: everything with 3+
    digits (as in rw_numbers_in) PLUS 1-2 digit numbers that carry a
    unit or currency ('EUR 10M', '41 publications', '12 PhDs')."""
    out = set()
    for m in RW_NUM_RE.finditer(text or ""):
        n = _rw_norm_num(m.group(0))
        core = n.lstrip("-")
        if re.fullmatch(r"\d{1,2}", core):
            after = text[m.end():m.end() + 14]
            before = text[max(0, m.start() - 6):m.start()]
            if not (CP_UNIT_AFTER.match(after)
                    or CP_CURRENCY_BEFORE.search(before)):
                continue
        out.add(n)
    return out


def cp_fact_audit(doc_md, facts, extra_pool_text=""):
    """Numbers in a candidate document that appear neither in the facts
    nor (for research documents) in the retrieved literature passages."""
    pool = cp_sig_numbers(facts) | cp_sig_numbers(extra_pool_text)
    rows = []
    clean = RW_AUTHOR_RE.sub("", FIG_IMG_RE.sub("", doc_md))
    seen = set()
    for m in RW_NUM_RE.finditer(clean):
        n = _rw_norm_num(m.group(0))
        core = n.lstrip("-")
        if n in pool or n in seen or re.fullmatch(r"(19|20)\d\d", core):
            continue
        if re.fullmatch(r"\d{1,2}", core):
            after = clean[m.end():m.end() + 14]
            before = clean[max(0, m.start() - 6):m.start()]
            if not (CP_UNIT_AFTER.match(after)
                    or CP_CURRENCY_BEFORE.search(before)):
                continue
        pre = clean[max(0, m.start() - 14):m.start()].lower()
        if re.search(r"(fig|figure|table|section|ref)\.?\s*s?$", pre) or \
                re.search(r"\[[\d,\s\-–]*$", pre):
            continue
        seen.add(n)
        a, b = max(0, m.start() - 60), min(len(clean), m.end() + 60)
        rows.append({"item": n, "context":
                     re.sub(r"\s+", " ", clean[a:b]).strip()})
    return rows


def cp_app_dir(slug):
    d = CAREER_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def cp_save_doc(slug, name, md):
    d = cp_app_dir(slug)
    (d / f"{name}.md").write_text(md, encoding="utf-8")
    try:
        doc = Document()
        md_to_docx(doc, md)
        doc.save(d / f"{name}.docx")
    except Exception:
        pass
    return d / f"{name}.docx"


def cp_req_text(req):
    if not req:
        return "(no requirements extracted yet)"
    return (f"POSITION: {req.get('position')} at {req.get('institution')} "
            f"({req.get('position_type')}); deadline {req.get('deadline') or '?'}\n"
            f"RESEARCH FOCUS: {req.get('research_focus')}\n"
            f"MUST HAVE: " + "; ".join(req.get("must_have", [])) + "\n"
            f"NICE TO HAVE: " + "; ".join(req.get("nice_to_have", [])) + "\n"
            f"EVALUATION CRITERIA: " + "; ".join(req.get("evaluation_criteria", []))
            + "\nKEYWORDS: " + ", ".join(req.get("keywords", []))
            + "\nDOCUMENTS REQUIRED: " + ", ".join(req.get("documents_required", []))
            + "\nNOTES: " + str(req.get("notes", "")))


# ------------------------------------------------- research-document runner
def cp_run(state, status_box):
    """Staged, resumable proposal/statement writer (like rv_run)."""
    _RW_UI["box"] = status_box
    opts, stg = state["opts"], state["stages"]
    ptype = PROPOSAL_TYPES[opts["doc_type"]]
    where = state.get("where")
    facts = opts["facts"]
    req = opts["req_text"]

    def step(msg):
        status_box.write(msg)
        rw_log(state, msg)

    if "queries" not in stg:
        step("Planning the literature search...")
        raw = rw_call(CP_QUERIES_SYSTEM,
                      f"POSITION'S RESEARCH FOCUS AND REQUIREMENTS:\n{req}\n\n"
                      f"PROPOSAL BRIEF:\n{opts['brief']}\n\nCANDIDATE "
                      f"EXPERTISE (profile only):\n{facts[:4000]}", 1200)
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            qs = [str(q) for q in _rwjson.loads(raw) if str(q).strip()]
        except Exception:
            qs = [opts["brief"][:200]]
        stg["queries"] = qs[:12]
        rv_save_state(state)
    if "sweep" not in stg:
        for i, q in enumerate(stg["queries"], 1):
            step(f"Sweeping the topic library ({i}/{len(stg['queries'])}): "
                 f"{q[:60]}")
            try:
                hits = retrieve(q, opts["k_map"], where_extra=where)
            except Exception as e:
                rw_log(state, f"retrieval failed: {e}")
                hits = []
            rv_register(state, hits)
        stg["sweep"] = {"papers": len(state["registry"])}
        rv_save_state(state)

    if "outline" not in stg:
        step(f"Outline (grounded in {len(state['registry'])} papers)...")
        entries = sorted(state["registry"].values(), key=lambda v: v["n"])
        listing = "\n".join(
            f"[{v['n']}] {v['title']} ({v['file']})"
            + (f" - {v['passages'][0][:300]}" if v["passages"] else "")
            for v in entries)[:60000]
        sysm = (CP_OUTLINE_SYSTEM.replace("{POSITION_TYPE}", opts["position_type"])
                .replace("{DOC_TYPE}", opts["doc_type"])
                .replace("{STRUCTURE}", ptype["structure"])
                .replace("{WORDS}", str(opts["words"])))
        umsg = (f"REQUIREMENTS:\n{req}\n\nPROPOSAL BRIEF:\n{opts['brief']}"
                f"{opts.get('plan_edits', '')}\n\nCANDIDATE FACTS:\n"
                f"{facts[:30000]}\n\nNUMBERED PAPERS FROM THE TOPIC LIBRARY:\n"
                f"{listing or '(none retrieved - the library may be empty; write from the brief and mark evidence gaps)'}"
                "\n\nWrite the OUTLINE now.")
        out = rw_call(sysm, umsg, 5000)
        stg["outline"] = out.replace("<<<END OUTLINE>>>", "").strip()
        o = _rw_json_block(stg["outline"])
        secs = rv_validate_outline(o) if o else None
        if not secs:
            secs = [{"id": f"P{i + 1}", "heading": h, "target_words":
                     max(250, opts["words"] // 6), "brief": "(default)",
                     "query": h, "cite": [], "display_items": []}
                    for i, h in enumerate(
                        ["Vision and scientific question",
                         "State of the art and positioning",
                         "Research programme and first projects",
                         "Methods, infrastructure and data",
                         "Team, collaborations and funding",
                         "Risks, outputs and impact"])]
            state["outline_fallback"] = True
        stg["manifest"] = {"sections": secs,
                           "core_message": (o or {}).get("core_message", ""),
                           "title": (o or {}).get("title", "")}
        rv_save_state(state)
        if opts.get("pause") and not state.get("plan_approved"):
            state["status"] = "awaiting_plan"
            rv_save_state(state)
            return

    secs = stg["manifest"]["sections"]
    stg.setdefault("sections_out", {})
    stg.setdefault("passages", {})
    prev_tail = ""
    for i, s in enumerate(secs):
        if s["id"] in stg["sections_out"]:
            prev_tail = stg["sections_out"][s["id"]]["text"][-1000:]
            continue
        step(f"Writing section {i + 1}/{len(secs)} - {s['heading']}")
        try:
            hits = retrieve(s["query"], opts["k_sec"], where_extra=where)
        except Exception:
            hits = []
        nums = rv_register(state, hits)
        eh, en = rv_hits_for_numbers(state, s["cite"])
        ctx = rv_context(state, hits + eh, nums + en, max_chars=40000)
        stg["passages"][s["id"]] = ctx
        sysm = (CP_SECTION_SYSTEM.replace("{DOC_TYPE}", opts["doc_type"])
                .replace("{POSITION_TYPE}", opts["position_type"]))
        umsg = (f"CORE MESSAGE: {stg['manifest']['core_message']}\n"
                f"SECTION: {s['id']} | heading: {s['heading']} | "
                f"target_words: {s['target_words']}\nBRIEF: {s['brief']}\n\n"
                f"REQUIREMENTS:\n{req}\n\nCANDIDATE FACTS:\n{facts[:30000]}\n\n"
                f"=== EVIDENCE PASSAGES (cite ONLY these numbers) ===\n"
                f"{ctx or '(none - mark scientific claims [AUTHOR: add evidence])'}\n\n"
                f"=== END OF THE PREVIOUS SECTION (do not repeat) ===\n"
                f"{prev_tail or '(first section)'}\n\nWrite this section now.")
        text, complete, raw = rw_call_delimited(
            sysm, umsg, 6000, "<<<SECTION>>>", "<<<END SECTION>>>")
        if not text.lstrip().startswith("#"):
            text = f"## {s['heading']}\n\n{text}"
        if not complete:
            text += "\n\n[AUTHOR: section cut off - review its end]"
        stg["sections_out"][s["id"]] = {"text": text, "complete": complete}
        prev_tail = text[-1000:]
        rv_save_state(state)

    if opts.get("figures", True) and "fig_done" not in stg:
        sec_texts = [(s["id"], s["heading"], stg["sections_out"][s["id"]]["text"])
                     for s in secs]
        _figs, sec_texts = fig_run_stage(
            state, "cp", sec_texts, _rw_strip_json(stg["outline"]), api_key,
            model, step, max_figs=int(opts.get("max_figs", 3)),
            vision=opts.get("vision", True))
        if _figs is None:            # figure plan awaiting approval
            return
        for sid, _h, md in sec_texts:
            stg["sections_out"][sid]["text"] = md
        stg["fig_done"] = True
        rv_save_state(state)

    if "bundle" not in stg:
        step("References, audits, assembly...")
        body = "\n\n".join(stg["sections_out"][s["id"]]["text"] for s in secs)
        title = stg["manifest"].get("title") or opts["doc_type"]
        article = f"# {title}\n\n{body}"
        cited = rv_cited_numbers(RW_AUTHOR_RE.sub("", article))
        refs = rv_reference_list(state, cited)
        lit_audit = rv_audit(state, article, stg["passages"])
        fact_rows = cp_fact_audit(article, facts,
                                  "\n".join(stg["passages"].values()))
        full = article + "\n\n## References\n\n" + refs
        words = len(re.sub(r"^#.*$", "", body, flags=re.M).split())
        bundle = (
            f"{full}\n\n---\n\n# Audit\n\n*{words} words · {len(cited)} papers "
            f"cited of {len(state['registry'])} retrieved · [AUTHOR] items: "
            f"{len(lit_audit['markers'])}*\n\n## Literature audit\n\n"
            f"{rw_audit_table(lit_audit)}\n\n## Candidate-fact audit "
            f"(numbers not found in your CV/profile/facts)\n\n"
            + ("\n".join(f"- {r['item']}: …{r['context']}…" for r in fact_rows)
               or "(none)")
            + f"\n\n---\n\n# Outline\n\n{stg['outline']}\n\n# Search queries\n\n"
            + "\n".join(f"- {q}" for q in stg["queries"]))
        stg["bundle"] = bundle
        stg["article_md"] = full
        stg["audit"] = {"lit": lit_audit, "facts": fact_rows, "words": words,
                        "cited": len(cited)}
        path = cp_save_doc(opts["slug"], opts["doc_key"], full)
        cp_save_doc(opts["slug"], opts["doc_key"] + "_bundle", bundle)
        state["saved_path"] = str(path)
        hits_for_qa = [v["hit"] for v in sorted(state["registry"].values(),
                                                key=lambda v: v["n"])
                       if v["n"] in cited]
        try:
            record_qa(f"[CAREER - {opts['doc_type'].split(' (')[0]}] "
                      f"{opts['slug']}", bundle, hits_for_qa, do_autosave)
        except Exception as e:
            rw_log(state, f"record_qa failed: {e}")
        state["status"] = "complete"
        rv_save_state(state)


# ---------------------------------------------------------------- the UI
def _cp_simple_doc(slug, key, label, system, user_msg, max_tokens, facts,
                   btn_key):
    """One-call document (fit, cover letter, CV, interview) with save,
    fact audit and download."""
    app_dir = cp_app_dir(slug)
    existing = app_dir / f"{key}.md"
    if st.button(label, type="primary", key=btn_key,
                 disabled=not api_key.strip()):
        try:
            with st.spinner("Writing..."):
                out = rw_call(system, user_msg, max_tokens)
            cp_save_doc(slug, key, out)
            st.rerun()
        except Exception as e:
            st.error(f"Claude error: {e}")
    if existing.exists():
        md = existing.read_text(encoding="utf-8")
        st.caption(f"Saved {datetime.datetime.fromtimestamp(existing.stat().st_mtime):%Y-%m-%d %H:%M} · answers/career/{slug}/{key}.docx")
        st.markdown(RW_AUTHOR_RE.sub(lambda m: f"**{m.group(0)}**", md))
        rows = cp_fact_audit(md, facts)
        if rows:
            with st.expander(f"⚠️ {len(rows)} number(s) not found in your "
                             "facts - verify before sending"):
                for r in rows:
                    st.markdown(f"- **{r['item']}** … {r['context']} …")
        docx_p = app_dir / f"{key}.docx"
        if docx_p.exists():
            st.download_button("⬇️ Word", docx_p.read_bytes(),
                               file_name=f"{slug}_{key}.docx",
                               mime="application/vnd.openxmlformats-"
                                    "officedocument.wordprocessingml.document",
                               key=f"{btn_key}_dl")


def render_career_tab():
    st.markdown(
        "**Job applications** - group-leader, professorship and fellowship "
        "positions. Paste the advertisement, keep your verified facts (CV, "
        "profile, extra facts) in one place, and generate: fit assessment, "
        "a research vision/proposal grounded in a topic library from your "
        "corpus (numbered citations, reference list), cover letter, "
        "tailored CV, and interview preparation. Every document is written "
        "ONLY from your facts; anything else becomes `[AUTHOR: ...]`, and "
        "numbers not found in your facts are flagged.")

    # ---- facts
    with st.expander("👤 My facts (CV, profile, extra facts) - the only "
                     "source about you", expanded=not CAREER_CV.exists()):
        f1, f2 = st.columns(2)
        with f1:
            cv_up = st.file_uploader("CV (.docx / .pdf / .txt)",
                                     type=["docx", "pdf", "txt", "md"],
                                     key="cp_cv_up")
            if cv_up is not None and st.button("💾 Save CV text",
                                                key="cp_cv_save"):
                try:
                    CAREER_DIR.mkdir(parents=True, exist_ok=True)
                    CAREER_CV.write_text(extract_uploaded_text(cv_up),
                                         encoding="utf-8")
                    st.success("CV text saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not read the CV: {e}")
            if CAREER_CV.exists():
                st.caption(f"CV on file: {len(CAREER_CV.read_text(encoding='utf-8').split()):,} words")
        with f2:
            extra = st.text_area(
                "Extra facts not in the CV (grants, papers in press, "
                "awards, numbers) - one per line", height=140, key="cp_extra",
                value=CAREER_FACTS.read_text(encoding="utf-8")
                if CAREER_FACTS.exists() else "")
            if st.button("💾 Save extra facts", key="cp_extra_save"):
                CAREER_DIR.mkdir(parents=True, exist_ok=True)
                CAREER_FACTS.write_text(extra, encoding="utf-8")
                st.success("Saved.")
        st.caption("The profile from Funding Radar (answers/funding/"
                   "profile.json) is included automatically.")
    facts = cp_facts_text()
    if not facts.strip():
        st.warning("Save your CV first - every document is written from it.")

    # ---- applications
    apps = cp_load_apps()
    a1, a2 = st.columns([2, 1])
    with a1:
        names = list(apps)
        pick = st.selectbox("Application", ["(new application)"] + names,
                            key="cp_pick")
    with a2:
        if pick != "(new application)" and st.button("🗑️ Delete application",
                                                     key="cp_del"):
            apps.pop(pick, None)
            cp_save_apps(apps)
            st.rerun()
    if pick == "(new application)":
        with st.form("cp_new"):
            n1, n2 = st.columns(2)
            with n1:
                new_name = st.text_input("Short name",
                                         placeholder="DIFFER group leader 2026")
            with n2:
                ad_up = st.file_uploader("Advertisement file (optional)",
                                         type=["docx", "pdf", "txt", "md"])
            ad_text = st.text_area("Advertisement text (paste)", height=200)
            if st.form_submit_button("➕ Create and extract requirements",
                                     type="primary"):
                text = ad_text.strip()
                if ad_up is not None and not text:
                    try:
                        text = extract_uploaded_text(ad_up)
                    except Exception as e:
                        st.error(f"Could not read the file: {e}")
                if not new_name.strip() or not text:
                    st.warning("Name and advertisement text are required.")
                else:
                    slug = re.sub(r"[^A-Za-z0-9]+", "_", new_name.strip())[:40]
                    req = None
                    if api_key.strip():
                        try:
                            raw = rw_call(CP_REQ_SYSTEM,
                                          f"ADVERTISEMENT:\n{text[:30000]}",
                                          2500)
                            req = _rw_json_block(raw)
                        except Exception as e:
                            st.warning(f"Requirement extraction failed: {e}")
                    apps[slug] = {"name": new_name.strip(), "ad": text,
                                  "req": req or {}, "status": "Preparing",
                                  "created": datetime.date.today().isoformat(),
                                  "checklist": {}}
                    cp_save_apps(apps)
                    st.success("Created.")
                    st.rerun()
        return

    app = apps[pick]
    slug = pick
    req = app.get("req") or {}
    req_text = cp_req_text(req)
    ptype_pos = req.get("position_type", "group leader") or "group leader"
    h1, h2, h3 = st.columns(3)
    h1.metric("Position", (req.get("position") or app["name"])[:40])
    h2.metric("Deadline", req.get("deadline") or "—")
    h3.metric("Status", app.get("status", "Preparing"))

    t_pos, t_fit, t_prop, t_cover, t_cv, t_int, t_check = st.tabs(
        ["📋 Position", "🎯 Fit", "🔬 Research document", "✉️ Cover letter",
         "📄 CV tailoring", "🎤 Interview prep", "✅ Checklist"])

    with t_pos:
        if not req:
            if st.button("Extract requirements from the advertisement",
                         key="cp_req_go", disabled=not api_key.strip()):
                try:
                    raw = rw_call(CP_REQ_SYSTEM,
                                  f"ADVERTISEMENT:\n{app['ad'][:30000]}", 2500)
                    app["req"] = _rw_json_block(raw) or {}
                    cp_save_apps(apps)
                    st.rerun()
                except Exception as e:
                    st.error(f"Claude error: {e}")
        else:
            st.markdown(f"**Research focus:** {req.get('research_focus', '')}")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Must have**\n" + "\n".join(
                    f"- {x}" for x in req.get("must_have", [])))
                st.markdown("**Evaluation criteria**\n" + "\n".join(
                    f"- {x}" for x in req.get("evaluation_criteria", [])))
            with c2:
                st.markdown("**Nice to have**\n" + "\n".join(
                    f"- {x}" for x in req.get("nice_to_have", [])))
                st.markdown("**Documents required**\n" + "\n".join(
                    f"- {x}" for x in req.get("documents_required", [])))
                st.caption("Notes: " + str(req.get("notes", "")))
        with st.expander("Advertisement text"):
            st.text(app["ad"][:20000])
        st.selectbox("Status", ["Preparing", "Submitted", "Interview",
                                "Offer", "Declined", "Rejected"],
                     index=["Preparing", "Submitted", "Interview", "Offer",
                            "Declined", "Rejected"].index(
                         app.get("status", "Preparing")),
                     key="cp_status_sel")
        if st.button("💾 Save status", key="cp_status_save"):
            app["status"] = st.session_state["cp_status_sel"]
            cp_save_apps(apps)
            st.success("Saved.")

    with t_fit:
        _cp_simple_doc(slug, "fit", "🎯 Assess my fit", CP_FIT_SYSTEM,
                       f"REQUIREMENTS:\n{req_text}\n\nCANDIDATE FACTS:\n"
                       f"{facts[:40000]}", 4000, facts, "cp_fit_go")

    with t_prop:
        st.caption("Grounded in a topic library: index the field's papers "
                   "(e.g. self-driving labs) with a topic tag in the filename "
                   "and restrict the search to it below - the document cites "
                   "them as [n] with a reference list built from your "
                   "corpus.")
        p1, p2 = st.columns(2)
        with p1:
            doc_type = st.selectbox("Document", list(PROPOSAL_TYPES),
                                    key="cp_doc_type")
            words = st.select_slider("Length (words)",
                                     [800, 1200, 2000, 3000, 4000, 5000, 6000],
                                     value=PROPOSAL_TYPES[doc_type]["words"],
                                     key="cp_words")
        with p2:
            brief = st.text_area(
                "Brief: the research direction you want to propose",
                height=120, key="cp_brief",
                placeholder="e.g. A self-driving lab for perovskite absorber "
                            "discovery that closes the loop from synthesis to "
                            "operational stability, using my outdoor "
                            "degradation analytics as the objective "
                            "function...")
            pause = st.checkbox("Pause after the outline for approval",
                                value=True, key="cp_pause")
            draw_figs = st.checkbox("🎨 Draw the figures (programme roadmap, "
                                    "architecture, workflow)", value=True,
                                    key="cp_figs")
        pause_figs = st.checkbox("Pause after the figure plan for my "
                                 "approval", value=True, key="cp_pause_figs")
        where = restrict_search_widget("career") if index_ok else None
        k_sec = st.slider("Passages per section", 8, 30, 16, key="cp_k")
        doc_key = "research_" + re.sub(r"[^a-z]+", "_",
                                       doc_type.lower())[:24]
        state = st.session_state.get("cp")
        if state and state["opts"].get("slug") != slug:
            state = None

        def _start():
            opts = {"slug": slug, "doc_key": doc_key, "doc_type": doc_type,
                    "position_type": ptype_pos, "brief": brief.strip(),
                    "words": int(words), "k_map": 8, "k_sec": int(k_sec),
                    "facts": facts, "req_text": req_text, "pause": pause,
                    "plan_edits": "", "figures": draw_figs, "vision": True,
                    "max_figs": 3, "pause_figs": pause_figs}
            sig = _rwhash.sha1((slug + doc_type + brief).encode(
                "utf-8", "replace")).hexdigest()[:12]
            return {"sig": "career_" + sig, "status": "running", "stages": {},
                    "opts": opts, "registry": {}, "log": [],
                    "plan_approved": False, "where": where}

        def _drive(s_):
            with st.status("Writing...", expanded=True) as box:
                try:
                    cp_run(s_, box)
                    box.update(label="Outline ready - review below"
                               if s_["status"] == "awaiting_plan"
                               else "Figure plan ready - review below"
                               if s_["status"] == "awaiting_figures"
                               else "Document complete", state="complete")
                except Exception as e:
                    rw_log(s_, f"ERROR: {e}")
                    s_["status"] = "error"
                    rv_save_state(s_)
                    box.update(label=f"Stopped: {e}", state="error")
                    st.error(f"Stopped: {e}. Progress saved - press Resume.")
            st.session_state["cp"] = s_

        b1, b2 = st.columns([2, 1])
        with b1:
            if state and state.get("status") in ("error", "running") and \
                    state["stages"]:
                if st.button("▶️ Resume", type="primary", key="cp_resume"):
                    state["status"] = "running"
                    _drive(state)
                    st.rerun()
            elif st.button("🔬 Write the document", type="primary",
                           key="cp_go",
                           disabled=not (brief.strip() and api_key.strip()
                                         and facts.strip())):
                _drive(_start())
                st.rerun()
        with b2:
            if state and st.button("🗑️ Start over", key="cp_clear"):
                st.session_state.pop("cp", None)
                st.rerun()
        state = st.session_state.get("cp")
        if state and state["opts"].get("slug") == slug:
            if state["status"] == "awaiting_plan":
                man = state["stages"]["manifest"]
                st.markdown("### Outline - awaiting approval")
                if state.get("outline_fallback"):
                    st.warning("Outline JSON unusable - default structure "
                               "used.")
                st.markdown(f"**Core message:** "
                            f"{man.get('core_message') or '(see outline)'}  \n"
                            f"**Library:** {len(state['registry'])} papers "
                            "retrieved")
                st.dataframe([{"id": s["id"], "heading": s["heading"],
                               "words": s["target_words"],
                               "cites": ", ".join(str(c) for c in s["cite"])}
                              for s in man["sections"]],
                             use_container_width=True, hide_index=True)
                with st.expander("Full outline"):
                    st.markdown(_rw_strip_json(state["stages"]["outline"]))
                edits = st.text_area("Edits (binding)", key="cp_edits")
                e1, e2 = st.columns(2)
                with e1:
                    if st.button("✅ Approve and write", type="primary",
                                 key="cp_approve"):
                        state["plan_approved"] = True
                        state["status"] = "running"
                        _drive(state)
                        st.rerun()
                with e2:
                    if st.button("🔁 Regenerate outline", key="cp_regen"):
                        state["opts"]["plan_edits"] += (
                            "\nBINDING EDITS FROM THE CANDIDATE: "
                            + edits.strip())
                        for k in ("outline", "manifest"):
                            state["stages"].pop(k, None)
                        state.pop("outline_fallback", None)
                        state["status"] = "running"
                        _drive(state)
                        st.rerun()
            elif state["status"] == "awaiting_figures":
                render_fig_checkpoint(state, "cp", "cp", _drive)
            elif state["status"] == "complete":
                stg = state["stages"]
                au = stg["audit"]
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Words", au["words"])
                m2.metric("Papers cited", f"{au['cited']} / "
                                          f"{len(state['registry'])}")
                m3.metric("Lit. flags", f"{au['lit']['counts']['CRITICAL']} / "
                                        f"{au['lit']['counts']['MAJOR']}")
                m4.metric("Fact flags", len(au["facts"]))
                st_md_figs(stg["article_md"])
                if stg.get("figures"):
                    with st.expander("Figures generated - review, download, redraw"):
                        render_fig_regen_ui(state, "cp", "cpf")
                if au["facts"]:
                    with st.expander(f"⚠️ {len(au['facts'])} number(s) about "
                                     "you not found in your facts"):
                        for r in au["facts"]:
                            st.markdown(f"- **{r['item']}** … {r['context']} …")
                if au["lit"]["rows"]:
                    with st.expander("Literature audit"):
                        st.dataframe(au["lit"]["rows"],
                                     use_container_width=True,
                                     hide_index=True)
                p = Path(state.get("saved_path", ""))
                if p.exists():
                    st.download_button("⬇️ Word", p.read_bytes(),
                                       file_name=p.name,
                                       mime="application/vnd.openxmlformats-"
                                            "officedocument.wordprocessingml"
                                            ".document", key="cp_dl")
                st.caption(f"Saved to answers/career/{slug}/")
                render_followup(
                    "cp_" + state["sig"], "Discuss and revise this document",
                    {"document": stg["article_md"].split("\n\n## References\n\n")[0]},
                    sources_text=("CANDIDATE FACTS:\n" + state["opts"]["facts"][:20000]
                                  + "\n\nPASSAGES:\n"
                                  + "\n\n".join(stg["passages"].values())[:30000]),
                    pool_texts=[state["opts"]["facts"]] + list(stg["passages"].values()),
                    apply_fn=lambda edits, pool: fu_apply_cp(state, edits, pool))
            else:
                with st.expander("Pipeline log"):
                    st.text("\n".join(state.get("log", [])))
        else:
            prev = cp_app_dir(slug) / f"{doc_key}.md"
            if prev.exists():
                with st.expander("Previously written document"):
                    st.markdown(prev.read_text(encoding="utf-8"))

    core_msg = ""
    for p in cp_app_dir(slug).glob("research_*.md"):
        if "_bundle" not in p.name:
            core_msg = p.read_text(encoding="utf-8")[:1500]
            break

    with t_cover:
        cov_instr = st.text_input("Instructions (optional)", key="cp_cov_in",
                                  placeholder="e.g. mention the pre-"
                                              "application call with the "
                                              "group head")
        _cp_simple_doc(slug, "cover_letter", "✉️ Write the cover letter",
                       CP_COVER_SYSTEM,
                       f"REQUIREMENTS:\n{req_text}\n\nCANDIDATE FACTS:\n"
                       f"{facts[:40000]}\n\nRESEARCH STATEMENT OPENING (if "
                       f"any):\n{core_msg or '(not written yet)'}\n\n"
                       f"INSTRUCTIONS: {cov_instr or '(none)'}", 2500, facts,
                       "cp_cov_go")

    with t_cv:
        if not CAREER_CV.exists():
            st.info("Save your CV in 'My facts' first.")
        else:
            _cp_simple_doc(slug, "cv_tailored", "📄 Tailor my CV",
                           CP_CV_SYSTEM,
                           f"REQUIREMENTS:\n{req_text}\n\nCV TEXT:\n"
                           f"{CAREER_CV.read_text(encoding='utf-8')[:60000]}",
                           8000, facts, "cp_cv_go")

    with t_int:
        _cp_simple_doc(slug, "interview_prep", "🎤 Prepare me",
                       CP_INTERVIEW_SYSTEM,
                       f"REQUIREMENTS:\n{req_text}\n\nCANDIDATE FACTS:\n"
                       f"{facts[:40000]}\n\nRESEARCH STATEMENT OPENING:\n"
                       f"{core_msg or '(not written yet)'}", 6000, facts,
                       "cp_int_go")

    with t_check:
        docs = req.get("documents_required") or ["CV", "Cover letter",
                                                 "Research statement"]
        chk = app.setdefault("checklist", {})
        changed = False
        for d in docs + ["Referees contacted", "Submitted"]:
            v = st.checkbox(d, value=bool(chk.get(d)), key=f"cp_chk_{slug}_{d}")
            if v != bool(chk.get(d)):
                chk[d] = v
                changed = True
        if changed:
            cp_save_apps(apps)
        gen = sorted(p.name for p in cp_app_dir(slug).glob("*.docx"))
        if gen:
            st.caption("Generated so far: " + ", ".join(gen))


# ==========================================================================
# Figure engine - sandboxed renderer for model-written matplotlib code.
#
# Contract for generated code: it defines   def draw(fig, plt, np, mpl)
# and draws onto the Figure the harness created (size, dpi, style card
# fixed by the harness). `np`, `mpl` and `plt` are vetted namespaces, not
# the real modules. The code must not create figures, save files, import
# anything but numpy / math / matplotlib sub-modules, or touch the OS.
# Five independent layers: (1) AST whitelist, (2) restricted builtins and
# a guarded __import__ in the exec namespace, (3) a separate process with
# a rebuilt environment, rlimits, its own session and a temp cwd, (4) the
# harness owns the outputs (PNG/SVG at paths the parent chose, verified by
# magic bytes and pixel width), (5) an epilogue audit that reports text
# overlaps, clipping, small text, empty axes and every number that reached
# the canvas, so the parent can check them against the declared DATA.
# ==========================================================================
import ast
import json as _fgjson
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

FIG_ALLOWED_MODULES = ("matplotlib", "numpy", "math")
FIG_MPL_SUBMODULES = ("patches", "lines", "path", "colors", "ticker",
                      "transforms", "patheffects", "gridspec", "cm", "text",
                      "table", "collections", "markers", "colormaps",
                      "legend_handler")
FIG_FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
    "exit", "quit", "help", "super", "type", "format", "object", "bytes",
    "bytearray", "id", "hash", "dir", "classmethod", "staticmethod", "property",
}
FIG_FORBIDDEN_ATTRS = {
    # introspection / frame walking
    "__subclasses__", "__globals__", "__builtins__", "__code__", "__dict__",
    "__class__", "__bases__", "__mro__", "__loader__", "__spec__", "__import__",
    "gi_frame", "gi_code", "cr_frame", "ag_frame", "f_back", "f_globals",
    "f_builtins", "f_locals", "f_code", "tb_frame", "tb_next", "mro",
    "format", "format_map",
    # OS / process
    "system", "popen", "spawn", "fork", "os", "sys", "subprocess", "socket",
    "pathlib", "shutil", "ctypes", "ctypeslib", "importlib", "builtins",
    "environ", "f2py", "distutils", "testing", "lib", "npyio", "_datasource",
    # file I/O reachable through numpy / matplotlib
    "load", "loadtxt", "genfromtxt", "fromfile", "save", "savetxt", "savez",
    "savez_compressed", "memmap", "DataSource", "tofile", "dump", "dumps",
    "open_memmap", "to_filehandle", "cbook", "imread", "imsave", "thumbnail",
    "savefig", "show", "print_figure", "print_raw", "print_rgba", "print_png",
    "print_svg", "json_load", "json_dump", "rc_file", "rc_params_from_file",
    "rc_file_defaults", "ft2font", "FT2Font", "textpath", "image",
    # subprocess-spawning matplotlib features and harness-owned state
    "font_manager", "texmanager", "animation", "rcParams", "rcParamsDefault",
    "rc", "rc_context", "rcdefaults", "use", "switch_backend", "figure",
    "close", "canvas", "pause", "subplots", "tight_layout", "subplots_adjust",
    "set_size_inches", "set_dpi", "set_layout_engine", "set_constrained_layout",
    "random",
}
# derived quantities are refused in DATA figures (values must be plotted
# as the sources state them; nothing computed)
FIG_DERIVED_ATTRS = {
    "mean", "average", "median", "std", "var", "polyfit", "polyval", "poly1d",
    "Polynomial", "gradient", "diff", "cumsum", "cumprod", "interp", "convolve",
    "lstsq", "trapz", "trapezoid", "linspace", "logspace", "geomspace",
    "percentile", "quantile", "histogram",
}
FIG_FORBIDDEN_KWARGS = {"usetex", "fname", "filename", "file", "fontfile",
                        "metaclass", "url"}
FIG_TIMEOUT_S = 60
FIG_MAX_SOURCE = 40000
FIG_MAX_NODES = 8000

FIG_WIDTHS_MM = {"single": 89.0, "double": 183.0, "onehalf": 120.0}

# Colour-blind-safe palette (Okabe-Ito) + greys, and the semantic roles the
# prompts use ("one entity, one colour, in every figure of a document").
FIG_PALETTE = {
    "blue": "#0072B2", "orange": "#E69F00", "green": "#009E73",
    "vermilion": "#D55E00", "sky": "#56B4E9", "purple": "#CC79A7",
    "yellow": "#F0E442", "black": "#000000", "grey": "#7F7F7F",
    "lightgrey": "#D9D9D9", "ink": "#1A1A1A",
}
FIG_ROLES = {
    "perovskite": "#8C4A2F", "etl": "#56B4E9", "htl": "#E69F00",
    "electrode": "#C9A227", "tco": "#7FB7BE", "glass": "#D9D9D9",
    "ion_pos": "#009E73", "ion_neg": "#CC79A7", "defect": "#D55E00",
    "light": "#F0E442", "accent": "#0072B2", "node": "#EEEEEE",
    "node_edge": "#4D4D4D", "emph": "#DCEBF7", "emph_edge": "#0072B2",
    "muted": "#D9D9D9", "muted_edge": "#B3B3B3", "ink": "#1A1A1A",
}
FIG_DATA_CYCLE = ["#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7",
                  "#56B4E9", "#808080", "#F0E442"]


def _fig_mod_ok(name):
    """Import target allowed? numpy (root only), math, matplotlib root or a
    vetted sub-module."""
    if name in ("numpy", "math", "matplotlib"):
        return True
    if name.startswith("matplotlib."):
        sub = name.split(".", 1)[1]
        return sub in FIG_MPL_SUBMODULES
    return False


def fig_validate_source(src, data_fig=False):
    """AST whitelist. Returns a list of violations (empty = OK). ALL
    violations are returned at once so one repair can fix them all."""
    problems = []
    if len(src) > FIG_MAX_SOURCE:
        return [f"source too long ({len(src)} chars)"]
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    has_draw, n_nodes = False, 0
    for node in ast.walk(tree):
        n_nodes += 1
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _fig_mod_ok(a.name):
                    problems.append(f"import of '{a.name}' not allowed "
                                    "(numpy, math, matplotlib.<patches|lines|"
                                    "path|colors|ticker|transforms|patheffects|"
                                    "gridspec|cm|text|table|collections|markers> only)")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                problems.append("relative imports not allowed")
            elif not _fig_mod_ok(mod):
                problems.append(f"import from '{mod}' not allowed")
            for a in node.names:
                if a.name == "*":
                    problems.append("star imports not allowed")
                elif mod == "matplotlib" and a.name not in FIG_MPL_SUBMODULES:
                    problems.append(f"'from matplotlib import {a.name}' not allowed")
                elif a.name in FIG_FORBIDDEN_ATTRS or a.name in FIG_FORBIDDEN_NAMES \
                        or a.name.startswith("_"):
                    problems.append(f"import of '{mod}.{a.name}' not allowed")
                elif data_fig and a.name in FIG_DERIVED_ATTRS:
                    problems.append(f"'{a.name}' computes a derived quantity - "
                                    "data figures plot only the source values")
                if a.asname and (a.asname in FIG_FORBIDDEN_NAMES or a.asname.startswith("_")):
                    problems.append(f"alias '{a.asname}' not allowed")
        elif isinstance(node, ast.Name):
            if node.id in FIG_FORBIDDEN_NAMES:
                problems.append(f"use of '{node.id}' not allowed"
                                + (" (use f-strings)" if node.id == "format" else ""))
        elif isinstance(node, ast.Attribute):
            if node.attr in FIG_FORBIDDEN_ATTRS or node.attr.startswith("_"):
                problems.append(f"attribute '{node.attr}' not allowed"
                                + (" (use f-strings)" if node.attr == "format" else ""))
            elif data_fig and node.attr in FIG_DERIVED_ATTRS:
                problems.append(f"'{node.attr}' computes a derived quantity - "
                                "data figures plot only the values in DATA")
        elif isinstance(node, ast.keyword):
            if node.arg in FIG_FORBIDDEN_KWARGS:
                problems.append(f"keyword argument '{node.arg}' not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.ClassDef,
                               ast.AsyncFunctionDef, ast.Await, ast.AsyncFor,
                               ast.AsyncWith, ast.Yield, ast.YieldFrom)):
            problems.append(f"{type(node).__name__} not allowed")
        elif isinstance(node, ast.While):
            problems.append("while loops not allowed (use for loops)")
        elif isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                problems.append("decorators not allowed")
            if node.name == "draw":
                has_draw = True
                args = [a.arg for a in node.args.args]
                if args[:4] != ["fig", "plt", "np", "mpl"]:
                    problems.append("draw must be defined as draw(fig, plt, np, mpl)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            problems.append("bytes literals not allowed")
    if n_nodes > FIG_MAX_NODES:
        problems.append(f"code too large ({n_nodes} AST nodes)")
    if not has_draw:
        problems.append("no draw(fig, plt, np, mpl) function defined")
    return sorted(set(problems))


FIG_RUNNER = r'''
import sys, json, math, resource, signal, io, contextlib, re, types
spec = json.loads(sys.stdin.read())
sys.path[:0] = [p for p in spec.get("sys_path", []) if p not in sys.path]
for _res, _val in (("RLIMIT_AS", 2 * 1024 ** 3), ("RLIMIT_FSIZE", 60 * 1024 ** 2),
                   ("RLIMIT_NPROC", 64)):
    try:
        _r = getattr(resource, _res)
        resource.setrlimit(_r, (_val, _val))
    except Exception:
        pass
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy
import matplotlib as mpl
from matplotlib import (patches, lines, path, colors, ticker, transforms,
                        patheffects, gridspec, cm, text, table, collections,
                        markers)
try:
    from matplotlib import legend_handler
except Exception:
    legend_handler = None

W_IN = spec["width_mm"] / 25.4
H_IN = spec["height_mm"] / 25.4
mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "Helvetica Neue", "Liberation Sans",
                        "Nimbus Sans", "DejaVu Sans"],
    "font.size": 7.5, "axes.titlesize": 7.5, "axes.labelsize": 7.5,
    "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
    "legend.frameon": False, "legend.handlelength": 1.2,
    "mathtext.fontset": "custom", "mathtext.rm": "sans", "mathtext.it": "sans:italic",
    "mathtext.bf": "sans:bold", "mathtext.fallback": "stixsans",
    "axes.linewidth": 0.6, "lines.linewidth": 0.8, "lines.markersize": 3.5,
    "patch.linewidth": 0.6, "axes.edgecolor": "#1A1A1A", "axes.labelcolor": "#1A1A1A",
    "xtick.major.width": 0.6, "ytick.major.width": 0.6, "xtick.major.size": 2.5,
    "ytick.major.size": 2.5, "xtick.direction": "out", "ytick.direction": "out",
    "xtick.color": "#1A1A1A", "ytick.color": "#1A1A1A", "text.color": "#1A1A1A",
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
    "axes.axisbelow": True, "axes.unicode_minus": True,
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "figure.dpi": 100, "savefig.dpi": 300, "svg.fonttype": "none", "pdf.fonttype": 42,
    "text.usetex": False, "path.simplify": True,
    "axes.prop_cycle": mpl.cycler(color=spec.get("cycle") or
                                  ["#0072B2", "#D55E00", "#009E73", "#E69F00",
                                   "#CC79A7", "#56B4E9", "#808080"]),
})
fig = plt.figure(figsize=(W_IN, H_IN))
try:
    fig.set_layout_engine("constrained")
except Exception:
    try:
        fig.set_constrained_layout(True)
    except Exception:
        pass

# ---- vetted namespaces handed to the model code ----------------------
_NP_NAMES = """array asarray arange zeros ones full empty zeros_like ones_like full_like
linspace logspace geomspace meshgrid concatenate stack vstack hstack column_stack
transpose reshape ravel flip roll repeat tile unique sort argsort searchsorted
sin cos tan arcsin arccos arctan arctan2 sinh cosh tanh exp log log10 log2 sqrt
cbrt power square abs absolute sign floor ceil round rint clip where isnan isfinite
nan inf pi e newaxis sum prod min max argmin argmax mean average median std var
cumsum cumprod diff gradient interp polyfit polyval poly1d dot cross outer
deg2rad rad2deg radians degrees hypot minimum maximum nanmin nanmax nansum
float64 float32 int64 int32 bool_ ndarray number integer floating any all
count_nonzero nonzero take put_along_axis insert delete append around fix mod
trapz trapezoid percentile quantile histogram""".split()
np_ns = types.SimpleNamespace(**{n: getattr(numpy, n) for n in _NP_NAMES
                                 if hasattr(numpy, n)})
np_ns.linalg = types.SimpleNamespace(norm=numpy.linalg.norm)
mpl_ns = types.SimpleNamespace(
    patches=patches, lines=lines, path=path, colors=colors, ticker=ticker,
    transforms=transforms, patheffects=patheffects, gridspec=gridspec, cm=cm,
    text=text, table=table, collections=collections, markers=markers,
    legend_handler=legend_handler, colormaps=getattr(matplotlib, "colormaps", None),
    cycler=mpl.cycler)
plt_ns = types.SimpleNamespace(
    cm=cm, get_cmap=getattr(plt, "get_cmap", None), Circle=patches.Circle,
    Rectangle=patches.Rectangle, Polygon=patches.Polygon, Line2D=lines.Line2D,
    Arrow=patches.Arrow, Text=text.Text, Normalize=colors.Normalize,
    MaxNLocator=ticker.MaxNLocator, FuncFormatter=ticker.FuncFormatter,
    colormaps=getattr(matplotlib, "colormaps", None), cycler=mpl.cycler)
_SUBS = {"patches": patches, "lines": lines, "path": path, "colors": colors,
         "ticker": ticker, "transforms": transforms, "patheffects": patheffects,
         "gridspec": gridspec, "cm": cm, "text": text, "table": table,
         "collections": collections, "markers": markers,
         "legend_handler": legend_handler,
         "colormaps": getattr(matplotlib, "colormaps", None)}

import builtins as _bi
def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Import statements written by the model are already whitelisted by the
    # AST gate; this runtime guard hands the model vetted namespaces and
    # lets library-internal lazy imports through. C code called from the
    # model's frame (e.g. ndarray.sum -> numpy._core._methods) resolves
    # __import__ through this frame's builtins with fromlist ('__doc__',).
    internal = bool(fromlist) and "__doc__" in tuple(fromlist)
    if level != 0:
        raise ImportError("relative imports are not allowed in figure code")
    if name == "math":
        return math
    root = name.split(".")[0]
    if root in ("numpy", "matplotlib"):
        if name == "numpy":
            return _bi.__import__(name, globals, locals, fromlist, level) if internal else np_ns
        if name == "matplotlib":
            return _bi.__import__(name, globals, locals, fromlist, level) if internal else mpl_ns
        sub = name.split(".", 1)[1]
        if root == "matplotlib" and sub in _SUBS and _SUBS[sub] is not None:
            return _SUBS[sub] if fromlist else mpl_ns
        # deeper library-internal modules (numpy._core._methods, backends...)
        mod = _bi.__import__(name, globals, locals, fromlist, level)
        return mod if fromlist else (np_ns if root == "numpy" else mpl_ns)
    if internal:
        return _bi.__import__(name, globals, locals, fromlist, level)
    raise ImportError("import of %r is not allowed in figure code" % name)

_B = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
_SAFE = ("abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
         "isinstance", "len", "list", "map", "max", "min", "range", "round",
         "set", "sorted", "str", "sum", "tuple", "zip", "reversed", "print",
         "ValueError", "TypeError", "Exception", "KeyError", "IndexError",
         "True", "False", "None", "divmod", "pow", "filter", "iter", "next",
         "chr", "ord", "frozenset", "complex", "hasattr", "repr", "slice",
         "callable", "StopIteration", "ZeroDivisionError", "ArithmeticError",
         "AttributeError", "RuntimeError", "NotImplementedError")
ns = {"__builtins__": {k: _B[k] for k in _SAFE if k in _B},
      "math": math, "np": np_ns, "plt": plt_ns, "mpl": mpl_ns,
      # uploaded data tables (file-backed data figures): read-only lists
      "TABLES": spec.get("tables") or {}}
ns["__builtins__"]["__import__"] = _guarded_import

def _alarm(*a):
    raise TimeoutError("drawing exceeded the time limit")
signal.signal(signal.SIGALRM, _alarm)
signal.alarm(int(spec["timeout"]))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(compile(spec["source"], "<figure>", "exec"), ns)
    ns["draw"](fig, plt_ns, np_ns, mpl_ns)
signal.alarm(0)
if tuple(round(v, 4) for v in fig.get_size_inches()) != (round(W_IN, 4), round(H_IN, 4)):
    fig.set_size_inches(W_IN, H_IN)
fig.canvas.draw()
renderer = fig.canvas.get_renderer()

# ---- epilogue audit: layout ------------------------------------------
_NUM = re.compile(r"(?<![A-Za-z0-9_$^{.])[-−]?\d+(?:[.,]\d+)?(?:\s*[×x]\s*10\^?\{?-?\d+\}?|e[-+]?\d+)?(?![A-Za-z0-9_])")

def _nums_in(s):
    out = []
    for m in _NUM.finditer(s or ""):
        tok = m.group(0).replace("−", "-").replace(" ", "").replace(",", "")
        tok = tok.replace("^", "").replace("{", "").replace("}", "")
        tok = tok.replace("×10", "e").replace("x10", "e")
        out.append(tok)
    return out

def _isnum(s):
    try:
        float((s or "").replace("−", "-").replace(",", ""))
        return True
    except ValueError:
        return False

def _bb(t):
    try:
        if not t.get_visible() or not (t.get_text() or "").strip():
            return None
        b = t.get_window_extent(renderer)
        if b.width <= 0 or b.height <= 0:
            return None
        return b
    except Exception:
        return None

texts = []   # (text, bbox, fontsize, is_tick, axes index)
for t in fig.texts:
    b = _bb(t)
    if b is not None:
        texts.append((t.get_text(), b, t.get_fontsize(), False, -1))
for ai, ax in enumerate(fig.axes):
    for t in list(ax.texts) + [ax.title, ax.xaxis.label, ax.yaxis.label]:
        b = _bb(t)
        if b is not None:
            texts.append((t.get_text(), b, t.get_fontsize(), False, ai))
    leg = ax.get_legend()
    if leg is not None:
        for t in leg.get_texts():
            b = _bb(t)
            if b is not None:
                texts.append((t.get_text(), b, t.get_fontsize(), False, ai))
    if ax.axison:
        # only ticks inside the view interval are drawn; the others would
        # be reported as clipped although they never appear
        for axis in (ax.xaxis, ax.yaxis):
            lo, hi = sorted(axis.get_view_interval())
            for tick in axis.get_major_ticks():
                try:
                    loc = tick.get_loc()
                except Exception:
                    continue
                if not (lo - 1e-9 <= loc <= hi + 1e-9):
                    continue
                t = tick.label1
                b = _bb(t)
                if b is not None:
                    texts.append((t.get_text(), b, t.get_fontsize(), True, ai))

def _inter(a, b):
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return w * h if (w > 0 and h > 0) else 0.0

overlaps, clipped, small = [], [], []
fb = fig.bbox
for i in range(len(texts)):
    ti, bi, fi, tick_i, ax_i = texts[i]
    if fi < 5.5:
        small.append({"text": ti[:40], "pt": round(fi, 1)})
    if bi.x0 < fb.x0 - 2 or bi.y0 < fb.y0 - 2 or bi.x1 > fb.x1 + 2 or bi.y1 > fb.y1 + 2:
        clipped.append({"text": ti[:40]})
    for j in range(i + 1, len(texts)):
        tj, bj, fj, tick_j, ax_j = texts[j]
        if tick_i and tick_j and ax_i == ax_j:
            continue
        inter = _inter(bi, bj)
        if inter <= 0:
            continue
        frac = inter / max(1e-9, min(bi.width * bi.height, bj.width * bj.height))
        if frac >= 0.15:
            overlaps.append({"a": ti[:40], "b": tj[:40], "frac": round(frac, 2)})

empty_axes = []
for ai, ax in enumerate(fig.axes):
    if not (ax.lines or ax.patches or ax.collections or ax.texts or ax.images
            or ax.tables):
        empty_axes.append(ai)

# text over data marks (dot plots / scatter)
text_marker = []
for ai, ax in enumerate(fig.axes):
    if not ax.axison:
        continue
    pts = []
    for l in ax.lines:
        mk = l.get_marker()
        if mk in (None, "None", "", " "):
            continue
        try:
            xy = l.get_xydata()
            pts.extend(ax.transData.transform(xy).tolist()[:400])
        except Exception:
            pass
    for c in ax.collections:
        if isinstance(c, collections.PathCollection):
            try:
                off = c.get_offsets()
                pts.extend(ax.transData.transform(off).tolist()[:400])
            except Exception:
                pass
    for (tt, bb, ff, tick, axi) in texts:
        if tick or axi != ai:
            continue
        n_in = sum(1 for (x, y) in pts if bb.x0 <= x <= bb.x1 and bb.y0 <= y <= bb.y1)
        if n_in:
            text_marker.append({"text": tt[:40], "marks": n_in})

# ---- epilogue audit: what numbers reached the canvas ------------------
vals_x, vals_y, text_nums = [], [], []
quant_axes, n_err, long_series = 0, 0, 0
for ax in fig.axes:
    skip = set()
    for c in getattr(ax, "containers", []):
        cname = type(c).__name__
        if cname == "ErrorbarContainer":
            n_err += 1
            try:
                _dl, caps, bars = c.lines
                skip.update(id(l) for l in caps)
                skip.update(id(l) for l in bars)
            except Exception:
                pass
        elif cname == "BarContainer":
            vertical = getattr(c, "orientation", "vertical") == "vertical"
            for r in c.patches:
                try:
                    vals_y.append(float(r.get_height() if vertical else r.get_width()))
                except Exception:
                    pass
    for l in ax.lines:
        if id(l) in skip:
            continue
        try:
            xd = numpy.asarray(l.get_xdata(), dtype=float)
            yd = numpy.asarray(l.get_ydata(), dtype=float)
        except Exception:
            continue
        if yd.size > 30:
            long_series += 1
        vals_x.extend(float(v) for v in xd[:200] if numpy.isfinite(v))
        vals_y.extend(float(v) for v in yd[:200] if numpy.isfinite(v))
    for c in ax.collections:
        if isinstance(c, collections.PathCollection):
            try:
                off = numpy.asarray(c.get_offsets(), dtype=float)
                if off.ndim == 2 and off.shape[1] == 2:
                    vals_x.extend(float(v) for v in off[:200, 0] if numpy.isfinite(v))
                    vals_y.extend(float(v) for v in off[:200, 1] if numpy.isfinite(v))
            except Exception:
                pass
    if ax.axison:
        labs = [t.get_text() for t in list(ax.get_xticklabels()) + list(ax.get_yticklabels())
                if t.get_visible()]
        if any(_isnum(s) for s in labs if s.strip()):
            quant_axes += 1
    for t in ax.texts:
        text_nums.extend(_nums_in(t.get_text()))
    for tb in ax.tables:
        for cell in tb.get_celld().values():
            try:
                text_nums.extend(_nums_in(cell.get_text().get_text()))
            except Exception:
                pass
for t in fig.texts:
    text_nums.extend(_nums_in(t.get_text()))

meta = {"Description": spec.get("meta_desc", ""), "Source": "GrapheAI figure engine"}
fig.savefig(spec["png"], dpi=300, bbox_inches=None, facecolor="white",
            metadata={"Description": meta["Description"], "Source": meta["Source"]})
fig.savefig(spec["svg"], bbox_inches=None, facecolor="white",
            metadata={"Description": meta["Description"], "Source": meta["Source"]})
if spec.get("pdf"):
    fig.savefig(spec["pdf"], bbox_inches=None, facecolor="white",
                metadata={"Subject": meta["Description"], "Creator": meta["Source"]})
print(json.dumps({
    "ok": True, "n_axes": len(fig.get_axes()),
    "n_text": len(texts),
    "audit": {"overlaps": overlaps[:30], "clipped": clipped[:30],
              "small_text": small[:30], "empty_axes": empty_axes[:10],
              "text_marker": text_marker[:20]},
    "harvest": {"vals_x": sorted(set(round(v, 6) for v in vals_x))[:400],
                "vals_y": sorted(set(round(v, 6) for v in vals_y))[:400],
                "text_nums": sorted(set(text_nums))[:200],
                "quant_axes": quant_axes, "n_errorbars": n_err,
                "long_series": long_series},
    "versions": {"matplotlib": matplotlib.__version__, "numpy": numpy.__version__},
    "stdout_tail": buf.getvalue()[-300:],
}))
'''

FIG_MPLCONFIG = Path(tempfile.gettempdir()) / "grapheai_mplconfig"


def _fig_child_env(job_tmp):
    """Environment rebuilt from scratch (no API key, no user site hooks
    beyond what matplotlib needs); persistent font cache."""
    FIG_MPLCONFIG.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "MPLBACKEND": "Agg",
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": str(job_tmp), "TMPDIR": str(job_tmp), "LANG": "C.UTF-8",
           "MPLCONFIGDIR": str(FIG_MPLCONFIG.resolve())}
    for k in ("PYTHONPATH", "PYTHONUSERBASE", "CONDA_PREFIX", "DYLD_LIBRARY_PATH",
              "LD_LIBRARY_PATH"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _fig_png_ok(png, width_mm):
    """Magic bytes + IHDR width within 2 px of the requested print width."""
    try:
        b = Path(png).read_bytes()
        if not b.startswith(b"\x89PNG\r\n\x1a\n") or len(b) < 2000:
            return False, "PNG missing or malformed"
        w = int.from_bytes(b[16:20], "big")
        want = round(width_mm / 25.4 * 300)
        if abs(w - want) > 2:
            return False, f"PNG width {w} px differs from the {want} px print width"
        return True, ""
    except Exception as e:
        return False, f"PNG check failed: {e}"


def _fig_svg_ok(svg):
    try:
        s = Path(svg).read_text(encoding="utf-8", errors="replace")
        head = s.lstrip()[:200].lower()
        if not (head.startswith("<?xml") or head.startswith("<svg")) or "</svg>" not in s:
            return False, "SVG malformed"
        low = s.lower()
        for bad in ("<script", "<foreignobject", 'href="http', "href='http"):
            if bad in low:
                return False, f"SVG contains {bad!r} - renderer output rejected"
        return True, ""
    except Exception as e:
        return False, f"SVG check failed: {e}"


def _fig_clean_error(err, limit=2000):
    """Traceback text that goes back into a prompt (and into state): keep
    the frames of the model's own module and the final message, redact
    absolute paths, cap the length."""
    lines = (err or "").splitlines()
    keep = []
    for i, ln in enumerate(lines):
        if 'File "<figure>"' in ln:
            keep.append(ln.strip())
            if i + 1 < len(lines):
                keep.append(lines[i + 1].rstrip())
    tail = [l for l in lines[-3:] if l.strip()]
    if not keep:
        keep = [l for l in lines[-8:] if l.strip()]
    out = keep + [l for l in tail if l not in keep]
    out = [re.sub(r'(?<![\w])/[\w.\-/ ]+', "<path>", l) for l in out]
    return "\n".join(out)[-limit:]


def fig_render(source, png_path, svg_path, width="double", height_mm=None,
               timeout=FIG_TIMEOUT_S, python=None, width_mm=None,
               meta_desc="", data_fig=False, pdf_path=None, tables=None):
    """Validate + render in an isolated subprocess. Returns dict with
    ok, error (cleaned traceback text), audit, harvest, versions.
    `tables` ({name: {"columns": [...], "data": {col: [...]}}}) is exposed
    to the code as the read-only global TABLES (file-backed data)."""
    problems = fig_validate_source(source, data_fig=data_fig)
    if problems:
        return {"ok": False, "stage": "validate",
                "error": "REJECTED BY SANDBOX:\n- " + "\n- ".join(problems)}
    width_mm = float(width_mm or FIG_WIDTHS_MM.get(width, FIG_WIDTHS_MM["double"]))
    h_mm = float(height_mm or round(width_mm * 0.62, 1))
    h_mm = max(30.0, min(h_mm, width_mm * 1.4))
    png_abs, svg_abs = str(Path(png_path).resolve()), str(Path(svg_path).resolve())
    pdf_abs = str(Path(pdf_path).resolve()) if pdf_path else ""
    spec = {"source": source, "png": png_abs, "svg": svg_abs, "pdf": pdf_abs,
            "width_mm": width_mm, "height_mm": h_mm, "timeout": int(timeout),
            "meta_desc": (meta_desc or "")[:2000], "cycle": FIG_DATA_CYCLE,
            "tables": tables or {},
            "sys_path": [p for p in sys.path if p]}
    job_tmp = Path(tempfile.mkdtemp(prefix="figjob_"))
    for p in (png_abs, svg_abs, pdf_abs):
        if not p:
            continue
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        proc = subprocess.Popen(
            [python or sys.executable, "-B", "-c", FIG_RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_fig_child_env(job_tmp), cwd=str(job_tmp),
            start_new_session=True)
        try:
            out, err = proc.communicate(_fgjson.dumps(spec), timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.communicate()
            return {"ok": False, "stage": "render",
                    "error": f"rendering exceeded {timeout} s (too heavy or an "
                             "endless loop) - simplify the drawing"}
    except Exception as e:
        return {"ok": False, "stage": "render",
                "error": f"could not start the renderer: {e}"}
    finally:
        import shutil as _sh
        _sh.rmtree(job_tmp, ignore_errors=True)
    lines = (out or "").strip().splitlines()
    if proc.returncode == 0 and lines:
        try:
            res = _fgjson.loads(lines[-1])
        except Exception:
            res = None
        if res and res.get("ok"):
            ok, why = _fig_png_ok(png_abs, width_mm)
            if ok:
                ok, why = _fig_svg_ok(svg_abs)
            if ok and pdf_abs:
                try:
                    ok = Path(pdf_abs).read_bytes()[:5] == b"%PDF-"
                except Exception:
                    ok = False
                why = "" if ok else "PDF missing or malformed"
            if ok:
                res["stage"] = "ok"
                res["width_mm"], res["height_mm"] = width_mm, h_mm
                return res
            return {"ok": False, "stage": "verify", "error": why}
    return {"ok": False, "stage": "render",
            "error": _fig_clean_error(err) or "renderer produced no output"}


def fig_placeholder_source(title, note):
    """Loud placeholder: dashed frame, PLACEHOLDER, the title and the
    author request rendered inside the image."""
    t = _fgjson.dumps(textwrap.fill(title or "Figure", 60))
    n = _fgjson.dumps(textwrap.fill(note or "", 70))
    return (
        "DATA = {}\n"
        "def draw(fig, plt, np, mpl):\n"
        "    ax = fig.add_subplot(111)\n"
        "    ax.set_axis_off(); ax.set_xlim(0, 1); ax.set_ylim(0, 1)\n"
        "    ax.add_patch(mpl.patches.FancyBboxPatch((0.03, 0.05), 0.94, 0.9,\n"
        "        boxstyle='round,pad=0.01', fill=False, linestyle='--', linewidth=1.0,\n"
        "        edgecolor='#D55E00'))\n"
        "    ax.text(0.5, 0.8, 'PLACEHOLDER', ha='center', va='center', fontsize=11,\n"
        "            fontweight='bold', color='#D55E00')\n"
        f"    ax.text(0.5, 0.58, {t}, ha='center', va='center', fontsize=7.5)\n"
        f"    ax.text(0.5, 0.28, {n}, ha='center', va='center', fontsize=7,\n"
        "            color='#4D4D4D')\n")
# Vision call for the figure critic. API backend: the rendered PNG goes in
# as an image block. Claude Max backend: Claude Code reads the PNG with its
# Read tool (image input through the Agent SDK), enabled only after a probe
# has confirmed that image content actually reaches the model.
def call_claude_vision(api_key, system, text, png_path, model, max_tokens=3000):
    import base64
    import time
    if st.session_state.get("backend") == "codex":
        return call_codex_vision(system, text, png_path, max_tokens)
    if st.session_state.get("backend") == "openai":
        return call_openai_vision(system, text, png_path, max_tokens)
    if st.session_state.get("backend") == "max":
        return call_claude_vision_max(system, text, png_path, model)
    import anthropic
    data = base64.standard_b64encode(Path(png_path).read_bytes()).decode("utf-8")
    client = anthropic.Anthropic(api_key=api_key)
    kw = _api_kwargs(model, system, text, max_tokens)
    kw["messages"] = [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": data}},
        {"type": "text", "text": text}]}]
    last = None
    for attempt in range(len(RW_RETRY_WAITS) + 1):
        try:
            resp = _api_create(client, kw)
            _track_usage(resp.usage.input_tokens, resp.usage.output_tokens, model)
            return _response_text(resp)
        except Exception as e:
            last = e
            if attempt == len(RW_RETRY_WAITS) or not any(
                    k in str(e).lower() for k in RW_TRANSIENT):
                raise
            time.sleep(RW_RETRY_WAITS[attempt])
    raise last


def call_claude_vision_max(system, text, png_path, model, max_turns=4):
    """Max backend: run Claude Code with only the Read tool, in the folder
    of the PNG, and ask it to read the image before judging."""
    from claude_agent_sdk import (query, ClaudeAgentOptions, AssistantMessage,
                                  TextBlock, ResultMessage)
    import asyncio
    p = Path(png_path).resolve()
    status = claude_code_status()
    extra = {"cli_path": status["path"]} if status.get("path") else {}
    prompt = (f"First use the Read tool to view the image file '{p.name}' in the "
              "current working directory (it is a PNG figure). Then answer the "
              f"request below based on what you see.\n\n{text}")

    async def _run():
        # Read is pre-approved through allowed_tools; no permission mode
        # override (bypassPermissions is refused for root users and is not
        # needed for a read-only tool).
        opts = ClaudeAgentOptions(
            system_prompt=system, model=model, max_turns=max_turns,
            allowed_tools=["Read"], cwd=str(p.parent),
            disallowed_tools=["Bash", "Edit", "Write", "Glob", "Grep",
                              "WebSearch", "WebFetch", "Task", "NotebookEdit"],
            **extra)
        parts, usage = [], None
        async for message in query(prompt=prompt, options=opts):
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

    out, usage = asyncio.run(_run())
    try:
        _track_usage(int(usage.get("input_tokens", 0)),
                     int(usage.get("output_tokens", 0)))
    except Exception:
        _track_usage(0, 0)
    if not out.strip():
        raise RuntimeError("Claude Code returned no text for the figure review")
    return out


FIG_PROBE_WORD, FIG_PROBE_NUMBER = "GRAPHEAI", "4173"


def fig_probe_source():
    return (
        "DATA = {}\n"
        "def draw(fig, plt, np, mpl):\n"
        "    ax = fig.add_subplot(111); ax.set_axis_off(); ax.set_xlim(0, 1); ax.set_ylim(0, 1)\n"
        "    ax.add_patch(mpl.patches.Circle((0.2, 0.5), 0.12, color='#D55E00'))\n"
        f"    ax.text(0.6, 0.62, '{FIG_PROBE_WORD}', ha='center', fontsize=16, fontweight='bold')\n"
        f"    ax.text(0.6, 0.35, '{FIG_PROBE_NUMBER}', ha='center', fontsize=16, color='#0072B2')\n")


def fig_max_vision_ok(model, refresh=False):
    """Can the Max backend see images? Probe once per Claude Code version:
    render a known image and ask what it shows. Cached in the session and
    on disk under figures_gen/.max_vision_probe.json."""
    key = "_max_vision_ok"
    if not refresh and key in st.session_state:
        return st.session_state[key]
    cli = claude_code_status().get("version", "?")
    cache = FIG_DIR / ".max_vision_probe.json"
    if not refresh and cache.exists():
        try:
            rec = _rwjson.loads(cache.read_text(encoding="utf-8"))
            if rec.get("cli") == cli and "ok" in rec:
                st.session_state[key] = bool(rec["ok"])
                return st.session_state[key]
        except Exception:
            pass
    ok, detail = False, ""
    try:
        d = FIG_DIR / "_probe"
        d.mkdir(parents=True, exist_ok=True)
        png, svg = d / "probe.png", d / "probe.svg"
        res = fig_render(fig_probe_source(), png, svg, width="single", height_mm=45)
        if res.get("ok"):
            ans = call_claude_vision_max(
                "You describe images precisely.",
                "Reply with exactly the word and the number printed in the image, "
                "and the colour of the circle. Nothing else.", png, model, max_turns=3)
            ok = FIG_PROBE_NUMBER in ans and FIG_PROBE_WORD.lower() in ans.lower()
            detail = ans[:200]
        else:
            detail = res.get("error", "")[:200]
    except Exception as e:
        detail = str(e)[:200]
    st.session_state[key] = ok
    try:
        FIG_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(_rwjson.dumps({"cli": cli, "ok": ok, "detail": detail}),
                         encoding="utf-8")
    except Exception:
        pass
    return ok


def fig_downscale_for_qa(png_path, max_px=1400):
    """A smaller copy for the critic (token cost) - returns a path."""
    try:
        from PIL import Image
        im = Image.open(png_path)
        if max(im.size) <= max_px:
            return png_path
        im.thumbnail((max_px, max_px))
        out = Path(png_path).with_name(Path(png_path).stem + "_qa.png")
        im.save(out)
        return out
    except Exception:
        return png_path


class _fig_effort:
    """Temporarily cap the reasoning effort for one call (the coder and
    critic do not need the planner's depth). Restores the sidebar value."""
    _ORDER = ("low", "medium", "high", "xhigh", "max")

    def __init__(self, cap):
        self.cap = cap

    def __enter__(self):
        self.prev = st.session_state.get("effort")
        cur = self.prev if self.prev in self._ORDER else "xhigh"
        if self.cap in self._ORDER and self._ORDER.index(self.cap) < self._ORDER.index(cur):
            st.session_state["effort"] = self.cap
        return self

    def __exit__(self, *a):
        if self.prev is None:
            st.session_state.pop("effort", None)
        else:
            st.session_state["effort"] = self.prev
        return False
# ==========================================================================
# Figure engine - core (design-independent): data-provenance contract,
# numeric audit of generated code, canvas audit of what was actually
# drawn, deterministic file naming, markdown insertion, repair loop,
# regeneration with author feedback.
# ==========================================================================
FIG_TRIGGERS = ("schematic", "conceptual model", "mechanism", "comparison",
                "workflow", "architecture", "roadmap", "taxonomy",
                "structure-property relationship", "process",
                "graphical summary", "graphical abstract", "timeline",
                "device stack", "energy diagram", "decision tree")

FIG_KINDS_CONCEPTUAL = {"schematic", "mechanism", "workflow", "architecture",
                        "roadmap", "taxonomy", "process", "graphical_abstract",
                        "conceptual_model", "timeline", "device_stack",
                        "energy_diagram", "decision_tree"}
FIG_KINDS_DATA = {"comparison", "structure_property", "trend", "data_chart"}

FIG_DIR = ANSWERS_DIR / "figures_gen"

# Journal display-item conventions (panel-label form, caption lead, column
# widths in mm). Keys are matched by keyword against the profile / venue
# name; unknown -> generic (Nature-like).
FIG_JOURNAL_STYLES = {
    "nature": {"panel": "bold lowercase letters a, b, c", "lead": " | ",
               "single_mm": 89.0, "double_mm": 183.0, "onehalf_mm": 120.0},
    "science": {"panel": "bold uppercase letters A, B, C", "lead": ". ",
                "single_mm": 55.0, "double_mm": 120.0, "onehalf_mm": 120.0},
    "cell": {"panel": "bold uppercase letters A, B, C", "lead": ". ",
             "single_mm": 85.0, "double_mm": 174.0, "onehalf_mm": 114.0},
    "rsc": {"panel": "lowercase letters in parentheses (a), (b), (c)", "lead": " ",
            "single_mm": 83.0, "double_mm": 171.0, "onehalf_mm": 120.0},
    "wiley": {"panel": "lowercase letters with a closing parenthesis a), b), c)",
              "lead": ". ", "single_mm": 85.0, "double_mm": 175.0, "onehalf_mm": 120.0},
    "acs": {"panel": "lowercase letters in parentheses (a), (b), (c)", "lead": ". ",
            "single_mm": 82.5, "double_mm": 178.0, "onehalf_mm": 120.0},
    "generic": {"panel": "bold lowercase letters a, b, c", "lead": " | ",
                "single_mm": 89.0, "double_mm": 183.0, "onehalf_mm": 120.0},
}


def fig_journal_style(name):
    n = (name or "").lower()
    if "nature" in n:
        key = "nature"
    elif any(k in n for k in ("rsc", "environmental science", "chemical society",
                              "energy & env", "ees")):
        key = "rsc"                  # before 'science' (E&E Science)
    elif any(k in n for k in ("joule", "matter", "cell press", "cell ")):
        key = "cell"
    elif any(k in n for k in ("wiley", "advanced ")):
        key = "wiley"
    elif any(k in n for k in ("acs", "jacs", "energy letters")):
        key = "acs"
    elif "science" in n:
        key = "science"
    else:
        key = "generic"
    d = dict(FIG_JOURNAL_STYLES[key])
    d["key"], d["name"] = key, name or "generic"
    return d


def fig_job_dir(job_sig):
    d = FIG_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", str(job_sig))[:40]
    d.mkdir(parents=True, exist_ok=True)
    return d


def fig_paths(job_sig, number):
    d = fig_job_dir(job_sig)
    return d / f"fig_{number:02d}.png", d / f"fig_{number:02d}.svg", \
        d / f"fig_{number:02d}.py"


def fig_numbers_in(text):
    """Every number in a source text as a canonical key (unlike
    rw_numbers_in this keeps 1-2 digit integers: a figure may legitimately
    plot '25 %' or '65 C')."""
    out = set()
    for m in RW_NUM_RE.finditer(text or ""):
        out.add(_fig_num_key(_rw_norm_num(m.group(0))))
    return out


def _fig_num_key(v):
    """Canonical key for exact comparison of numbers written differently
    (25.20 == 25.2 == 2.52e1)."""
    try:
        f = float(str(v).replace("−", "-"))
    except (TypeError, ValueError):
        return str(v).strip()
    if f == 0:
        return "0"
    return f"{f:.6g}"


# ------------------------------------------------------------ data tables
def fig_tables_pack(tables, max_rows=2000, max_cols=40):
    """Uploaded CSV/XLSX tables -> the read-only TABLES structure handed
    to the renderer: {name: {"columns": [...], "data": {col: [...]},
    "n_rows": n}}. Numeric cells become floats (None where blank)."""
    pack = {}
    for t in tables or []:
        cols = [str(c) for c in t.get("columns", [])][:max_cols]
        rows = t.get("rows", [])[:max_rows]
        data = {}
        for j, c in enumerate(cols):
            col = []
            for r in rows:
                v = r[j] if j < len(r) else None
                if v is None or (isinstance(v, str) and not v.strip()):
                    col.append(None)
                    continue
                try:
                    col.append(float(str(v).replace(",", "").replace("−", "-")))
                except ValueError:
                    col.append(str(v))
            data[c] = col
        pack[str(t.get("name", "table"))] = {"columns": cols, "data": data,
                                             "n_rows": len(rows)}
    return pack


def fig_tables_numbers(pack):
    """Canonical keys of every numeric cell (provenance pool for file data)."""
    out = set()
    for t in (pack or {}).values():
        for col in t.get("data", {}).values():
            for v in col:
                if isinstance(v, float):
                    out.add(_fig_num_key(v))
    return out


def fig_tables_schema(pack, sample=5):
    """Human/model-readable schema of the tables for the planner and coder."""
    lines = []
    for name, t in (pack or {}).items():
        lines.append(f"TABLE '{name}' ({t.get('n_rows', 0)} rows) - source ID FILE:{name}")
        for c in t["columns"]:
            col = t["data"].get(c, [])
            nums = [v for v in col if isinstance(v, float)]
            if nums:
                lines.append(f"  - {c}: numeric, {len(nums)} values, min {min(nums):g}, "
                             f"max {max(nums):g}, first {', '.join(f'{v:g}' for v in nums[:sample])}")
            else:
                cats = [v for v in col if isinstance(v, str)]
                uniq = list(dict.fromkeys(cats))[:8]
                lines.append(f"  - {c}: text ({len(uniq)}+ distinct): {', '.join(uniq)}")
    return "\n".join(lines)


def fig_extract_data(src):
    """The generated code must declare its plotted values in a module-
    level literal:  DATA = {"series label": {"values": [...],
    "source": "E4"}, ...}  (empty dict for conceptual figures). Returns
    (data_dict or None, error)."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return None, f"syntax error: {e}"
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and \
                node.targets[0].id == "DATA":
            try:
                val = ast.literal_eval(node.value)
            except Exception as e:
                return None, f"DATA is not a plain literal: {e}"
            if not isinstance(val, dict):
                return None, "DATA must be a dict"
            return val, ""
    return None, "no module-level DATA = {...} declaration"


def _fig_walk_numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("source", "unit", "label", "note", "categories",
                     "conditions", "n", "file", "x_col", "y_col", "columns"):
                continue
            yield from _fig_walk_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _fig_walk_numbers(v)


def fig_data_numbers(data):
    return {_fig_num_key(v) for v in _fig_walk_numbers(data or {})}


def fig_data_audit(data, pool_numbers, known_ids):
    """Every plotted value must exist in the sources (exact match after
    canonicalisation - the 0.5 % tolerance is only used to phrase the
    diagnostic) and every series must cite a known source ID. Returns a
    list of problem strings (empty = OK)."""
    problems = []
    if not data:
        return problems
    pool_keys = {_fig_num_key(p) for p in pool_numbers}
    pool_vals = []
    for p in pool_numbers:
        try:
            pool_vals.append(float(p))
        except (TypeError, ValueError):
            pass
    for label, series in data.items():
        if not isinstance(series, dict):
            problems.append(f"series '{label}' must be a dict with values "
                            "and source")
            continue
        src = str(series.get("source", "")).strip()
        ids = [s.strip() for s in re.split(r"[,;]\s*|\s+(?![^:]*:)", src) if s.strip()]
        if not ids:
            problems.append(f"series '{label}' has no source ID")
        else:
            bad = [i for i in ids if i.strip("[]") not in known_ids
                   and i not in known_ids]
            if bad:
                problems.append(f"series '{label}' cites unknown source "
                                f"ID(s) {bad}")
        vals = series.get("values", [])
        conds = series.get("conditions")
        if isinstance(vals, list) and isinstance(conds, list) and vals and \
                len(conds) != len(vals):
            problems.append(f"series '{label}': 'conditions' must have one "
                            "entry per value (empty string if the source "
                            "states none)")
        for v in _fig_walk_numbers(series):
            if abs(v) < 1e-12:
                continue
            if _fig_num_key(v) in pool_keys:
                continue
            near = [pv for pv in pool_vals
                    if abs(pv - v) <= max(abs(v) * 0.005, 1e-9)]
            hint = (f" (closest source value: {near[0]:g} - copy it verbatim)"
                    if near else "")
            problems.append(f"series '{label}': value {v:g} is not in any "
                            f"source{hint}")
    return problems


def fig_code_uses_data(src):
    """Data figures must draw from DATA or TABLES (not from other literals)."""
    body = src.split("def draw", 1)[-1]
    return bool(re.search(r"\bDATA\b|\bTABLES\b", body))


def _fig_free_number(v):
    """Numbers that need no source: ordinals/panel indices (0-12) and
    years (1900-2100)."""
    return float(v).is_integer() and (0 <= v <= 12 or 1900 <= v <= 2100)


def fig_canvas_audit(harvest, data, is_data, extra_numbers=None):
    """Compare what actually reached the canvas with the declared DATA
    (plus the numeric cells of uploaded tables, when the figure is
    file-backed). Data figures: every plotted y value (and x value,
    unless positional) must be declared; long series look like computed
    curves unless they come from a table. Conceptual figures: no
    quantitative axes, no error bars, no unsourced numbers in text."""
    probs = []
    if not harvest:
        return probs
    dnums = fig_data_numbers(data) | set(extra_numbers or ())
    file_backed = bool(extra_numbers)
    if is_data:
        bad = [v for v in harvest.get("vals_y", [])
               if abs(v) > 1e-9 and _fig_num_key(v) not in dnums
               and not (float(v).is_integer() and 0 <= v <= 30)]
        if bad:
            probs.append("plotted values not declared in DATA"
                         + (" or present in the uploaded tables" if file_backed else "")
                         + ": " + ", ".join(f"{v:g}" for v in bad[:8])
                         + " - plot only source values; remove fits, trend "
                           "lines, means and guide curves")
        badx = [v for v in harvest.get("vals_x", [])
                if abs(v) > 1e-9 and _fig_num_key(v) not in dnums
                and not (float(v).is_integer() and 0 <= v <= 30)]
        if badx:
            probs.append("x values not declared in DATA"
                         + (" or present in the uploaded tables" if file_backed else "")
                         + ": " + ", ".join(f"{v:g}" for v in badx[:8]))
        if harvest.get("long_series") and not file_backed:
            probs.append(f"{harvest['long_series']} series with more than 30 "
                         "points - looks like a computed curve; plot the "
                         "discrete source values only")
    else:
        if harvest.get("quant_axes"):
            probs.append("conceptual figure has a quantitative axis (numeric "
                         "tick labels) - use ax.set_axis_off() and ordered, "
                         "unscaled elements")
        if harvest.get("n_errorbars"):
            probs.append("conceptual figure has error bars")
    for tok in harvest.get("text_nums", []):
        try:
            v = float(tok)
        except ValueError:
            continue
        if _fig_free_number(v) or _fig_num_key(v) in dnums:
            continue
        probs.append(f"number '{tok}' written on the figure is not declared "
                     "in DATA with a source ID - remove it or declare it")
    return sorted(set(probs))[:10]


def fig_layout_problems(audit):
    """Mechanical layout lint -> list of repair instructions."""
    out = []
    for o in (audit or {}).get("overlaps", [])[:12]:
        out.append(f"text '{o['a']}' overlaps text '{o['b']}' "
                   f"({int(o['frac'] * 100)} %) - move the satellite or shorten")
    for c in (audit or {}).get("clipped", [])[:8]:
        out.append(f"text '{c['text']}' runs outside the figure - pull it "
                   "inside the margin")
    for s in (audit or {}).get("small_text", [])[:8]:
        out.append(f"text '{s['text']}' is {s['pt']} pt (< 5.5 pt) - use 7-7.5 pt")
    for i in (audit or {}).get("empty_axes", [])[:4]:
        out.append(f"axes #{i} drew nothing - remove it or draw the panel")
    for t in (audit or {}).get("text_marker", [])[:8]:
        out.append(f"label '{t['text']}' sits on {t['marks']} data mark(s) - "
                   "offset it")
    return out


def fig_worst_overlap(audit):
    return max([o.get("frac", 0) for o in (audit or {}).get("overlaps", [])] or [0])


def fig_sanitize_alt(caption):
    """Alt text of the markdown image: one line, and never the sequence
    '](' (the only thing that could end the alt early). Brackets stay, so
    citations '[4]' and '[AUTHOR: ...]' markers in captions survive; the
    image regexes are non-greedy and anchored on the file extension."""
    c = re.sub(r"\s+", " ", caption or "").strip()
    return c.replace("](", "] (")


def _fig_err_summary(err, limit=110):
    """One line for logs and placeholder notes: the exception message for
    tracebacks, otherwise the first problem line."""
    elines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    if not elines:
        return (err or "").strip()[:limit]
    if 'File "<figure>"' in err or "Error" in elines[-1]:
        first = elines[-1]
    else:
        first = next((l for l in elines if not l.endswith(":")), elines[0])
    return first.lstrip("- ")[:limit]


def fig_insert_markdown(section_md, fig_number, caption, png_path):
    """Place the figure block right after the section heading (or at a
    '[FIGURE n]' anchor if the writer left one). Idempotent: a section
    that already embeds this PNG is returned unchanged."""
    if png_path and str(png_path) in section_md:
        return section_md
    block = f"\n\n![{fig_sanitize_alt(caption)}]({png_path})\n\n"
    anchor = re.compile(rf"\[FIGURE\s*{fig_number}\]", re.I)
    if anchor.search(section_md):
        return anchor.sub(block.strip(), section_md, count=1)
    lines = section_md.split("\n")
    # after the first paragraph following the heading
    out, placed, seen_para = [], False, False
    for i, ln in enumerate(lines):
        out.append(ln)
        if placed:
            continue
        if ln.strip() and not ln.startswith("#"):
            seen_para = True
        if seen_para and (i + 1 == len(lines) or not lines[i + 1].strip()):
            out.append(block.rstrip("\n"))
            placed = True
    if not placed:
        out.append(block)
    return "\n".join(out)


def fig_replace_caption(md, png_path, new_caption):
    """Swap the alt text of the image line that embeds png_path."""
    pat = re.compile(r"!\[.*?\]\(" + re.escape(str(png_path)) + r"\)")
    return pat.sub(f"![{fig_sanitize_alt(new_caption)}]({png_path})", md)


FIG_HEDGE_RE = re.compile(r"\b(proposed|hypothes\w*|putative|inferred|"
                          r"speculat\w*|tentative)\b", re.I)


def fig_fix_caption(caption, lead, spec, data, pool_numbers):
    """Mechanical caption rules: journal lead with the engine number,
    epistemic hedge for INDIRECT/SPECULATIVE mechanisms, 'not to scale'
    for stacks and ladders, and an author marker for any caption number
    that is neither in DATA nor in the sources."""
    cap = (caption or "").strip()
    cap = re.sub(r"^\*\*\s*(?:Proposed\s+)?(?:Figure|Fig\.?)\s*[A-Za-z]?\d*\s*"
                 r"[.|:]?\s*\**\s*", "", cap)
    if cap.startswith("**"):
        cap = cap[2:]
    body = cap                      # hedge check on the body, not the label
    cap = f"**{lead}{cap}"
    if "**" not in cap[2:]:
        cap = cap + "**"
    status = str((spec.get("epistemic") or {}).get("status", "")).upper()
    if status in ("INDIRECT", "SPECULATIVE") and not FIG_HEDGE_RE.search(body):
        ev = ", ".join((spec.get("epistemic") or {}).get("evidence_ids") or [])
        cap += (" Proposed relationship" + (f" inferred from {ev}" if ev else "")
                + "; not directly evidenced in the cited sources.")
    kind = spec.get("kind", "")
    if kind in ("device_stack", "energy_diagram", "schematic", "mechanism",
                "architecture") and "not to scale" not in cap.lower():
        cap += " Schematic, not to scale."
    dnums = fig_data_numbers(data) | {_fig_num_key(p) for p in pool_numbers}
    unknown = []
    for m in RW_NUM_RE.finditer(re.sub(r"\[[^\]]*\]|\b[A-Z]{1,2}-?\d+\b", " ", cap)):
        tok = _rw_norm_num(m.group(0))
        try:
            v = float(tok)
        except ValueError:
            continue
        if _fig_free_number(v) or _fig_num_key(v) in dnums:
            continue
        unknown.append(m.group(0).strip())
    if unknown:
        cap += (" [AUTHOR: caption value(s) " + ", ".join(sorted(set(unknown))[:5])
                + " not found in the sources - verify or remove]")
    return cap


def fig_make_placeholder(job_sig, number, title, note, width, width_mm=None):
    png, svg, pyp = fig_paths(job_sig, number)
    src = fig_placeholder_source(title, note)
    res = fig_render(src, png, svg, width=width, width_mm=width_mm,
                     height_mm=round((width_mm or FIG_WIDTHS_MM.get(width, 183.0)) * 0.5, 1),
                     meta_desc=f"PLACEHOLDER - {note}", pdf_path=png.with_suffix(".pdf"))
    return str(png) if res.get("ok") else ""


def fig_make_one(spec, job_sig, number, pool_numbers, known_ids, api_key,
                 model, width, log=None, max_repairs=2, vision=True,
                 jstyle=None, lead=None):
    """Code gen -> static gate -> DATA provenance audit -> sandbox render
    -> canvas audit (what reached the canvas vs DATA / tables) -> layout
    lint; every failure is one repair (shared budget max_repairs) ->
    optional visual QA (one repair). Returns a dict: ok, png, svg, pdf,
    code, caption, issues, attempts, audit, qa_mode, sources."""
    jstyle = jstyle or fig_journal_style("")
    width_mm = float(jstyle.get(f"{width}_mm") or FIG_WIDTHS_MM.get(width, 183.0))
    png, svg, pyp = fig_paths(job_sig, number)
    pdf = png.with_suffix(".pdf")
    kind = spec.get("kind", "schematic")
    is_data = kind in FIG_KINDS_DATA
    tables = spec.get("_tables") or {}
    table_nums = fig_tables_numbers(tables) if tables else set()
    pool_all = set(pool_numbers) | table_nums
    lead = lead or f"Figure {number}{jstyle['lead']}"
    sysm = (FIG_CODE_SYSTEM.replace("{WIDTH}", width)
            .replace("{WIDTH_MM}", f"{width_mm:g}")
            .replace("{HEIGHT_MM}", f"{float(spec.get('height_mm') or round(width_mm * 0.62)):g}")
            .replace("{PANEL_STYLE}", jstyle["panel"])
            .replace("{CAPTION_LEAD}", lead))
    umsg = fig_code_user(spec, is_data, width)
    src_ids = ", ".join(sorted({str(d.get("source", "")) for d in (spec.get("data") or [])
                                if isinstance(d, dict) and d.get("source")}))
    meta_desc = (f"GrapheAI figure {number} ({kind}); "
                 + (f"sources: {src_ids}" if src_ids else "conceptual; no data plotted"))
    attempts, code, err, history = 0, "", "", ""
    caption, data, audit, harvest, versions = spec.get("caption", ""), {}, {}, {}, {}
    lint, rendered, h_used = [], False, float(spec.get("height_mm") or 0)
    for attempt in range(max_repairs + 1):
        attempts += 1
        rendered = False
        with _fig_effort("high"):
            raw = rw_call(sysm, umsg + history, 7000)
        code, cap = fig_parse_code(raw)
        if cap:
            caption = cap
        problems = fig_validate_source(code, data_fig=is_data)
        data = {}
        if not problems:
            data, derr = fig_extract_data(code)
            if data is None:
                problems.append(derr)
                data = {}
            else:
                problems += fig_data_audit(data, pool_all, known_ids)
                if is_data and not fig_code_uses_data(code):
                    problems.append("draw() must plot from DATA (or TABLES)")
        lint = []
        if problems:
            err = "REJECTED:\n- " + "\n- ".join(problems)
        else:
            res = fig_render(code, png, svg, width=width, width_mm=width_mm,
                             height_mm=spec.get("height_mm"), meta_desc=meta_desc,
                             data_fig=is_data, pdf_path=pdf, tables=tables)
            if res.get("ok"):
                rendered = True
                audit, harvest = res.get("audit", {}), res.get("harvest", {})
                versions, h_used = res.get("versions", {}), res.get("height_mm", h_used)
                canvas = fig_canvas_audit(harvest, data, is_data, table_nums)
                lint = fig_layout_problems(audit)
                if canvas:
                    err = "INTEGRITY AUDIT (what reached the canvas):\n- " + "\n- ".join(canvas)
                elif lint:
                    err = "LAYOUT AUDIT (rendered, but must be fixed):\n- " + "\n- ".join(lint)
                else:
                    err = ""
                    break
            else:
                err = res.get("error", "render failed")
        if log:
            log(f"figure {number}: attempt {attempt + 1} - {_fig_err_summary(err)}")
        history = ("\n\n=== REPAIR REQUEST ===\nYour previous module failed. Return the "
                   "COMPLETE corrected module and caption (not a patch).\nFAILURE:\n"
                   + err[:3000] + "\n\nPREVIOUS MODULE:\n```python\n" + code[:12000]
                   + "\n```")
    # accept only a clean render, or a render whose sole remaining fault is
    # layout lint (integrity failures are never published)
    if not rendered or (err and not err.startswith("LAYOUT")):
        return {"ok": False, "error": err, "code": code, "attempts": attempts,
                "caption": caption, "audit": audit}
    issues = list(lint)
    if lint and not is_data and fig_worst_overlap(audit) >= 0.4:
        # an overlapping schematic is worse than a labelled gap
        return {"ok": False, "error": "LAYOUT: severe text overlap remained after "
                f"{max_repairs} repairs - " + "; ".join(lint[:3]),
                "code": code, "attempts": attempts, "caption": caption,
                "audit": audit}
    pyp.write_text(code, encoding="utf-8")
    qa_mode = "skipped"
    is_max = st.session_state.get("backend") == "max"
    if vision and lint:
        qa_mode = "skipped (layout lint remains)"
    elif vision and is_max and not fig_max_vision_ok(model):
        qa_mode = "blind (Claude Code could not read images in the probe)"
    elif vision:
        try:
            qa_png = fig_downscale_for_qa(png)
            with _fig_effort("medium"):
                verdict_raw = call_claude_vision(
                    api_key, FIG_CRITIC_SYSTEM,
                    f"FIGURE {number}  KIND: {kind}  CLASS: {'data' if is_data else 'conceptual'}"
                    f"  SIZE: {width_mm:g} x {h_used:g} mm\n"
                    f"EPISTEMIC STATUS: {_rwjson.dumps(spec.get('epistemic') or {})}\n"
                    f"MUST NOT IMPLY: {_rwjson.dumps(spec.get('must_not_imply') or [])}\n\n"
                    f"BRIEF:\n{_rwjson.dumps({k: spec.get(k) for k in ('title', 'claim', 'purpose', 'panels', 'brief') if spec.get(k)}, indent=1)[:6000]}\n\n"
                    f"DATA DECLARED BY THE CODE:\n{_rwjson.dumps(data, indent=1)[:3000]}\n\n"
                    + (f"FILE-BACKED TABLES USED: {', '.join(tables)}\n\n" if tables else "")
                    + f"CAPTION:\n{caption}\n\nRENDERER AUDIT:\n{_rwjson.dumps(audit)[:1500]}\n\n"
                    f"CODE:\n{code[:6000]}",
                    qa_png, model, max_tokens=2500)
            verdict = _rw_json_block(verdict_raw) or {}
            qa_mode = "visual (Claude Code Read)" if is_max else "visual"
            integrity = [str(x) for x in (verdict.get("integrity") or [])]
            issues += [str(x) for x in (verdict.get("issues") or [])]
            fix = str(verdict.get("fix_instructions") or "").strip()
            cap_fix = str(verdict.get("caption_fix") or "").strip()
            if cap_fix:
                caption = cap_fix
            if (not verdict.get("pass", True) or integrity) and (fix or integrity):
                with _fig_effort("high"):
                    raw = rw_call(sysm, umsg + "\n\n=== VISUAL REVIEW ===\nThe art "
                                  "editor reviewed your rendered figure.\n"
                                  + ("INTEGRITY PROBLEMS (remove the offending "
                                     "element; never add a value or a source):\n- "
                                     + "\n- ".join(integrity) + "\n" if integrity else "")
                                  + ("ISSUES:\n- " + "\n- ".join(issues) + "\n" if issues else "")
                                  + f"FIX: {fix}\n\nReturn the complete corrected "
                                  f"module and caption.\n\nPREVIOUS MODULE:\n```python\n"
                                  f"{code[:12000]}\n```", 7000)
                code2, cap2 = fig_parse_code(raw)
                probs2 = fig_validate_source(code2, data_fig=is_data)
                d2 = {}
                if not probs2:
                    d2, derr2 = fig_extract_data(code2)
                    if d2 is None:
                        probs2.append(derr2)
                        d2 = {}
                    else:
                        probs2 += fig_data_audit(d2, pool_all, known_ids)
                if not probs2:
                    res2 = fig_render(code2, png, svg, width=width, width_mm=width_mm,
                                      height_mm=spec.get("height_mm"),
                                      meta_desc=meta_desc, data_fig=is_data,
                                      pdf_path=pdf, tables=tables)
                    if res2.get("ok") and not fig_canvas_audit(
                            res2.get("harvest", {}), d2, is_data, table_nums):
                        code, data = code2, d2
                        caption = cap2 or caption
                        audit = res2.get("audit", {})
                        pyp.write_text(code, encoding="utf-8")
                        issues = [f"(fixed) {i}" for i in issues]
                    else:
                        # keep the reviewed version on disk
                        fig_render(code, png, svg, width=width, width_mm=width_mm,
                                   height_mm=spec.get("height_mm"),
                                   meta_desc=meta_desc, data_fig=is_data,
                                   pdf_path=pdf, tables=tables)
        except Exception as e:
            qa_mode = "skipped"
            issues.append(f"visual QA skipped: {str(e)[:80]}")
    caption = fig_fix_caption(caption, lead, spec, data, pool_all)
    sidecar = {"number": number, "kind": kind, "class": "data" if is_data else "conceptual",
               "title": spec.get("title", ""), "claim": spec.get("claim", ""),
               "sources": src_ids, "data": data, "tables": list(tables),
               "caption": caption, "epistemic": spec.get("epistemic") or {},
               "must_not_imply": spec.get("must_not_imply") or [],
               "attempts": attempts, "qa_mode": qa_mode, "issues": issues,
               "audit": audit, "versions": versions,
               "width_mm": width_mm, "journal_style": jstyle.get("key"),
               "feedback": spec.get("feedback", "")}
    try:
        pyp.with_suffix(".json").write_text(_rwjson.dumps(sidecar, indent=1, default=str),
                                            encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "png": str(png), "svg": str(svg), "pdf": str(pdf),
            "code": code, "caption": caption, "issues": issues,
            "attempts": attempts, "audit": audit, "qa_mode": qa_mode,
            "sources": src_ids, "data": data}


def fig_parse_code(raw):
    """Model output: a ```python block with the code and, optionally, a
    <<<CAPTION>>> ... <<<END CAPTION>>> block."""
    m = re.search(r"```python\s*(.*?)```", raw, re.S)
    code = m.group(1).strip() if m else raw.strip()
    cap, _ = _rw_between(raw, "<<<CAPTION>>>", "<<<END CAPTION>>>")
    return code, (cap or "").strip()
# ==========================================================================
# Figure engine - prompts, planner, pipeline integration (checkpoint,
# regeneration, file-backed data), Figure Studio
# ==========================================================================
FIG_PLANNER_SYSTEM = """\
You are the art editor of a top energy-materials journal (Nature Energy / Joule / Energy & Environmental Science) preparing the display items for a manuscript, review or research proposal. You decide which figures the document needs and brief a scientific illustrator so precisely that the drawings come out at journal standard without you in the room. You do not draw and you do not write prose.

THE AUTHORS' STANDING RULE: whenever the text mentions, or would be understood faster with, a figure, schematic, conceptual model, mechanism, comparison, workflow, architecture, roadmap, taxonomy, structure-property relationship, process or graphical summary, a real figure is produced - never a bare "Figure X" placeholder. A figure earns its place when it makes ONE claim faster or clearer than words; write that claim as a sentence (it becomes the bold caption lead). Do not plan figures that merely decorate a paragraph, duplicate an original figure of the manuscript (rewrite mode: the ledger's F-IDs), or show data the sources do not contain. Prefer fewer, stronger figures: at most {MAX_FIGS}, in order of importance.

TWO CLASSES - DIFFERENT RULES
- CONCEPTUAL (kind: schematic | mechanism | workflow | architecture | roadmap | taxonomy | process | conceptual_model | device_stack | energy_diagram | timeline | decision_tree | graphical_abstract): drawn from the document's own argument; every named entity, layer, step or relation must appear in the text or sources. Record the epistemic status of every mechanism or causal link (DIRECT = shown by the sources' own data; INDIRECT = consistent with them; SPECULATIVE = the authors' proposal) - INDIRECT and SPECULATIVE links are drawn as "proposed" (dashed) and hedged in the caption. The only numbers allowed on a conceptual figure are labels copied verbatim from a source and listed under "data" with their source ID (e.g. a layer thickness); nothing else numeric.
- DATA (kind: comparison | structure_property | trend | data_chart): every plotted value MUST exist in the SOURCE INVENTORY and is copied VERBATIM with unit, uncertainty, n and measurement conditions, plus its source ID. Never round, convert, average, normalise, interpolate, extrapolate or add a value from memory. Discrete literature values are plotted as discrete marks - never joined into a curve or fitted - unless the source itself reports the curve. Minimum for a comparison panel: three comparable values from at least two sources; two values are a sentence, not a figure. "conditions" is REQUIRED for every value (empty string if the source states none); when conditions differ between points the figure groups by condition and the caption says "not directly comparable". If the text would benefit from a data figure but the inventory lacks the numbers, plan the CONCEPTUAL version of the idea instead (the mechanism, the comparison as a qualitative taxonomy or matrix); only when nothing honest can be drawn set "needs_author": true with a precise request for the missing values.
- DATA TABLES (uploaded CSV/XLSX files listed in the inventory) are first-class sources and the strongest basis for a data figure: measured sweeps, time series and device statistics can be plotted column by column. Refer to a table by its source ID FILE:<table name>; in "data" give {"label": ..., "file": "<table name>", "x_col": "<column>", "y_col": "<column>", "values": [], "unit": "...", "conditions": ["as stated in the file or text"], "source": "FILE:<table name>"}. The illustrator receives the columns directly, so do not type the values. Prefer promoting a real measurement from the SI tables into the main text over a literature comparison when both are possible.

THE ILLUSTRATOR'S BRIEF - what makes the drawing professional
For each figure give: the claim; the panels (letters; at most 4 in a single column, 6 in a double), one sentence each; the spine - the 3-6 primary elements in reading order (left to right, top to bottom; cause before effect; time to the right; energy vertical; device stacks in physical orientation with the substrate at the bottom); the satellites (secondary annotations, at most 5); the colour roles (perovskite, etl, htl, electrode, tco, glass, ion_pos, ion_neg, defect, light, accent - accent is reserved for the ONE new thing and is never used for a defect; the same entity keeps the same role in every figure of the document); the arrow semantics (flow | cause | transport | equilibrium | inhibit | light | proposed); the text budget (words on the figure: <= 40 single column, <= 90 double); what must NOT be drawn; what the figure must NOT imply (e.g. "energy levels to scale", "a trend between categories", "comparable conditions", "that the experiment was done"); and the exact label strings, copied from the text. Width "single" unless the figure has >= 3 panels or is a roadmap/workflow with >= 5 columns; height_mm 45-120 (single) or 50-170 (double).

JOURNAL CONVENTIONS: {JOURNAL_CONVENTIONS}

OUTPUT: ONLY a JSON object (no prose before or after):
{"figures": [
  {"number": 1, "kind": "mechanism", "class": "conceptual", "section_id": "S3",
   "title": "short title phrase", "claim": "one sentence stating what the figure shows",
   "purpose": "what the reader learns", "width": "single", "height_mm": 70,
   "panels": [{"id": "a", "title": "Without LiF", "brief": "what this panel shows",
               "spine": ["FAPbI3 absorber", "spiro-OMeTAD HTL", "I- migration"],
               "satellites": ["interfacial recombination note"],
               "arrows": ["I- : perovskite -> HTL : transport"],
               "colour_roles": {"FAPbI3": "perovskite", "LiF": "accent"},
               "text_budget_words": 20, "do_not_draw": ["efficiency values"]}],
   "brief": "the complete drawing brief in prose: layout, every element and its label, reading order, emphasis",
   "data": [], "must_not_imply": ["energy levels to scale"],
   "epistemic": {"status": "INDIRECT", "evidence_ids": ["C2", "E7"]},
   "needs_author": false, "author_note": ""},
  {"number": 2, "kind": "comparison", "class": "data", "section_id": "R4",
   "title": "...", "claim": "...", "purpose": "...", "width": "single", "height_mm": 60,
   "panels": [{"id": "a", "title": "T80 across HTL classes", "brief": "dot plot; grouped by condition; direct labels; source tag per point", "x_label": "HTL", "y_label": "T80 (h)"}],
   "brief": "...",
   "data": [{"label": "T80", "categories": ["PTAA", "spiro-OMeTAD"], "values": [1000, 500], "unit": "h",
             "uncertainty": [null, null], "n": [null, null],
             "conditions": ["ISOS-L-2, 65 C", "ISOS-L-2, 65 C"], "source": "[4], [7]"}],
   "must_not_imply": ["a trend between categories"],
   "epistemic": {"status": "DIRECT", "evidence_ids": ["[4]", "[7]"]},
   "needs_author": false, "author_note": ""}
]}
Rules: numbers 1..n in document order; "class" is "conceptual" or "data"; every value of a data figure is verbatim from a source whose ID is in "source" (comma-separated IDs allowed; FILE:<name> for uploaded tables); "conditions" has one entry per value; conceptual figures have "data": [] unless they carry source-labelled numbers; section_id must be one of the section ids given."""

FIG_CODE_SYSTEM = """\
You are a senior scientific illustrator who composes and then draws ONE journal figure in matplotlib, for a harness that owns everything except the drawing. Figure size: {WIDTH} column, {WIDTH_MM} x {HEIGHT_MM} mm at final print size - every size decision below is at that scale.

STEP 1 - COMPOSE BEFORE YOU CODE (write it as a comment block at the top of the module)
Lay every panel out on a 100 x H grid (H = 100 x panel height / panel width, y upward) with 6-unit margins, and list each element with its coordinates, size, text and colour role. One claim per figure; reading order left to right, top to bottom; cause left/top, effect right/bottom; time to the right; energy vertical; device stacks in physical orientation with the substrate at the bottom and "not to scale" written on the panel. Three visual weights at most: spine elements (bold outline 1.2 pt, role fill), satellites (thin outline 0.5 pt, muted fill), annotations (text with a thin leader). Before/after panels share identical geometry; only the changing species moves or changes colour. Typical node in a single column: 18-26 units wide, 9-12 high. Nothing touches the frame; no text overlaps other text, marks, arrowheads or box edges; a label goes beside a shape when the shape is narrower than the word; at most 6 words inside a shape. Text budget: <= 40 words (single) / <= 90 (double).

STEP 2 - THE CONTRACT (checked by a static gate and a sandbox; every violation is reported and the module is rejected)
- Define `def draw(fig, plt, np, mpl):` and draw on `fig`, which already has the right size, dpi and style card (constrained layout is on, so labels outside the axes are accommodated). Create panels with fig.add_subplot / fig.add_gridspec / fig.add_axes. Never create figures, resize, call savefig/show/tight_layout/subplots_adjust, or touch rcParams, fonts or dpi.
- `np`, `mpl` and `plt` are vetted namespaces: np has the usual array and maths functions; mpl exposes patches, lines, path, colors, ticker, transforms, patheffects, gridspec, cm, text, table, collections, markers; plt exposes cm, get_cmap, Circle, Rectangle, Polygon, Line2D, Normalize, MaxNLocator, FuncFormatter. Imports allowed: numpy, math, matplotlib and those sub-modules only. No os/sys/pathlib/open/eval/getattr/str.format, no attribute starting with an underscore, no classes, decorators, generators or while loops; f-strings for text.
- Declare a module-level literal `DATA = {...}` (a plain literal, no expressions). DATA figures: every plotted value with its unit, conditions and source ID exactly as briefed, e.g. DATA = {"T80": {"values": [1000, 500], "unit": "h", "categories": ["PTAA", "spiro-OMeTAD"], "conditions": ["ISOS-L-2, 65 C", "ISOS-L-2, 65 C"], "source": "[4], [7]"}}; draw() plots ONLY from DATA - never type a measured value as a literal, never compute means, fits, trend lines, interpolations or ratios (np.mean/polyfit/linspace/diff are refused in data figures); discrete values stay discrete marks (no connecting lines unless the brief says the source reports the curve); error bars only from an "uncertainty" list in DATA. Numeric literals are for coordinates, sizes and axis limits only. The harness harvests every number that reaches the canvas and rejects the figure if one is not in DATA.
- FILE-BACKED DATA: when the brief lists uploaded data tables, the read-only global `TABLES` holds them as {table name: {"columns": [...], "data": {column: [values]}}} (None where a cell is blank - filter it out). Plot columns directly, e.g. x = [v for v in TABLES["jv.csv"]["data"]["Voltage (V)"] if v is not None]; declare each used column pair in DATA as {"label": {"file": "jv.csv", "x_col": "Voltage (V)", "y_col": "Current density (mA cm-2)", "values": [], "unit": "...", "source": "FILE:jv.csv"}}. A measured sweep or time series from a file may be drawn as a line (it is the source's own curve); still no fits, smoothing or derived quantities.
- CONCEPTUAL figures: DATA = {} unless the brief lists source-labelled numbers (then declare them in DATA with their source and use them only as labels). No quantitative axes (ax.set_axis_off(); ax.set_xlim(0, 100); ax.set_ylim(0, H)), no error bars, no plotted values, nothing drawn to an apparent scale unless labelled "not to scale". Any number written on a conceptual panel that is not in DATA gets the figure rejected (ordinals 0-12 and years are fine).

STEP 3 - STYLE CARD (pre-applied: 7.5 pt sans-serif body, 7 pt ticks/legend, thin spines, no top/right spines, Okabe-Ito data cycle; you use these)
- Panel labels: {PANEL_STYLE}, 8 pt bold, top-left outside each panel: ax.text(-0.08, 1.03, "a", transform=ax.transAxes, fontsize=8, fontweight="bold", va="bottom") - adjust the x offset so it never collides with the y-axis label; omit for a single-panel figure.
- Semantic colours (one entity, one colour, in every figure of the document - obey the DOCUMENT COLOUR LEDGER when given): perovskite absorber "#8C4A2F" (dark brick); etl "#56B4E9" (sky); htl "#E69F00" (orange); electrode "#C9A227" (gold); tco "#7FB7BE" (teal); glass/substrate "#D9D9D9"; ion_pos "#009E73" (green); ion_neg "#CC79A7" (purple); defect "#D55E00" (vermilion); light "#F0E442" (yellow); accent "#0072B2" (blue) for the ONE new thing, used at most three times and never for a defect; generic node fill "#EEEEEE" with edge "#4D4D4D"; emphasised node fill "#DCEBF7" with edge "#0072B2"; muted "#D9D9D9" with edge "#B3B3B3"; ink "#1A1A1A". Data series: "#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9", "#808080". No gradients, shadows, 3D, icons, emoji or clip-art.
- Arrows via mpl.patches.FancyArrowPatch, one style per meaning: flow (process step) arrowstyle "-|>", mutation_scale=8, solid; cause "-|>", mutation_scale=11, linewidth=1.2; transport/migration "-|>" dashed (linestyle=(0, (3, 2))); equilibrium "<|-|>"; inhibit "-[" ; light: yellow "-|>" arriving from outside the stack. A mechanism or link whose epistemic status is INDIRECT or SPECULATIVE is drawn as a PROPOSED arrow: dashed grey "#4D4D4D" with the word "proposed" in 7 pt beside it. Arrowheads stay clear of text.
- Boxes: mpl.patches.FancyBboxPatch(boxstyle="round,pad=0.4") in grid units; text centred inside at 7.5 pt (7 pt for satellites); break lines with "\\n" so no line exceeds the box width; formulae and units in mathtext ("J$_{sc}$", "mA cm$^{-2}$", "I$^-$", "FAPbI$_3$"); never unicode superscripts or subscripts.
- Device stacks: mpl.patches.Rectangle layers of equal width, substrate at the bottom, layer names to the right of the stack (never inside a thin layer), "not to scale" in 7 pt grey at the bottom.
- Energy diagrams: horizontal level lines; a qualitative ladder labelled "Energy vs vacuum (not to scale)" unless every level value is in DATA with a source - never half-quantitative.
- Roadmaps: lanes (rows) x periods (columns) from the brief only; milestones as diamonds; 3-5-word labels. Taxonomies: at most two levels visible in a single column, leaves aligned, family colour at level 1. Workflows: equal-sized step boxes on one baseline, flow arrows, the emphasised step in the emph fill. Graphical abstract: one panel, problem -> intervention -> outcome in one sweep, <= 12 words, no axes.
- Data panels: dot plot for <= 12 categories (horizontal bars when labels are long; scatter for structure-property; a line only for a genuinely ordered trend the source reports or a measured file curve); when "conditions" differ between points, group by condition and print the condition tag beside each group - never order by value across differing conditions; a source tag ("[4]", "E7" or the file name) in 7 pt grey beside every point or curve; direct labels instead of a legend when <= 5 series (frameless legend otherwise); axis labels with units as "T80 (h)"; y from zero for bars, tight for dots; ax.margins(x=0.15) so labels fit; no gridlines, dual axes, pies or 3D.

OUTPUT
```python
<the complete module: layout comment block, DATA literal, helpers, draw()>
```
<<<CAPTION>>>
**{CAPTION_LEAD}Bold claim phrase.** a, what panel a shows (present tense). b, ... For data figures end with "Values as reported in <source IDs>; conditions differ where noted." (write "not directly comparable" when conditions differ; for file data: "Data from <file name> as uploaded by the authors."). For a mechanism with INDIRECT/SPECULATIVE status write "Proposed pathway inferred from <evidence IDs>." Add "Schematic, not to scale." when a stack, geometry or energy ladder is drawn. No number that is not in DATA; no citation not in the sources.
<<<END CAPTION>>>"""

FIG_CRITIC_SYSTEM = """\
You are the art editor of Nature Energy reviewing a rendered figure proof (image attached) at final print size, together with its BRIEF, the DATA declared by the code, the CAPTION and the renderer's mechanical AUDIT. Judge in this order and be concrete (panel, element, fix):
1. INTEGRITY (fatal): a data panel shows a value, curve, fit, trend line, error bar or annotation that is not in DATA (or, for file-backed figures, not in the named table), or joins discrete literature points into a line; a category, legend or axis label states a condition (stabilised, certified, n, area, protocol, temperature) that the DATA conditions do not state; a conceptual panel has a quantitative axis, error bars or a number not in DATA, draws levels, thicknesses or proportions to an apparent scale without "not to scale", or shows an INDIRECT/SPECULATIVE mechanism as an established solid cause arrow; anything under MUST NOT IMPLY is implied; entity names differ from the document's terminology.
2. FIDELITY: every panel and labelled element of the brief is present; reading order and spine as briefed; the one claim is legible from the image alone in five seconds.
3. LEGIBILITY at print size: overlapping or clipped text, text inside shapes narrower than the word, labels crossing marks or arrowheads, text smaller than ~6 pt, unicode superscripts, missing units, more than two text sizes.
4. LAYOUT AND COLOUR: elements touching the frame, empty space larger than a third of a panel, more than three visual weights, box-in-box nesting, semantic colour inconsistencies, accent used for more than one thing, chartjunk (gradients, 3D, shadows), a legend where direct labels would do.
5. CAPTION: bold claim lead, one clause per panel, attribution sentence with source IDs for data, "proposed" hedge for INDIRECT/SPECULATIVE, "not to scale" where needed, no number absent from DATA.
Respond with ONLY a JSON object: {"pass": true|false, "score": 1-10, "integrity": ["fatal problems; empty list if none"], "issues": ["specific positional problems"], "fix_instructions": "exact code-level changes the illustrator applies without judgement: which element, which coordinate/size/text/colour/arrow style; for integrity problems: remove the element - never add a value or a source", "caption_fix": "corrected caption, or empty string"}. pass=false for any integrity problem, overlap, clipping, missing panel or missing hedge."""


def fig_code_user(spec, is_data, width):
    keep = ("number", "kind", "class", "title", "claim", "purpose", "width",
            "height_mm", "panels", "brief", "must_not_imply", "epistemic",
            "labels_from_sources", "colour_ledger", "feedback")
    brief = {k: spec.get(k) for k in keep if spec.get(k) not in (None, "", [], {})}
    data_txt = ""
    if is_data or spec.get("data"):
        data_txt = ("\n\nDATA TO PLOT (exact values; each series must appear "
                    "in DATA with its unit, conditions and source):\n"
                    + _rwjson.dumps(spec.get("data", []), indent=1))
    tables = spec.get("_tables") or {}
    tab_txt = ""
    if tables:
        tab_txt = ("\n\nUPLOADED DATA TABLES available as TABLES[name] (plot the "
                   "listed columns directly; declare them in DATA with source "
                   "FILE:<name>):\n" + fig_tables_schema(tables))
    return (f"FIGURE {spec.get('number')} - kind: {spec.get('kind')} - class: "
            f"{'data' if is_data else 'conceptual'} - width: {width} column\n\n"
            f"ART EDITOR'S BRIEF:\n{_rwjson.dumps(brief, indent=1)[:12000]}"
            f"{data_txt}{tab_txt}\n\nCompose, then write the module and the caption now.")


def fig_state_tables(state):
    """TABLES pack from the pipeline inputs (rewrite mode uploads)."""
    tabs = (state.get("inputs") or {}).get("tables") or []
    return fig_tables_pack(tabs) if tabs else {}


def fig_colour_ledger(manifest):
    """entity -> colour role across all planned figures (first assignment
    wins), so every figure of the document uses the same colour for the
    same thing."""
    ledger = {}
    for f in manifest or []:
        for p in f.get("panels") or []:
            for ent, role in (p.get("colour_roles") or {}).items():
                ledger.setdefault(str(ent), str(role))
    return ledger


def fig_source_inventory(mode, state):
    """(inventory_text, pool_numbers, known_ids) for the planner and the
    provenance audit, per pipeline. Uploaded data tables (rewrite mode)
    are added as FILE:<name> sources."""
    stg = state["stages"]
    if mode == "rw":
        ledger = stg.get("ledger", "") + "\n" + stg.get("si_ledger", "")
        inv = "\n".join(ln for ln in ledger.splitlines()
                        if re.match(r"^\s*(S?[EFCMP]|SE|SF|SM)-?\d+\s*\|", ln)
                        or ln.startswith("## "))[:40000]
        pool = set()
        for t in state["inputs"]["evidence_texts"]:
            pool |= fig_numbers_in(t)
        pool |= fig_numbers_in(ledger)
        ids = set(re.findall(r"\b(?:S?[EFCMP]|SE|SF|SM)-?\d+\b", ledger))
        tables = fig_state_tables(state)
        if tables:
            inv += "\n\n## DATA TABLES (uploaded files; source ID FILE:<name>)\n" \
                   + fig_tables_schema(tables)
            pool |= fig_tables_numbers(tables)
            for name in tables:
                ids |= {f"FILE:{name}", name}
        # library passages (author's corpus) in 'compare' mode: [Ln] sources
        libp = stg.get("lib_passages") or {}
        if libp and (state.get("opts") or {}).get("lib_mode") == "compare":
            joined = "\n\n".join(t for t in libp.values() if t)
            inv += ("\n\n## LIBRARY PASSAGES (author's corpus; source IDs [Ln]; "
                    "literature values verbatim with conditions)\n" + joined[:25000])
            pool |= fig_numbers_in(joined)
            for v in (state.get("registry") or {}).values():
                ids |= {f"L{v['n']}", f"[L{v['n']}]"}
        return inv, pool, ids
    # rv / cp: numbered papers + passages
    reg = state.get("registry", {})
    entries = sorted(reg.values(), key=lambda v: v["n"])
    inv = "\n".join(f"[{v['n']}] {v['title']}" for v in entries)[:20000]
    pool = set()
    for txt in stg.get("passages", {}).values():
        pool |= fig_numbers_in(txt)
    ids = {str(v["n"]) for v in entries} | {f"[{v['n']}]" for v in entries}
    return inv, pool, ids


def fig_document_journal(mode, state):
    opts = state.get("opts", {}) or {}
    if mode == "rw":
        return opts.get("journal", "")
    if mode == "rv":
        return opts.get("venue", "")
    return opts.get("journal") or opts.get("venue") or ""


def _fig_save(state, mode):
    (rv_save_state if mode != "rw" else rw_save_state)(state)


def fig_run_stage(state, mode, section_texts, plan_text, api_key, model,
                  step, max_figs=4, vision=True, label_prefix="Figure"):
    """Plan + make figures for a pipeline run. section_texts: ordered list
    of (section_id, heading, markdown). Returns (figures, section_texts
    with figures inserted). If the author asked to approve the figure plan
    first, sets state['status'] = 'awaiting_figures' after planning and
    returns (None, section_texts) - the caller must stop."""
    stg = state["stages"]
    opts = state.get("opts", {}) or {}
    job = state["sig"]
    inv, pool, ids = fig_source_inventory(mode, state)
    jstyle = fig_journal_style(fig_document_journal(mode, state))
    stg["fig_label_prefix"] = label_prefix
    if "fig_manifest" not in stg:
        step("Planning figures...")
        secs_txt = "\n\n".join(f"=== SECTION {sid} | {head} ===\n{md[:6000]}"
                               for sid, head, md in section_texts)
        conv = (f"{jstyle['name'] or 'generic high-impact journal'}: panel labels "
                f"{jstyle['panel']}; single column {jstyle['single_mm']:g} mm, "
                f"double column {jstyle['double_mm']:g} mm"
                + ("; manuscript rewrite - your figures are PROPOSED new display "
                   "items, the original figures keep their numbers" if mode == "rw" else ""))
        umsg = (f"MODE: {mode}\nSECTION IDS: "
                + ", ".join(str(sid) for sid, _, _ in section_texts)
                + f"\n\nPLAN / OUTLINE:\n{plan_text[:15000]}\n\nSECTION TEXTS:\n"
                f"{secs_txt[:60000]}\n\nSOURCE INVENTORY (the only permitted "
                f"values for data figures):\n{inv}\n"
                + (f"\nBINDING EDITS FROM THE AUTHOR TO THE FIGURE PLAN:{opts['fig_plan_edits']}\n"
                   if opts.get("fig_plan_edits") else "")
                + "\nPlan the figures now (JSON only).")
        raw = rw_call(FIG_PLANNER_SYSTEM.replace("{MAX_FIGS}", str(max_figs))
                      .replace("{JOURNAL_CONVENTIONS}", conv), umsg, 6000)
        man = _rw_json_block(raw) or {}
        figs = [f for f in (man.get("figures") or []) if isinstance(f, dict)]
        for i, f in enumerate(figs[:max_figs], 1):
            f["number"] = i
            f.setdefault("kind", "schematic")
            f.setdefault("width", "double")
            f.setdefault("data", [])
            f.setdefault("draw", True)
            if f["width"] not in FIG_WIDTHS_MM:
                f["width"] = "double"
        stg["fig_manifest"] = figs[:max_figs]
        _fig_save(state, mode)
        if opts.get("pause_figs") and not state.get("figs_approved") and figs:
            state["status"] = "awaiting_figures"
            _fig_save(state, mode)
            return None, section_texts
    stg.setdefault("figures", {})
    ledger = fig_colour_ledger(stg["fig_manifest"])
    tables = fig_state_tables(state)
    for f in stg["fig_manifest"]:
        key = str(f["number"])
        if key in stg["figures"]:
            continue
        lead = f"{label_prefix} {f['number']}{jstyle['lead']}"
        width = f.get("width", "double")
        width_mm = jstyle.get(f"{width}_mm")
        if f.get("draw") is False:
            stg["figures"][key] = {"ok": False, "skipped": True, "user_skipped": True,
                                   "caption": "", "spec": f}
            _fig_save(state, mode)
            continue
        if f.get("needs_author"):
            note = (f"[AUTHOR: supply the data for figure {f['number']} "
                    f"({f.get('kind')}) - {f.get('author_note', '')}]")
            ph = fig_make_placeholder(job, f["number"], f.get("title", ""),
                                      f.get("author_note", ""), width, width_mm)
            stg["figures"][key] = {"ok": False, "skipped": True, "placeholder": ph,
                                   "caption": f"**{lead}{f.get('title', 'Placeholder')} "
                                              f"(placeholder).** {note}",
                                   "spec": f}
            _fig_save(state, mode)
            continue
        step(f"Drawing figure {f['number']}/{len(stg['fig_manifest'])} - "
             f"{f.get('kind')}: {f.get('title', '')[:50]}")
        spec = dict(f)
        spec["caption"] = ""
        spec["colour_ledger"] = ledger
        if f.get("author_instructions"):
            spec["brief"] = (f.get("brief", "") + "\n\nINSTRUCTIONS FROM THE AUTHOR "
                             f"(binding): {f['author_instructions']}")
        if tables:
            spec["_tables"] = tables
        res = fig_make_one(spec, job, f["number"], pool, ids, api_key, model,
                           width, log=step, vision=vision, jstyle=jstyle, lead=lead)
        res["spec"] = f
        if res.get("ok") and not res.get("caption"):
            res["caption"] = f"**{lead}{f.get('title', '')}.** {f.get('purpose', '')}"
        if not res.get("ok"):
            first = _fig_err_summary(str(res.get("error", "")), 160)
            note = f"[AUTHOR: figure {f['number']} ({f.get('kind')}) could not be rendered - {first}]"
            res["placeholder"] = fig_make_placeholder(
                job, f["number"], f.get("title", ""),
                f"Could not be rendered after {res.get('attempts', 0)} attempts: {first}",
                width, width_mm)
            res["caption"] = (f"**{lead}{f.get('title', 'Figure')} (placeholder).** "
                              f"{note}")
        stg["figures"][key] = res
        _fig_save(state, mode)
    return [stg["figures"][str(f["number"])] for f in stg["fig_manifest"]], \
        fig_insert_all(stg, section_texts)


def fig_insert_all(stg, section_texts):
    """Insert every rendered figure / placeholder into its section."""
    out = []
    by_sec = {}
    for f in stg["fig_manifest"]:
        by_sec.setdefault(str(f.get("section_id")), []).append(f)
    for sid, head, md in section_texts:
        # each figure goes right after the first paragraph, so insert the
        # highest number first to end up in ascending order
        for f in sorted(by_sec.get(str(sid), []), key=lambda x: -int(x["number"])):
            res = stg["figures"].get(str(f["number"]), {})
            if res.get("ok"):
                md = fig_insert_markdown(md, f["number"], res["caption"], res["png"])
            elif res.get("placeholder"):
                md = fig_insert_markdown(md, f["number"], res["caption"],
                                         res["placeholder"])
            elif res.get("caption") and res["caption"] not in md:
                md = md.rstrip() + "\n\n" + res["caption"] + "\n"
        out.append((sid, head, md))
    known = {str(sid) for sid, _, _ in section_texts}
    orphan = [f for f in stg["fig_manifest"] if str(f.get("section_id")) not in known]
    if orphan and out:
        sid, head, md = out[-1]
        for f in orphan:
            res = stg["figures"].get(str(f["number"]), {})
            img = res.get("png") if res.get("ok") else res.get("placeholder")
            if img and str(img) not in md:
                md = md.rstrip() + f"\n\n![{fig_sanitize_alt(res['caption'])}]({img})\n"
        out[-1] = (sid, head, md)
    return out


def fig_regenerate(state, mode, number, feedback, api_key, model, log=None):
    """Redraw one figure with the author's feedback and refresh every text
    that embeds it (sections, assembled document, bundle)."""
    stg = state["stages"]
    f = next((x for x in stg.get("fig_manifest", []) if int(x["number"]) == int(number)), None)
    if f is None:
        raise ValueError(f"figure {number} is not in the plan")
    inv, pool, ids = fig_source_inventory(mode, state)
    jstyle = fig_journal_style(fig_document_journal(mode, state))
    label_prefix = stg.get("fig_label_prefix", "Figure")
    lead = f"{label_prefix} {f['number']}{jstyle['lead']}"
    width = f.get("width", "double")
    spec = dict(f)
    spec["caption"] = ""
    spec["colour_ledger"] = fig_colour_ledger(stg["fig_manifest"])
    prev = stg.get("figures", {}).get(str(number), {})
    fb = (prev.get("spec", {}).get("feedback_history", "") + "\n" + feedback.strip()).strip()
    spec["feedback"] = fb
    spec["brief"] = (f.get("brief", "") + "\n\nREVISION FEEDBACK FROM THE AUTHOR "
                     f"(binding; latest last):\n{fb}")
    tables = fig_state_tables(state)
    if tables:
        spec["_tables"] = tables
    res = fig_make_one(spec, state["sig"], f["number"], pool, ids, api_key, model,
                       width, log=log, vision=(state.get("opts") or {}).get("vision", True),
                       jstyle=jstyle, lead=lead)
    f["feedback_history"] = fb
    res["spec"] = f
    if not res.get("ok"):
        first = _fig_err_summary(str(res.get("error", "")), 160)
        res["placeholder"] = fig_make_placeholder(
            state["sig"], f["number"], f.get("title", ""),
            f"Could not be rendered after feedback: {first}", width,
            jstyle.get(f"{width}_mm"))
        res["caption"] = (f"**{lead}{f.get('title', 'Figure')} (placeholder).** "
                          f"[AUTHOR: figure {f['number']} could not be redrawn - {first}]")
    stg["figures"][str(number)] = res
    png = res.get("png") or res.get("placeholder")
    cap = res.get("caption", "")
    if png:
        secs = stg.get("sections_out", {})
        embedded = False
        for sid, sec in secs.items():
            if str(png) in sec.get("text", ""):
                sec["text"] = fig_replace_caption(sec["text"], png, cap)
                embedded = True
        if not embedded and str(f.get("section_id")) in secs:
            sec = secs[str(f["section_id"])]
            sec["text"] = fig_insert_markdown(sec["text"], f["number"], cap, png)
        for key in ("manuscript_md", "article_md", "bundle"):
            if key in stg and str(png) in stg[key]:
                stg[key] = fig_replace_caption(stg[key], png, cap)
        if "bundle" in stg and "# Figures generated" in stg["bundle"]:
            head = stg["bundle"].split("# Figures generated")[0]
            stg["bundle"] = head + "# Figures generated\n\n" + fig_report_md(
                [stg["figures"][str(x["number"])] for x in stg["fig_manifest"]])
    _fig_save(state, mode)
    return res


def fig_report_md(figs):
    """Figures section of the bundle: provenance and QA per figure."""
    if not figs:
        return "(no figures planned)"
    lines = []
    for f in figs:
        sp = f.get("spec", {})
        n, kind = sp.get("number"), sp.get("kind")
        ep = (sp.get("epistemic") or {}).get("status", "")
        if f.get("ok"):
            lines.append(
                f"- Figure {n} ({kind}, {'data' if kind in FIG_KINDS_DATA else 'conceptual'}"
                + (f", {ep}" if ep else "") + f"): rendered in {f.get('attempts', 1)} "
                f"attempt(s); sources: {f.get('sources') or 'none (conceptual)'}; "
                f"QA: {f.get('qa_mode', 'skipped')}"
                + (f" - {'; '.join(str(i) for i in f['issues'])}" if f.get("issues")
                   else " - no issues")
                + (" - redrawn with author feedback" if sp.get("feedback_history") else "")
                + f" - {f['png']} (+ .svg, .pdf)")
        elif f.get("user_skipped"):
            lines.append(f"- Figure {n} ({kind}): skipped by the author at the "
                         "figure-plan checkpoint")
        elif f.get("skipped"):
            lines.append(f"- Figure {n} ({kind}): PLACEHOLDER - needs data the "
                         "sources lack (author item in the text)")
        else:
            lines.append(f"- Figure {n} ({kind}): PLACEHOLDER - could not be "
                         f"rendered: {_fig_err_summary(str(f.get('error', '')), 160)}")
    lines.append("\nEvery data figure plots only values declared in its DATA "
                 "block (or columns of the uploaded data tables), each checked "
                 "against the sources; the renderer harvests the numbers that "
                 "reached the canvas and rejects any not in DATA. Sidecar JSON "
                 "files (fig_NN.json) next to the PNGs record data, sources, "
                 "epistemic status and QA.")
    return "\n".join(lines)


# ------------------------------------------------------- checkpoint + redraw
def render_fig_checkpoint(state, mode, key_prefix, drive):
    """UI for status 'awaiting_figures': approve, edit, skip or re-plan
    the figures before any drawing cost."""
    stg = state["stages"]
    man = stg.get("fig_manifest", [])
    st.markdown("---")
    st.markdown("## Figure plan - awaiting your approval")
    st.caption("Untick a figure to skip it; add instructions to steer a "
               "drawing; or regenerate the whole plan with binding edits. "
               "Nothing is drawn until you approve.")
    for f in man:
        c1, c2 = st.columns([1, 5])
        with c1:
            f["draw"] = st.checkbox("Draw", value=f.get("draw", True) is not False,
                                    key=f"{key_prefix}_fig_draw_{f['number']}")
        with c2:
            cls = f.get("class") or ("data" if f.get("kind") in FIG_KINDS_DATA else "conceptual")
            st.markdown(f"**Figure {f['number']} - {f.get('title', '')}**  \n"
                        f"{f.get('kind')} · {cls} · section {f.get('section_id')} · "
                        f"{f.get('width', 'double')} column · epistemic "
                        f"{(f.get('epistemic') or {}).get('status', '-')}"
                        + ("  \n⚠️ needs author data: " + str(f.get("author_note", ""))
                           if f.get("needs_author") else ""))
            st.caption(f.get("claim") or f.get("purpose") or "")
            with st.expander("Brief and data", expanded=False):
                st.text(f.get("brief", ""))
                if f.get("data"):
                    st.json(f["data"])
            f["author_instructions"] = st.text_input(
                "Instructions for this figure (optional)",
                value=f.get("author_instructions", ""),
                key=f"{key_prefix}_fig_instr_{f['number']}")
    edits = st.text_area("Edits to the figure plan (regenerate the plan with "
                         "these binding instructions)", key=f"{key_prefix}_fig_edits",
                         placeholder="e.g. drop figure 2; make figure 1 a two-panel "
                                     "before/after mechanism; add a roadmap for WP3")
    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("✅ Approve and draw", type="primary", key=f"{key_prefix}_fig_ok"):
            state["figs_approved"] = True
            state["status"] = "running"
            _fig_save(state, mode)
            drive(state)
            st.rerun()
    with a2:
        if st.button("🔁 Regenerate plan with edits", key=f"{key_prefix}_fig_regen",
                     disabled=not edits.strip()):
            opts = state.setdefault("opts", {})
            opts["fig_plan_edits"] = (opts.get("fig_plan_edits", "") + "\n- " + edits.strip())
            stg.pop("fig_manifest", None)
            state["status"] = "running"
            _fig_save(state, mode)
            drive(state)
            st.rerun()
    with a3:
        if st.button("⏭️ Skip all figures", key=f"{key_prefix}_fig_skip"):
            for f in man:
                f["draw"] = False
            state["figs_approved"] = True
            state["status"] = "running"
            _fig_save(state, mode)
            drive(state)
            st.rerun()


def render_fig_regen_ui(state, mode, key_prefix):
    """Results view: each generated figure with its caption, QA notes and
    a feedback box to redraw it in place."""
    stg = state["stages"]
    figs = stg.get("figures", {})
    if not figs:
        return
    st.markdown(fig_report_md([figs[str(f["number"])] for f in stg["fig_manifest"]
                               if str(f["number"]) in figs]))
    for f in stg.get("fig_manifest", []):
        res = figs.get(str(f["number"]))
        if not res or res.get("user_skipped"):
            continue
        img = res.get("png") if res.get("ok") else res.get("placeholder")
        with st.expander(f"Figure {f['number']} - {f.get('title', '')}"
                         + ("" if res.get("ok") else " (placeholder)"), expanded=False):
            if img and Path(img).exists():
                st.image(img, use_container_width=True)
            st.markdown(res.get("caption", ""))
            if res.get("issues"):
                st.caption("QA: " + "; ".join(str(i) for i in res["issues"]))
            if res.get("ok"):
                d1, d2, d3 = st.columns(3)
                for col, ext, label in ((d1, "png", "PNG 300 dpi"), (d2, "svg", "SVG"),
                                        (d3, "pdf", "PDF")):
                    p = Path(res.get(ext) or Path(res["png"]).with_suffix(f".{ext}"))
                    if p.exists():
                        with col:
                            st.download_button(f"⬇️ {label}", p.read_bytes(),
                                               file_name=p.name,
                                               key=f"{key_prefix}_dl_{ext}_{f['number']}")
            fb = st.text_input("Feedback for a redraw", key=f"{key_prefix}_fig_fb_{f['number']}",
                               placeholder="e.g. move the LiF label above the layer; "
                                           "make panel b a dot plot grouped by protocol")
            if st.button("🎨 Redraw with this feedback", key=f"{key_prefix}_fig_redo_{f['number']}",
                         disabled=not (fb.strip() and api_key.strip())):
                try:
                    with st.status(f"Redrawing figure {f['number']}...", expanded=True) as box:
                        _RW_UI["box"] = box
                        r = fig_regenerate(state, mode, f["number"], fb.strip(),
                                           api_key.strip(), model, log=box.write)
                        box.update(label="Redrawn" if r.get("ok") else "Could not redraw",
                                   state="complete" if r.get("ok") else "error")
                    st.rerun()
                except Exception as e:
                    st.error(f"Redraw failed: {e}")


# ---------------------------------------------------------------- Studio
def render_figure_studio():
    st.markdown(
        "**Describe a figure; get a publication-grade PNG + SVG + PDF.** Schematics, "
        "mechanisms, workflows, architectures, roadmaps, taxonomies, "
        "graphical abstracts - and data charts, but only from numbers you "
        "supply here with their source (nothing is ever invented). Each "
        "figure is composed on a grid, drawn as matplotlib code in a "
        "sandbox, audited (numbers on the canvas vs. declared data; text "
        "overlaps; clipping) and reviewed visually; iterate with feedback.")
    kinds = sorted(FIG_KINDS_CONCEPTUAL | FIG_KINDS_DATA)
    kind = st.selectbox("Kind", kinds, index=kinds.index("schematic"), key="fs_kind")
    brief = st.text_area("Brief (panels, labels, layout, emphasis)", height=180,
                         key="fs_brief",
                         placeholder="e.g. Two-panel figure. (a) p-i-n device stack "
                                     "glass/ITO/NiOx/perovskite/C60/BCP/Ag as "
                                     "stacked layers with a highlighted buried "
                                     "interface; (b) workflow: synthesis -> "
                                     "characterisation -> outdoor test -> "
                                     "PeroDeg analytics, arrows left to right, "
                                     "the analytics box emphasised.")
    is_data = kind in FIG_KINDS_DATA
    data_txt = ""
    tables = {}
    if is_data:
        data_txt = st.text_area(
            "Data (one series per line: label | values comma-separated | unit | "
            "source | conditions (optional))",
            height=100, key="fs_data",
            placeholder="PCE by architecture | 25.2, 28.0, 26.5 | % | Table 1 of "
                        "Krishna 2024 | stabilised, 1 sun")
        ups = st.file_uploader("...or upload data files (CSV / XLSX) to plot "
                               "columns directly", type=["csv", "xlsx", "xls"],
                               accept_multiple_files=True, key="fs_tables")
        raw_tabs = []
        for u in ups or []:
            try:
                raw_tabs.append(extract_uploaded_table(u))
            except Exception as e:
                st.warning(f"Could not read {u.name}: {e}")
        if raw_tabs:
            tables = fig_tables_pack(raw_tabs)
            st.caption(fig_tables_schema(tables)[:1500])
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        width = st.selectbox("Width", ["double", "single", "onehalf"], key="fs_w")
    with c2:
        jname = st.selectbox("Journal style", ["Nature family", "Science",
                                               "Cell Press (Joule / Matter)",
                                               "RSC (EES)", "Wiley (Advanced)",
                                               "ACS"], key="fs_j")
    with c3:
        vision = st.checkbox("Visual QA pass", value=True, key="fs_qa",
                             help="API backend: the image is reviewed directly. "
                                  "Max backend: Claude Code reads the PNG, after a "
                                  "one-time probe confirms it can see images.")
    with c4:
        fs_model = st.selectbox("Model", list(MODELS), index=3, key="fs_model")
    epi = st.selectbox("Epistemic status of any mechanism shown",
                       ["DIRECT (shown by data)", "INDIRECT (consistent with data)",
                        "SPECULATIVE (proposal)"], key="fs_epi")
    feedback = st.text_input("Feedback on the last version (optional)", key="fs_fb",
                             placeholder="e.g. make the arrows thicker; move the "
                                         "legend outside; wrap the long label")
    if st.button("🎨 Draw", type="primary", key="fs_go",
                 disabled=not (brief.strip() and api_key.strip())):
        data, pool, ids = [], set(), set()
        for ln in (data_txt or "").splitlines():
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) >= 4:
                try:
                    vals = [float(x) for x in parts[1].split(",") if x.strip()]
                except ValueError:
                    st.warning(f"Could not parse values in: {ln}")
                    continue
                cond = parts[4] if len(parts) > 4 else ""
                data.append({"label": parts[0], "values": vals, "unit": parts[2],
                             "conditions": [cond] * len(vals), "source": parts[3]})
                pool |= {_fig_num_key(v) for v in vals}
                ids.add(parts[3])
        for name in tables:
            ids |= {f"FILE:{name}", name}
        spec = {"number": 1, "kind": kind,
                "class": "data" if is_data else "conceptual",
                "title": brief.split(".")[0][:60], "purpose": "", "width": width,
                "brief": brief.strip() + (f"\n\nREVISION FEEDBACK FROM THE AUTHOR: "
                                          f"{feedback.strip()}" if feedback.strip() else ""),
                "epistemic": {"status": epi.split(" ")[0], "evidence_ids": []},
                "data": data, "caption": ""}
        if tables:
            spec["_tables"] = tables
        job = "studio_" + _rwhash.sha1(brief.encode("utf-8", "replace")).hexdigest()[:8]
        n = st.session_state.get("fs_n", 0) + 1
        st.session_state["fs_n"] = n
        jstyle = fig_journal_style(jname)
        try:
            with st.status("Drawing...", expanded=True) as box:
                _RW_UI["box"] = box
                res = fig_make_one(spec, job, n, pool, ids, api_key.strip(),
                                   MODELS[fs_model], width, log=box.write,
                                   vision=vision, jstyle=jstyle,
                                   lead=f"Figure {n}{jstyle['lead']}")
                box.update(label="Done" if res.get("ok") else "Failed",
                           state="complete" if res.get("ok") else "error")
            st.session_state["fs_last"] = res
        except Exception as e:
            st.error(f"Figure generation failed: {e}")
    res = st.session_state.get("fs_last")
    if res:
        if res.get("ok"):
            st.image(res["png"], use_container_width=True)
            st.markdown(res["caption"])
            st.caption(f"QA: {res.get('qa_mode', 'skipped')}"
                       + (" - " + "; ".join(str(i) for i in res["issues"])
                          if res.get("issues") else " - no issues"))
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                st.download_button("⬇️ PNG (300 dpi)", Path(res["png"]).read_bytes(),
                                   file_name=Path(res["png"]).name, key="fs_dl_png")
            with d2:
                st.download_button("⬇️ SVG (vector)", Path(res["svg"]).read_bytes(),
                                   file_name=Path(res["svg"]).name, key="fs_dl_svg")
            with d3:
                pdfp = Path(res.get("pdf") or Path(res["png"]).with_suffix(".pdf"))
                if pdfp.exists():
                    st.download_button("⬇️ PDF (vector)", pdfp.read_bytes(),
                                       file_name=pdfp.name, key="fs_dl_pdf")
            with d4:
                if st.button("📁 Copy to figures_out (Slide Studio / Analytics)",
                             key="fs_copy"):
                    dest = ANSWERS_DIR / "figures_out"
                    dest.mkdir(parents=True, exist_ok=True)
                    import shutil as _sh
                    for ext in ("png", "svg", "pdf"):
                        p = Path(res["png"]).with_suffix(f".{ext}")
                        if p.exists():
                            _sh.copy(p, dest / p.name)
                    st.success("Copied.")
            with st.expander("Code"):
                st.code(res["code"], language="python")
        else:
            st.error(f"Could not render after {res.get('attempts')} attempt(s): "
                     f"{res.get('error', '')[:800]}")
            with st.expander("Last code"):
                st.code(res.get("code", ""), language="python")


FIG_IMG_RE = re.compile(r"!\[.*?\]\(([^()\s]+?\.(?:png|svg|jpe?g))\)")


def st_md_figs(md):
    """Render markdown in Streamlit, showing embedded ![caption](path)
    figures as real images (st.markdown cannot serve local files)."""
    pos = 0
    for m in FIG_IMG_RE.finditer(md):
        chunk = md[pos:m.start()]
        if chunk.strip():
            st.markdown(RW_AUTHOR_RE.sub(lambda x: f"**{x.group(0)}**", chunk))
        alt, path = re.match(r"!\[(.*?)\]\((.*?)\)", m.group(0), re.S).groups()
        if Path(path).exists():
            st.image(path, use_container_width=True)
            st.caption(alt)
        else:
            st.caption(f"[missing figure: {path}]")
        pos = m.end()
    tail = md[pos:]
    if tail.strip():
        st.markdown(RW_AUTHOR_RE.sub(lambda x: f"**{x.group(0)}**", tail))


# ==========================================================================
# Revision mode - reviewer reports + manuscript -> point-by-point response
# letter, exact-match text changes applied to the manuscript, before/after
# diff, Word bundle. Staged and resumable like the rewrite pipeline.
# ==========================================================================
RX_DIR = ANSWERS_DIR / "revision_jobs"

RX_POINTS_SYSTEM = """\
You are the handling editor's assistant. You receive the decision letter and reviewer reports for a manuscript. Split them into the individual points a response letter must answer, exactly as an author team would number them, and classify each.

Rules: one point per distinct request or criticism (split compound comments; keep praise only if it needs acknowledgement); keep the reviewer's own words verbatim in "text" (trim only surrounding boilerplate); "reviewer" is the reviewer number (0 for the editor); "type" in {experiment, analysis, clarification, citation, writing, error, scope, praise}; "severity" in {major, minor}; "asks_for" is a one-line paraphrase of what would satisfy the reviewer.
OUTPUT: ONLY a JSON object {"points": [{"id": "R1.1", "reviewer": 1, "type": "analysis", "severity": "major", "text": "...", "asks_for": "..."}]} in reading order; ids R<reviewer>.<n> (E.1, E.2 for the editor)."""

RX_RESPOND_SYSTEM = """\
You draft the authors' point-by-point response for a manuscript under revision at {JOURNAL}, and the exact text changes that back each response. You receive the reviewer points to answer in this batch, the manuscript (with section ids), the supporting files (Supplementary Information, figure captions, cover letter ... each under '=== FILE: <name> ==='), the AUTHORS' NOTES (what they have done, new data they now have, positions they want to take) and, optionally, numbered passages [n] from the authors' literature corpus.

RULES OF HONESTY - these outrank everything else
- Never invent a result, a measurement, a number, a figure, a reference or a change that the manuscript, the supporting files or the authors' notes do not contain. Where a point needs new data, a new experiment or a decision, answer with what CAN be said now and put the rest in an [AUTHOR: ...] marker stating exactly what is needed.
- Every number in a response or a text change must exist verbatim in the manuscript, the supporting files or the authors' notes.
- Cite literature only as [n] passages provided; never from memory.
- A rebuttal is allowed when the reviewer is wrong or asks beyond scope: make it with evidence (the manuscript's own data, the supporting files, [n]) and courtesy.

STYLE - top-journal response letters
- One reply per point. Thank the reviewers once (the letter opener is written separately) - not in every reply. Confident, specific, never obsequious or defensive. Lead with the action ("We have added ...", "We agree and now ...", "We respectfully disagree because ...").
- Point to where the change is: document, section heading and, when possible, a short quote of the new sentence.
- Keep each response under ~180 words unless the point is major.

TEXT CHANGES
For every response that changes a document, give the exact edit as a find/replace pair: "doc" is "manuscript" or the exact file name of the supporting file the change belongs to (SI figures, SI tables and SI methods live in the SI file); "find" is a verbatim substring of THAT document (40-300 characters, no ellipses, occurring once); "replace_with" is the new text that replaces it (keep everything that should stay). New paragraphs are inserted by including the sentence before them in "find" and repeating it in "replace_with" followed by the new paragraph. Every number in "replace_with" must be in the sources. If a change needs data the authors do not yet have, do not fabricate it: write the sentence with an [AUTHOR: insert ...] marker.

If a PREVIOUS RESPONSE and an AUTHOR'S INSTRUCTION are given for a point, revise that response as instructed (keep what the instruction does not touch), and return the complete new response and its complete list of changes - the previous changes are discarded.

OUTPUT: ONLY a JSON object:
{"responses": [{"id": "R1.1", "action": "revised_text | new_data_needed | rebuttal | clarified | out_of_scope",
  "response_md": "the reply text (markdown; [AUTHOR: ...] markers allowed)",
  "changes": [{"doc": "manuscript", "find": "...", "replace_with": "...", "where": "section heading", "why": "one line"}],
  "author_needed": "what only the authors can supply, or empty string"}]}"""

RX_LETTER_SYSTEM = """\
You write the opening and closing of a response letter to the editor of {JOURNAL} for a revised manuscript, from the point-by-point material provided. Opening (<= 180 words): thank the editor and reviewers once, state the manuscript id/title if given, summarise the main revisions in 3-5 bullets (only revisions that actually appear in the point-by-point material), mention any new supplementary items by their names in the material. Closing (<= 80 words): the authors' confidence statement and standard courtesies. No invented facts. Output markdown with the headings "## Letter to the editor" and "## Closing". Nothing else."""


def rx_save_state(state):
    try:
        d = RX_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        slim = dict(state)
        slim["hits"] = []
        (d / "state.json").write_text(_rwjson.dumps(slim, default=str), encoding="utf-8")
    except Exception:
        pass


def rx_load_last_state():
    try:
        cands = sorted(RX_DIR.glob("*/state.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        if cands:
            return _rwjson.loads(cands[0].read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


RX_UNIT_RE = re.compile(r"(?<![\w.])(\d{1,2}(?:\.\d+)?)\s?(?:%|°\s?C|℃|\bK\b|\bh\b|\bs\b|"
                        r"\bmin\b|\bnm\b|\bµm\b|\bum\b|\bmV\b|\bV\b|\bmA\b|\bcm\b|\bmm\b|"
                        r"\bwt\b|\bmol\b|\bdays?\b|\bcycles\b|\bdevices\b|\bcells\b)")


def rx_numbers_strict(text):
    """rw_numbers_in plus small integers that carry a unit (82 %, 65 C,
    50 nm, 6 devices): those are measurements and must be sourced."""
    out = set(rw_numbers_in(text))
    for m in RX_UNIT_RE.finditer(text or ""):
        out.add(_rw_norm_num(m.group(1)))
    return out


def rx_number_ok(find, repl, pool):
    """Numbers introduced by a change must exist in the sources (pools are
    built with rx_numbers_strict / fu_pool)."""
    new = rx_numbers_strict(repl) - rx_numbers_strict(find)
    bad = {n for n in new if n not in pool}
    return not bad, bad


def rx_apply_changes(paragraphs, changes, pool):
    """Apply exact-match find/replace changes to the manuscript paragraphs
    (list of str). A change is applied only if `find` occurs exactly once
    in the whole text and introduces no number absent from the sources.
    Returns (paragraphs, applied, skipped)."""
    applied, skipped = [], []
    for ch in changes:
        find, repl = str(ch.get("find", "")), str(ch.get("replace_with", ""))
        if not find or len(find) < 15 or len(find) > 600 or find == repl:
            skipped.append(dict(ch, reason="empty, too short/long or identical"))
            continue
        hits = [i for i, p in enumerate(paragraphs) if find in p]
        total = sum(p.count(find) for p in paragraphs)
        if total != 1:
            skipped.append(dict(ch, reason=f"text found {total} times (needs exactly 1)"))
            continue
        ok, bad = rx_number_ok(find, repl, pool)
        if not ok:
            skipped.append(dict(ch, reason="introduces number(s) not in the sources: "
                                + ", ".join(sorted(bad))))
            continue
        i = hits[0]
        paragraphs[i] = paragraphs[i].replace(find, repl, 1)
        applied.append(ch)
    return paragraphs, applied, skipped


def rx_diff_paragraphs(before, after):
    """Paragraph-level before/after list for changed paragraphs, with
    word-level markup (~~deleted~~ / **inserted**)."""
    import difflib
    out = []
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        b_par = "\n".join(before[i1:i2])
        a_par = "\n".join(after[j1:j2])
        out.append({"before": b_par, "after": a_par, "marked": rx_word_diff(b_par, a_par)})
    return out


def rx_word_diff(a, b):
    import difflib
    aw, bw = a.split(), b.split()
    sm = difflib.SequenceMatcher(a=aw, b=bw, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(" ".join(aw[i1:i2]))
        elif tag == "delete":
            parts.append("~~" + " ".join(aw[i1:i2]) + "~~")
        elif tag == "insert":
            parts.append("**" + " ".join(bw[j1:j2]) + "**")
        else:
            parts.append("~~" + " ".join(aw[i1:i2]) + "~~ **" + " ".join(bw[j1:j2]) + "**")
    return " ".join(parts)


def rx_diff_docx(doc, diffs):
    """Word rendering of the paragraph diff: deletions struck through in
    red, insertions bold blue - the reviewer-facing 'marked manuscript'."""
    for d in diffs:
        p = doc.add_paragraph()
        for m in re.finditer(r"~~(.+?)~~|\*\*(.+?)\*\*|([^~*]+)", d["marked"]):
            if m.group(1) is not None:
                r = p.add_run(m.group(1) + " ")
                r.font.strike = True
                r.font.color.rgb = RGBColor(0xB0, 0x1E, 0x1E)
            elif m.group(2) is not None:
                r = p.add_run(m.group(2) + " ")
                r.bold = True
                r.font.color.rgb = RGBColor(0x0B, 0x4F, 0xA8)
            else:
                p.add_run(m.group(3))
        doc.add_paragraph()


def rx_docs(state):
    """Ordered {doc name: original paragraphs} - manuscript first, then every
    supporting file (old jobs may carry only si_text)."""
    inp = state["inputs"]
    docs = {"manuscript": list(inp["ms_paragraphs"])}
    for d in inp.get("si_docs") or []:
        docs[d["name"]] = list(d.get("paragraphs") or rx_mark_sections(d.get("text", ""))[0])
    if not inp.get("si_docs") and inp.get("si_text"):
        docs["Supplementary Information"] = rx_mark_sections(inp["si_text"])[0]
    return docs


def rx_sources_text(state, cap_si=60000):
    """The supporting files as the model sees them (file headers)."""
    inp = state["inputs"]
    if inp.get("si_docs"):
        out, used = [], 0
        for d in inp["si_docs"]:
            take = d.get("text", "")[:max(2000, (cap_si - used) // max(1, len(inp["si_docs"])))]
            used += len(take)
            out.append(f"=== FILE: {d['name']} ===\n{take}")
        return "\n\n".join(out)
    return inp.get("si_text", "")[:cap_si]


def rx_pool(state):
    """Numbers that may appear in changes: manuscript, supporting files,
    notes of every round, per-point notes and redo instructions."""
    inp, opts, stg = state["inputs"], state["opts"], state["stages"]
    texts = [inp["ms_text"], inp.get("si_text", ""), opts.get("notes", "")]
    texts += [d.get("text", "") for d in inp.get("si_docs") or []]
    texts += list(opts.get("round_notes") or [])
    texts += [p.get("note", "") for p in stg.get("points", [])]
    texts += [it.get("instruction", "") for it in stg.get("iterations", [])]
    pool = set()
    for t in texts:
        pool |= rx_numbers_strict(t or "")
    return pool


def _rx_doc_for_change(ch, docs):
    """Pick the document a change belongs to: the named one if the text is
    found there once, else the unique document containing it once."""
    find = str(ch.get("find", ""))
    named = str(ch.get("doc") or "manuscript").strip()
    order = list(docs)
    keyed = {n.lower(): n for n in docs}
    cand = keyed.get(named.lower())
    if not cand:
        for n in docs:
            if named.lower() in n.lower() or n.lower() in named.lower():
                cand = n
                break
    if not cand and any(k in named.lower() for k in ("si", "supp")) and len(docs) == 2:
        cand = order[1]
    if cand:
        order = [cand] + [n for n in order if n != cand]
    if find:
        for n in order:
            if sum(p.count(find) for p in docs[n]) == 1:
                return n
    return cand or order[0]


def rx_apply_all(state):
    """Apply every change of the answered points to the original documents
    (idempotent: always from the originals), fill applied / skipped /
    diff for the manuscript and si_revised / si_diff per supporting file."""
    stg = state["stages"]
    pts = [p for p in stg["points"] if p.get("answer", True) is not False]
    docs = rx_docs(state)
    pool = rx_pool(state)
    per_doc = {n: [] for n in docs}
    for p in pts:
        for ch in stg["responses"].get(p["id"], {}).get("changes", []) or []:
            if isinstance(ch, dict):
                ch = dict(ch, point=p["id"])
                ch["doc"] = _rx_doc_for_change(ch, docs)
                per_doc[ch["doc"]].append(ch)
    applied, skipped = [], []
    revised = {}
    for n, paras in docs.items():
        new, a, s = rx_apply_changes(list(paras), per_doc[n], pool)
        revised[n] = new
        applied += a
        skipped += s
    stg["revised_paragraphs"] = revised["manuscript"]
    stg["applied"], stg["skipped"] = applied, skipped
    stg["diff"] = rx_diff_paragraphs(docs["manuscript"], revised["manuscript"])
    stg["si_revised"] = {n: revised[n] for n in docs if n != "manuscript"}
    stg["si_diff"] = {n: rx_diff_paragraphs(docs[n], revised[n]) for n in docs if n != "manuscript"}


def rx_assemble_letter(state):
    """letter_md from the opening/closing (stg['letter']) and the current
    replies, applied and skipped changes - no model call."""
    stg = state["stages"]
    pts = [p for p in stg["points"] if p.get("answer", True) is not False]
    letter = stg.get("letter") or ""
    by_rev = {}
    for p in pts:
        by_rev.setdefault(p["reviewer"], []).append(p)
    parts = [letter.split("## Closing")[0].strip(), ""]
    for rev in sorted(by_rev):
        parts.append(f"## {'Editor' if rev == 0 else f'Reviewer {rev}'}\n")
        for p in by_rev[rev]:
            r = stg["responses"][p["id"]]
            chg = [c for c in stg["applied"] if c.get("point") == p["id"]]
            skp = [c for c in stg["skipped"] if c.get("point") == p["id"]]
            parts.append(f"**{p['id']} ({p['type']}, {p['severity']}).** "
                         f"*{p['text'].strip()}*\n\n**Response.** {str(r.get('response_md', '')).strip()}")
            if chg:
                parts.append("*Changes made:* " + "; ".join(
                    (f"[{c['doc']}] " if c.get("doc", "manuscript") != "manuscript" else "")
                    + f"{c.get('where', '')} - \"{str(c.get('replace_with', ''))[:120]}…\"" for c in chg))
            if skp:
                parts.append("[AUTHOR: proposed change(s) not applied automatically - "
                             + "; ".join(f"{c.get('where', '')}: {c.get('reason', '')}" for c in skp) + "]")
            if r.get("author_needed"):
                parts.append(f"[AUTHOR: {r['author_needed']}]")
            parts.append("")
    closing = letter.split("## Closing")[-1].strip() if "## Closing" in letter else ""
    stg["letter_md"] = "\n".join(parts) + ("\n\n## Closing\n\n" + closing if closing else "")


def rx_snapshot(state, label):
    """Keep the current letter and revised documents as a numbered version."""
    stg = state["stages"]
    vs = state.setdefault("versions", [])
    vs.append({"n": len(vs) + 1, "label": label,
               "time": f"{datetime.datetime.now():%Y-%m-%d %H:%M}",
               "letter_md": stg.get("letter_md", ""),
               "revised_md": "\n\n".join(stg.get("revised_paragraphs", [])),
               "si_revised": {n: "\n\n".join(v) for n, v in (stg.get("si_revised") or {}).items()},
               "n_applied": len(stg.get("applied", [])), "n_skipped": len(stg.get("skipped", []))})
    del vs[:-12]


def rx_rebuild(state, label):
    """Re-apply changes, re-assemble the letter, rebuild the bundle, keep
    a version, persist."""
    rx_apply_all(state)
    rx_assemble_letter(state)
    rx_bundle(state)
    rx_snapshot(state, label)
    rx_save_state(state)


def rx_draft_points(state, grp, instruction="", status_box=None):
    """One model call drafting (or re-drafting) the given points; stores
    the responses. `instruction` turns it into a revision of the previous
    responses."""
    inp, opts, stg = state["inputs"], state["opts"], state["stages"]
    corpus = ""
    if state.get("hits"):
        corpus = ("\n\n=== LITERATURE PASSAGES FROM THE AUTHORS' CORPUS (cite as [n]) ===\n"
                  + build_context(state["hits"]))
    blocks = []
    for p in grp:
        b = (f"[{p['id']}] reviewer {p['reviewer']} · {p['type']} · {p['severity']}\n{p['text']}\n"
             f"ASKS FOR: {p.get('asks_for', '')}")
        if p.get("note"):
            b += f"\nAUTHORS' NOTE ON THIS POINT: {p['note']}"
        prev = stg.get("responses", {}).get(p["id"]) if instruction else None
        if prev:
            b += (f"\nPREVIOUS RESPONSE (to revise):\n{str(prev.get('response_md', ''))[:4000]}\n"
                  f"PREVIOUS CHANGES: {_rwjson.dumps(prev.get('changes', []))[:4000]}\n"
                  f"AUTHOR'S INSTRUCTION FOR THIS REVISION: {instruction}")
        blocks.append(b)
    round_notes = "\n".join(f"- {n}" for n in (opts.get("round_notes") or []) if n.strip())
    sysm = RX_RESPOND_SYSTEM.replace("{JOURNAL}", opts.get("journal") or "the journal")
    si = rx_sources_text(state)
    umsg = (f"POINTS TO ANSWER IN THIS BATCH:\n\n" + "\n\n".join(blocks) + "\n\n"
            f"=== AUTHORS' NOTES (what has been done / new data / stance) ===\n"
            f"{opts.get('notes') or '(none given - mark every new-data request [AUTHOR: ...])'}\n"
            + (f"\n=== AUTHORS' NOTES FROM LATER ROUNDS ===\n{round_notes}\n" if round_notes else "")
            + f"\n=== MANUSCRIPT (section ids in brackets) ===\n{inp['ms_marked'][:120000]}\n\n"
            + (f"=== SUPPORTING FILES ===\n{si}\n\n" if si else "")
            + corpus + "\n\nWrite the responses and changes now (JSON only).")
    raw = rw_call(sysm, umsg, 9000)
    resp = (_rw_json_block(raw) or {}).get("responses") or []
    got = {str(r.get("id")): r for r in resp if isinstance(r, dict)}
    stg.setdefault("responses", {})
    for p in grp:
        r = got.get(p["id"]) or {"id": p["id"], "action": "clarified",
                                 "response_md": "[AUTHOR: the model returned no response for "
                                                "this point - write it]",
                                 "changes": [], "author_needed": ""}
        r.setdefault("changes", [])
        if instruction:
            r["revised_by_instruction"] = instruction
        stg["responses"][p["id"]] = r
    rx_save_state(state)


def rx_redo_points(state, ids, instruction, round_note=""):
    """Iteration: re-draft the given points under an instruction (and an
    optional new round note), then rebuild everything as a new version."""
    stg, opts = state["stages"], state["opts"]
    if round_note and round_note.strip():
        opts.setdefault("round_notes", [])
        if round_note.strip() not in opts["round_notes"]:
            opts["round_notes"].append(round_note.strip())
    state["round"] = int(state.get("round", 1)) + 1
    stg.setdefault("iterations", []).append(
        {"round": state["round"], "ids": list(ids), "instruction": instruction,
         "time": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"})
    pts = [p for p in stg["points"] if p["id"] in set(ids) and p.get("answer", True) is not False]
    for bi in range(0, len(pts), 5):
        rx_draft_points(state, pts[bi:bi + 5], instruction=instruction or "Revise as the notes say.")
    rx_rebuild(state, f"round {state['round']}: redo {', '.join(ids)}")


def rx_run(state, status_box):
    """points -> (checkpoint) -> responses + changes (batched) -> apply ->
    diff -> letter -> bundle."""
    _RW_UI["box"] = status_box
    inp, opts, stg = state["inputs"], state["opts"], state["stages"]

    def step(msg):
        status_box.write(msg)
        rw_log(state, msg)

    if "points" not in stg:
        step("Splitting the reviews into points...")
        raw = rw_call(RX_POINTS_SYSTEM, f"DECISION LETTER AND REVIEWS:\n\n{inp['reviews'][:120000]}",
                      6000)
        pts = (_rw_json_block(raw) or {}).get("points") or []
        pts = [p for p in pts if isinstance(p, dict) and p.get("text")]
        for i, p in enumerate(pts, 1):
            p.setdefault("id", f"P.{i}")
            p.setdefault("reviewer", 1)
            p.setdefault("type", "clarification")
            p.setdefault("severity", "minor")
            p["answer"] = p.get("answer", True)
        if not pts:
            raise RuntimeError("No reviewer points could be extracted - check the reviews text.")
        stg["points"] = pts
        rx_save_state(state)
        if opts.get("pause_points") and not state.get("points_approved"):
            state["status"] = "awaiting_points"
            rx_save_state(state)
            return

    pts = [p for p in stg["points"] if p.get("answer", True) is not False]
    stg.setdefault("responses", {})
    todo = [p for p in pts if p["id"] not in stg["responses"]]
    if todo and opts.get("ground") and index_ok and "hits" not in state:
        try:
            q = " ".join(p["text"][:200] for p in pts[:6])[:1500]
            state["hits"] = retrieve(q, min(top_k, 10))
        except Exception:
            state["hits"] = []
    batch, n_batches = 5, max(1, (len(todo) + 4) // 5)
    for bi in range(0, len(todo), batch):
        grp = todo[bi:bi + batch]
        step(f"Drafting responses {bi // batch + 1}/{n_batches} "
             f"({', '.join(p['id'] for p in grp)})...")
        rx_draft_points(state, grp)

    if "applied" not in stg:
        step("Applying exact-match text changes...")
        rx_apply_all(state)
        rx_save_state(state)

    if "letter" not in stg:
        step("Letter opening and closing...")
        summary = "\n".join(f"[{p['id']}] {stg['responses'][p['id']].get('action')}: "
                            f"{str(stg['responses'][p['id']].get('response_md'))[:300]}"
                            for p in pts)
        umsg = (f"MANUSCRIPT TITLE / ID: {inp.get('ms_name', '')}\n\n"
                f"POINT-BY-POINT MATERIAL:\n{summary[:40000]}\n\n"
                f"CHANGES APPLIED: {len(stg['applied'])}; changes needing the authors: "
                f"{len(stg['skipped'])}")
        stg["letter"] = rw_call(RX_LETTER_SYSTEM.replace("{JOURNAL}", opts.get("journal") or "the journal"),
                                umsg, 2500)
        rx_save_state(state)

    if "bundle" not in stg:
        step("Assembling...")
        rx_assemble_letter(state)
        rx_bundle(state)
        if not state.get("versions"):
            rx_snapshot(state, "round 1: first draft")
        try:
            record_qa(f"[RESPONSE TO REVIEWERS] {inp.get('ms_name', '')}", stg["bundle"],
                      state.get("hits", []), do_autosave)
        except Exception as e:
            rw_log(state, f"record_qa failed: {e}")
        state["status"] = "complete"
        rx_save_state(state)


def rx_bundle(state):
    """(Re)build the bundle text and the Word file from letter_md,
    revised_paragraphs, diff and the revised supporting files - also
    after follow-up edits and iterations."""
    inp, stg = state["inputs"], state["stages"]
    pts = [p for p in stg["points"] if p.get("answer", True) is not False]
    letter_md = stg["letter_md"]
    revised_md = "\n\n".join(stg["revised_paragraphs"])
    diff_md = "\n\n".join(f"**Before:** {d['before']}\n\n**After:** {d['after']}"
                          for d in stg["diff"]) or "(no paragraph changed)"
    si_md = ""
    for n, paras in (stg.get("si_revised") or {}).items():
        dd = (stg.get("si_diff") or {}).get(n) or []
        if dd:
            si_md += (f"\n\n---\n\n# Changes in {n} (before / after)\n\n"
                      + "\n\n".join(f"**Before:** {d['before']}\n\n**After:** {d['after']}" for d in dd)
                      + f"\n\n---\n\n# Revised {n}\n\n" + "\n\n".join(paras))
    markers = RW_AUTHOR_RE.findall(letter_md + revised_md)
    n_major = sum(1 for p in pts if p.get("severity") == "major")
    n_si = sum(1 for c in stg["applied"] if c.get("doc", "manuscript") != "manuscript")
    stg["bundle"] = (f"# Response to reviewers - {inp.get('ms_name', '')}\n\n"
                     f"*{len(pts)} points ({n_major} major) · {len(stg['applied'])} text changes "
                     f"applied ({n_si} in supporting files) · {len(stg['skipped'])} proposed changes "
                     f"need the authors · [AUTHOR] items: {len(markers)}"
                     + (f" · version {state['versions'][-1]['n']}" if state.get("versions") else "")
                     + "*\n\n"
                     "> Every response is grounded in the manuscript, the supporting files and your "
                     "notes; nothing was invented. Resolve every [AUTHOR: ...] item, then paste the "
                     "letter into the journal's response form.\n\n---\n\n"
                     f"{letter_md}\n\n---\n\n# Summary of changes (before / after)\n\n{diff_md}"
                     f"\n\n---\n\n# Revised manuscript\n\n{revised_md}" + si_md)
    stg["revised_md"] = revised_md
    stg["si_revised_md"] = {n: "\n\n".join(v) for n, v in (stg.get("si_revised") or {}).items()}
    try:
        d = RX_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        doc = Document()
        doc.add_heading("Response to reviewers", level=1)
        md_to_docx(doc, letter_md)
        doc.add_page_break()
        doc.add_heading("Marked changes (deletions struck through, insertions bold)", level=1)
        rx_diff_docx(doc, stg["diff"])
        doc.add_page_break()
        doc.add_heading("Revised manuscript (clean)", level=1)
        md_to_docx(doc, revised_md)
        for n, paras in (stg.get("si_revised") or {}).items():
            dd = (stg.get("si_diff") or {}).get(n) or []
            if not dd:
                continue
            doc.add_page_break()
            doc.add_heading(f"Marked changes in {n}", level=1)
            rx_diff_docx(doc, dd)
            doc.add_page_break()
            doc.add_heading(f"Revised {n} (clean)", level=1)
            md_to_docx(doc, "\n\n".join(paras))
        doc.save(d / "response_bundle.docx")
        state["docx"] = str(d / "response_bundle.docx")
    except Exception as e:
        rw_log(state, f"docx failed: {e}")


def rx_mark_sections(text):
    """Paragraph list + a copy with [S<n>] ids on headings for the model.
    Word files arrive with single newlines between paragraphs (no blank
    lines), so fall back to line splitting when blank lines are absent."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 2 and text.count("\n") >= 4:
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    marked, n = [], 0
    for p in paras:
        if re.match(r"^(#+\s|\d+(\.\d+)*\s+[A-Z]|[A-Z][A-Za-z ]{2,40}$)", p) and len(p) < 120:
            n += 1
            marked.append(f"[S{n}] {p}")
        else:
            marked.append(p)
    return paras, "\n\n".join(marked)


def render_revision_panel():
    st.markdown(
        "**Respond to reviewers - staged.** Upload the manuscript (the version the "
        "reviewers saw), the supporting files (SI, figure captions, cover letter - as "
        "many as you like) and the decision letter with the reviews, and add your "
        "notes (what you have done, new data you now have, where you want to push "
        "back). The pipeline splits the reviews into numbered points (you approve the "
        "list), drafts a grounded point-by-point response, proposes exact text "
        "changes in the manuscript AND the supporting files, applies only those that "
        "match exactly and add no unsourced number, and produces the letter, "
        "before/after diffs and the revised documents as one Word bundle. Then you "
        "iterate: edit any reply, redo a point with an instruction, add new facts.")
    st.caption("Nothing is invented: requests for new data become [AUTHOR: ...] "
               "items unless your notes supply them.")
    c1, c2 = st.columns(2)
    with c1:
        ms_up = st.file_uploader("Manuscript as reviewed (required)",
                                 type=["docx", "pdf", "txt", "md"], key="rx_ms")
        si_ups = st.file_uploader("Supporting files: SI, captions, cover letter ... (optional, several)",
                                  type=["docx", "pdf", "txt", "md"], key="rx_si",
                                  accept_multiple_files=True)
    with c2:
        rv_ups = st.file_uploader("Decision letter + reviews (one or several files)",
                                  type=["docx", "pdf", "txt", "md"], key="rx_rev",
                                  accept_multiple_files=True)
        rv_txt = st.text_area("...or paste the reviews here", height=120, key="rx_rev_txt")
    notes = st.text_area("Your notes: what you have done, new results (with numbers), "
                         "your stance on each point (optional but decisive)", height=140,
                         key="rx_notes",
                         placeholder="e.g. R1.2: we repeated the MPP test on 6 new devices - "
                                     "T80 = 1,120 h (Fig. S12 new). R2.1: we disagree; the "
                                     "reviewer confuses Voc loss with FF loss ...")
    o1, o2, o3 = st.columns(3)
    with o1:
        journal = st.text_input("Journal", value="", key="rx_journal",
                                placeholder="e.g. Nature Energy")
    with o2:
        pause_points = st.checkbox("Pause after the point list for my approval",
                                   value=True, key="rx_pause")
    with o3:
        ground = st.checkbox("Ground rebuttals in my corpus", value=False,
                             disabled=not index_ok, key="rx_ground")
    ms_text = ms_name = reviews = ""
    si_docs = []
    if ms_up is not None:
        try:
            ms_text, ms_name = extract_uploaded_text(ms_up), ms_up.name
        except Exception as e:
            st.error(f"Could not read {ms_up.name}: {e}")
    for up in (si_ups or []):
        try:
            txt = extract_uploaded_text(up)
            if txt.strip():
                si_docs.append({"name": up.name, "text": txt, "paragraphs": rx_mark_sections(txt)[0]})
        except Exception as e:
            st.error(f"Could not read {up.name}: {e}")
    si_text = "\n\n".join(f"=== FILE: {d['name']} ===\n{d['text']}" for d in si_docs)
    rv_parts = []
    for up in (rv_ups or []):
        try:
            rv_parts.append(f"=== FILE: {up.name} ===\n{extract_uploaded_text(up)}")
        except Exception as e:
            st.error(f"Could not read {up.name}: {e}")
    if rv_txt.strip():
        rv_parts.append(rv_txt.strip())
    reviews = "\n\n".join(rv_parts).strip()
    if ms_text:
        st.caption(f"{ms_name}: {len(ms_text.split()):,} words · reviews: "
                   f"{len(reviews.split()):,} words"
                   + (" · " + " · ".join(f"{d['name']}: {len(d['text'].split()):,} words"
                                         for d in si_docs) if si_docs else " · no supporting files"))

    state = st.session_state.get("rx")
    if state is None:
        last = rx_load_last_state()
        if last and last.get("status") != "complete":
            if st.button(f"↩️ Resume the unfinished job ({last['inputs'].get('ms_name', '?')}, "
                         f"{last.get('status')})", key="rx_resume_disk"):
                st.session_state["rx"] = last
                st.rerun()
        elif last and last.get("status") == "complete":
            if st.button(f"↩️ Reopen the last response ({last['inputs'].get('ms_name', '?')}) "
                         "to keep iterating", key="rx_reopen_disk"):
                st.session_state["rx"] = last
                st.rerun()

    def _start():
        paras, marked = rx_mark_sections(ms_text)
        sig = _rwhash.sha1((ms_text + reviews + "".join(d["name"] for d in si_docs))
                           .encode("utf-8", "replace")).hexdigest()[:12]
        return {"sig": "rx_" + sig, "status": "running", "stages": {}, "log": [],
                "points_approved": False, "round": 1, "versions": [],
                "opts": {"journal": journal.strip(), "notes": notes.strip(),
                         "pause_points": pause_points, "ground": ground, "round_notes": []},
                "inputs": {"ms_name": ms_name, "ms_text": ms_text, "ms_paragraphs": paras,
                           "ms_marked": marked, "si_text": si_text, "si_docs": si_docs,
                           "reviews": reviews}}

    def _drive(s_):
        with st.status("Working on the response...", expanded=True) as box:
            try:
                rx_run(s_, box)
                box.update(label="Point list ready - review it below"
                           if s_["status"] == "awaiting_points" else "Response complete",
                           state="complete")
            except Exception as e:
                rw_log(s_, f"ERROR: {e}")
                s_["status"] = "error"
                rx_save_state(s_)
                box.update(label=f"Stopped: {e}", state="error")
                st.error(f"Stopped: {e}. Progress is saved - press Resume.")
        st.session_state["rx"] = s_

    b1, b2 = st.columns([2, 1])
    with b1:
        if state and state.get("status") in ("error", "running") and state["stages"]:
            if st.button("▶️ Resume", type="primary", key="rx_resume"):
                state["status"] = "running"
                _drive(state)
                st.rerun()
        elif st.button("📨 Draft the response", type="primary", key="rx_go",
                       disabled=not (ms_text and reviews.strip() and api_key.strip())):
            _drive(_start())
            st.rerun()
    with b2:
        if state and st.button("🗑️ Start over", key="rx_clear"):
            st.session_state.pop("rx", None)
            st.rerun()
    state = st.session_state.get("rx")
    if not state:
        return

    if state["status"] == "awaiting_points":
        st.markdown("---")
        st.markdown("## Reviewer points - awaiting your approval")
        st.caption("Untick a point to leave it out, fix the type/severity, and add a "
                   "note per point (your stance or your new data) - notes bind the response.")
        for p in state["stages"]["points"]:
            c1, c2 = st.columns([1, 6])
            with c1:
                p["answer"] = st.checkbox("Answer", value=p.get("answer", True),
                                          key=f"rx_ans_{p['id']}")
                p["severity"] = st.selectbox("Sev.", ["major", "minor"],
                                             index=0 if p.get("severity") == "major" else 1,
                                             key=f"rx_sev_{p['id']}")
            with c2:
                st.markdown(f"**{p['id']}** · reviewer {p['reviewer']} · {p['type']}  \n"
                            f"*{p['text']}*")
                p["note"] = st.text_input("Your note for this point", value=p.get("note", ""),
                                          key=f"rx_note_{p['id']}")
        if st.button("✅ Approve and draft responses", type="primary", key="rx_approve"):
            state["points_approved"] = True
            state["status"] = "running"
            rx_save_state(state)
            _drive(state)
            st.rerun()
        return
    if state["status"] != "complete":
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
        return

    stg = state["stages"]
    sig = state["sig"]
    st.markdown("---")
    m1, m2, m3, m4 = st.columns(4)
    pts = [p for p in stg["points"] if p.get("answer", True) is not False]
    n_si = sum(1 for c in stg["applied"] if c.get("doc", "manuscript") != "manuscript")
    m1.metric("Points answered", len(pts))
    m2.metric("Changes applied", len(stg["applied"]), f"{n_si} in supporting files" if n_si else None)
    m3.metric("Changes needing you", len(stg["skipped"]))
    m4.metric("[AUTHOR] items", len(RW_AUTHOR_RE.findall(stg["letter_md"] + stg["revised_md"])))
    if state.get("versions"):
        st.caption(f"Version {state['versions'][-1]['n']} ({state['versions'][-1]['label']}) · "
                   f"round {state.get('round', 1)}")
    si_names = [n for n, dd in (stg.get("si_diff") or {}).items()]
    tab_names = ["📨 Response letter", "🔀 Before / after", "📄 Revised manuscript"]
    if si_names:
        tab_names.append("📎 Supporting files")
    tab_names.append("🧭 Points & log")
    tabs = st.tabs(tab_names)
    with tabs[0]:
        st.markdown(RW_AUTHOR_RE.sub(lambda m: f"**{m.group(0)}**", stg["letter_md"]))
    with tabs[1]:
        if not stg["diff"]:
            st.info("No manuscript paragraph was changed automatically.")
        for d in stg["diff"]:
            st.markdown(d["marked"])
            st.markdown("---")
        if stg["skipped"]:
            st.markdown("**Proposed changes not applied (need you):**")
            st.dataframe([{"point": c.get("point"), "document": c.get("doc", "manuscript"),
                           "where": c.get("where"), "reason": c.get("reason"),
                           "find": str(c.get("find"))[:80],
                           "replace_with": str(c.get("replace_with"))[:120]}
                          for c in stg["skipped"]], use_container_width=True, hide_index=True)
    with tabs[2]:
        st.markdown(RW_AUTHOR_RE.sub(lambda m: f"**{m.group(0)}**", stg["revised_md"]))
    if si_names:
        with tabs[3]:
            for n in si_names:
                dd = stg["si_diff"].get(n) or []
                st.markdown(f"### {n} - {len(dd)} changed paragraph(s)")
                if not dd:
                    st.caption("No change proposed in this file.")
                for d in dd:
                    st.markdown(d["marked"])
                    st.markdown("---")
    with tabs[-1]:
        st.dataframe([{"id": p["id"], "reviewer": p["reviewer"], "type": p["type"],
                       "severity": p["severity"], "action": stg["responses"][p["id"]].get("action"),
                       "changes": len(stg["responses"][p["id"]].get("changes", [])),
                       "text": p["text"][:120]} for p in pts],
                     use_container_width=True, hide_index=True)
        if stg.get("iterations"):
            st.markdown("**Iterations**")
            st.dataframe([{"round": it["round"], "points": ", ".join(it["ids"]),
                           "instruction": it["instruction"][:160], "time": it["time"]}
                          for it in stg["iterations"]], use_container_width=True, hide_index=True)
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))

    # downloads
    docs0 = rx_docs(state)
    dl = st.columns(3)
    try:
        _tc = tracked_changes_bytes(
            "\n\n".join(docs0["manuscript"]), stg["revised_md"],
            title=f"Revised manuscript with tracked changes - {state['inputs'].get('ms_name', '')}",
            note="Word revisions applied to the manuscript as reviewed.")
        dl[0].download_button("⬇️ Manuscript with Word Track Changes", _tc,
                              file_name="revised_manuscript_tracked.docx",
                              mime="application/vnd.openxmlformats-officedocument."
                                   "wordprocessingml.document", key=f"rx_dl_tc_{sig}")
    except Exception as e:
        dl[0].caption(f"Tracked-changes export unavailable: {e}")
    p = Path(state.get("docx", ""))
    if p.exists():
        dl[1].download_button("⬇️ Word bundle (letter + changes + clean documents)",
                              p.read_bytes(), file_name=p.name,
                              mime="application/vnd.openxmlformats-officedocument."
                                   "wordprocessingml.document", key=f"rx_dl_{sig}")
    for i, n in enumerate(si_names):
        if not (stg["si_diff"].get(n) or []):
            continue
        try:
            _tc = tracked_changes_bytes("\n\n".join(docs0[n]), stg["si_revised_md"][n],
                                        title=f"{n} with tracked changes",
                                        note="Word revisions applied to the file as submitted.")
            dl[(i + 2) % 3].download_button(f"⬇️ {n} with Word Track Changes", _tc,
                                            file_name=re.sub(r"\.\w+$", "", n) + "_tracked.docx",
                                            mime="application/vnd.openxmlformats-officedocument."
                                                 "wordprocessingml.document", key=f"rx_dl_si_{sig}_{i}")
        except Exception as e:
            dl[(i + 2) % 3].caption(f"{n}: tracked-changes export unavailable: {e}")
    st.caption(f"Saved under answers/revision_jobs/{sig}/")

    # ---------------------------------------------------------- iteration
    st.markdown("---")
    st.markdown("## 🔁 Iterate on the response")
    st.caption("Edit any reply in place, or give an instruction and redo the point - the "
               "model revises that reply and its text changes, everything is re-applied "
               "from the original documents, the letter and the Word bundle are rebuilt, "
               "and the previous state is kept as a version. Numbers you type here count "
               "as sources. Free-text chat edits to the letter are overwritten by a rebuild, "
               "so make wording edits on the replies below.")
    round_note = st.text_area("New facts or notes for this round (optional; apply to every redo)",
                              key=f"rx_round_note_{sig}", height=80,
                              placeholder="e.g. New XPS data: Pb 4f shift of 0.3 eV (Fig. S14). "
                                          "Reviewer 2 is right about the FF; concede it.")

    def _after_change():
        for p_ in pts:
            st.session_state.pop(f"rx_edit_{sig}_{p_['id']}", None)
        st.session_state["rx"] = state
        st.rerun()

    for p in pts:
        r = stg["responses"][p["id"]]
        n_ch = sum(1 for c in stg["applied"] if c.get("point") == p["id"])
        n_sk = sum(1 for c in stg["skipped"] if c.get("point") == p["id"])
        flag = " · ✏️ edited" if r.get("edited") else (" · 🔁 revised" if r.get("revised_by_instruction") else "")
        with st.expander(f"{p['id']} · {p['type']} · {p['severity']} · {r.get('action', '')} · "
                         f"{n_ch} change(s) applied" + (f", {n_sk} need you" if n_sk else "") + flag):
            st.markdown(f"*{p['text']}*")
            st.text_area("Reply (edit freely)", value=str(r.get("response_md", "")),
                         key=f"rx_edit_{sig}_{p['id']}", height=160)
            for c in [c for c in stg["applied"] if c.get("point") == p["id"]]:
                st.caption(f"✔ [{c.get('doc', 'manuscript')}] {c.get('where', '')}: "
                           f"\"{str(c.get('replace_with', ''))[:140]}…\"")
            for c in [c for c in stg["skipped"] if c.get("point") == p["id"]]:
                st.caption(f"✘ [{c.get('doc', 'manuscript')}] {c.get('where', '')}: {c.get('reason', '')}")
            if r.get("author_needed"):
                st.caption(f"[AUTHOR] {r['author_needed']}")
            instr = st.text_input("Instruction to redo this point", key=f"rx_instr_{sig}_{p['id']}",
                                  placeholder="e.g. Be firmer - we disagree because the SI statistics "
                                              "show otherwise; quote Table S3. / Also add one sentence "
                                              "to Methods about the aperture mask.")
            if st.button("🔁 Redo this point", key=f"rx_redo_{sig}_{p['id']}",
                         disabled=not (instr.strip() and api_key.strip())):
                try:
                    with st.spinner(f"Revising {p['id']}..."):
                        rx_redo_points(state, [p["id"]], instr.strip(), round_note)
                except Exception as e:
                    st.error(f"Redo failed: {e}")
                else:
                    _after_change()

    i1, i2, i3 = st.columns([1, 1, 2])
    with i1:
        if st.button("💾 Save my edits & rebuild", key=f"rx_save_edits_{sig}",
                     help="Takes the reply texts above as edited, re-assembles the letter "
                          "and the Word bundle, keeps a version."):
            changed = 0
            for p in pts:
                v = st.session_state.get(f"rx_edit_{sig}_{p['id']}")
                r = stg["responses"][p["id"]]
                if v is not None and v.strip() != str(r.get("response_md", "")).strip():
                    r["response_md"] = v.strip()
                    r["edited"] = True
                    changed += 1
            rx_rebuild(state, f"manual edits ({changed} repl{'y' if changed == 1 else 'ies'})")
            st.toast(f"{changed} reply(ies) updated - version {state['versions'][-1]['n']}")
            _after_change()
    with i2:
        if st.button("📝 Rewrite letter opening / closing", key=f"rx_reletter_{sig}",
                     disabled=not api_key.strip()):
            try:
                with st.spinner("Rewriting the opening and closing..."):
                    summary = "\n".join(f"[{p['id']}] {stg['responses'][p['id']].get('action')}: "
                                        f"{str(stg['responses'][p['id']].get('response_md'))[:300]}"
                                        for p in pts)
                    umsg = (f"MANUSCRIPT TITLE / ID: {state['inputs'].get('ms_name', '')}\n\n"
                            f"POINT-BY-POINT MATERIAL:\n{summary[:40000]}\n\n"
                            f"CHANGES APPLIED: {len(stg['applied'])}; changes needing the authors: "
                            f"{len(stg['skipped'])}"
                            + (f"\n\nAUTHORS' NOTES:\n{round_note.strip()}" if round_note.strip() else ""))
                    stg["letter"] = rw_call(RX_LETTER_SYSTEM.replace(
                        "{JOURNAL}", state["opts"].get("journal") or "the journal"), umsg, 2500)
                    rx_assemble_letter(state)
                    rx_bundle(state)
                    rx_snapshot(state, "letter opening/closing rewritten")
                    rx_save_state(state)
            except Exception as e:
                st.error(f"Letter rewrite failed: {e}")
            else:
                _after_change()
    with i3:
        sel = st.multiselect("Redo several points with the round notes above",
                             [p["id"] for p in pts], key=f"rx_redo_sel_{sig}")
        if st.button("🔁 Redo selected points", key=f"rx_redo_many_{sig}",
                     disabled=not (sel and api_key.strip() and round_note.strip())):
            try:
                with st.spinner(f"Revising {', '.join(sel)}..."):
                    rx_redo_points(state, sel, "Revise according to the authors' notes from later rounds.",
                                   round_note)
            except Exception as e:
                st.error(f"Redo failed: {e}")
            else:
                _after_change()

    if state.get("versions"):
        with st.expander(f"🕘 Versions ({len(state['versions'])})"):
            st.dataframe([{"version": v["n"], "label": v["label"], "time": v["time"],
                           "applied": v["n_applied"], "need you": v["n_skipped"]}
                          for v in state["versions"]], use_container_width=True, hide_index=True)
            vsel = st.selectbox("Download a version", [f"v{v['n']} - {v['label']}" for v in state["versions"]],
                                index=len(state["versions"]) - 1, key=f"rx_ver_sel_{sig}")
            v = state["versions"][[f"v{x['n']} - {x['label']}" for x in state["versions"]].index(vsel)]
            vc = st.columns(2)
            vc[0].download_button("⬇️ Letter (Markdown)", v["letter_md"],
                                  file_name=f"response_letter_v{v['n']}.md", key=f"rx_ver_dl_l_{sig}")
            vc[1].download_button("⬇️ Revised manuscript (Markdown)", v["revised_md"],
                                  file_name=f"revised_manuscript_v{v['n']}.md", key=f"rx_ver_dl_m_{sig}")
            if st.button("↩️ Restore this version's replies as current", key=f"rx_ver_restore_{sig}",
                         help="Puts the letter and documents of that version back; a new version "
                              "is kept so nothing is lost."):
                stg["letter_md"] = v["letter_md"]
                stg["revised_paragraphs"] = [x for x in re.split(r"\n\s*\n", v["revised_md"]) if x.strip()]
                stg["diff"] = rx_diff_paragraphs(docs0["manuscript"], stg["revised_paragraphs"])
                for n, txt in (v.get("si_revised") or {}).items():
                    stg.setdefault("si_revised", {})[n] = [x for x in re.split(r"\n\s*\n", txt) if x.strip()]
                    stg.setdefault("si_diff", {})[n] = rx_diff_paragraphs(docs0.get(n, []), stg["si_revised"][n])
                rx_bundle(state)
                rx_snapshot(state, f"restored v{v['n']}")
                rx_save_state(state)
                _after_change()

    render_followup(
        "rx_" + sig, "Discuss and revise the response (free text)",
        {"letter": stg["letter_md"], "revised_manuscript": stg["revised_md"],
         **{f"revised {n}": t for n, t in (stg.get("si_revised_md") or {}).items()}},
        sources_text=("REVIEWS:\n" + state["inputs"]["reviews"][:20000] + "\n\nAUTHORS' NOTES:\n"
                      + state["opts"].get("notes", "")[:10000] + "\n"
                      + "\n".join(state["opts"].get("round_notes") or [])[:5000]
                      + "\n\nSUPPORTING FILES:\n" + rx_sources_text(state, 20000)),
        pool_texts=[state["inputs"]["ms_text"], state["inputs"].get("si_text", ""),
                    state["opts"].get("notes", "")] + list(state["opts"].get("round_notes") or []),
        apply_fn=lambda edits, pool: fu_apply_rx(state, edits, pool))


# ==========================================================================
# Proposal tools v2: ESR-calibrated mock evaluation, consistency audit,
# Gantt / work-package figures with tables.
# ==========================================================================
PROP_EVAL_DIR = ANSWERS_DIR / "proposal_eval"
PROP_FIG_DIR = ANSWERS_DIR / "proposal_figs"

ESR_CALIB_SYSTEM = """\
You distil the applicant's PAST Evaluation Summary Reports (ESRs) into an evaluator-calibration digest for {instrument} proposals. You receive excerpts from those ESRs (each identified by its file). Extract ONLY what the excerpts support - quote the evaluators' own words - and never generalise beyond them.
OUTPUT: ONLY a JSON object
{"recurring_weaknesses": [{"pattern": "one line naming the weakness type", "criterion": "Excellence|Impact|Implementation|other", "quotes": ["verbatim evaluator sentences"], "files": ["file names"]}],
 "credited_strengths": [{"pattern": "...", "quotes": ["..."], "files": ["..."]}],
 "scoring_habits": ["how these panels scored: e.g. 'Impact scored 3.5 when the pathway was generic although Excellence was 4.5'"],
 "threshold_language": ["phrases the evaluators used for below-threshold criteria"],
 "instrument_specific": ["expectations specific to this instrument that the ESRs reveal"],
 "n_reports": <number of distinct ESR files seen>}"""

PROP_EVAL_V2_SYSTEM = """\
You are an experienced European Commission expert evaluator writing an Evaluation Summary Report (ESR) for a {instrument} proposal.

Instrument criteria:
{criteria}

{calibration}

Write the ESR exactly as panels do. For EACH criterion: a score out of 5.00 (one decimal) and the threshold, then "Strengths:" bullets and "Weaknesses:" bullets. Weaknesses must be specific and quote or reference the proposal's own text; vague weaknesses ("could be clearer") are useless. Real panels punish: unquantified claims, generic impact statements, objectives without KPIs, missing risk mitigation, work plans whose effort does not match the ambition, and SoA sections an expert finds shallow or outdated. Then: TOTAL SCORE, a verdict against the thresholds, "Repeat offences" (weaknesses that reappear from the applicant's past ESRs, when a calibration digest is given - quote both), and "The 5 changes that would most raise this score", ranked.
Base everything on the provided text; never invent content the proposal does not contain. Note where the current call's work programme must be checked (criteria evolve between calls).

After the prose ESR, output the same judgement as a fenced json block:
```json
{"criteria": [{"name": "Excellence", "score": 3.5, "threshold": 3.0,
   "strengths": ["..."], "weaknesses": [{"text": "...", "quote": "<proposal text>", "fix": "one concrete change"}]}],
 "total": 10.5, "threshold_total": 10.0, "verdict": "one sentence",
 "repeat_offences": [{"past": "<quote from a past ESR>", "now": "<what recurs here>"}],
 "top_fixes": ["ranked, concrete"]}
```
Finish with <<<END ESR>>>."""

PROP_MODEL_SYSTEM = """\
You extract the implementation logic of a research proposal into a structured model, copying identifiers and titles verbatim and inventing nothing. Where the draft does not state a value use null. Months are project months (M1 = first month).
OUTPUT: ONLY a JSON object
{"duration_months": 36, "total_budget_eur": null,
 "objectives": [{"id": "O1", "text": "...", "kpis": ["value + unit + means of verification"]}],
 "work_packages": [{"id": "WP1", "title": "...", "lead": "partner", "start_month": 1, "end_month": 18, "person_months": 24,
    "objectives": ["O1"], "depends_on": ["WP2"], "tasks": [{"id": "T1.1", "title": "...", "start_month": 1, "end_month": 6}]}],
 "deliverables": [{"id": "D1.1", "title": "...", "wp": "WP1", "month": 12, "type": "report|demonstrator|data|other"}],
 "milestones": [{"id": "MS1", "title": "...", "month": 12, "wps": ["WP1"], "means_of_verification": "..."}],
 "risks": [{"id": "R1", "text": "...", "wps": ["WP2"], "likelihood": "low|medium|high", "impact": "low|medium|high", "mitigation": "..."}],
 "partners": [{"name": "...", "role": "...", "person_months": 30, "budget_eur": null}]}"""

PROP_CONSISTENCY_FIX_SYSTEM = """\
You are an expert proposal editor. You receive the structured model of a proposal's implementation and a list of consistency findings (orphan objectives, work packages without deliverables, effort mismatches, dates outside the project, missing verification means, risks without owners). For each finding write the concrete fix as ready-to-paste proposal text or a precise instruction (which table row, which sentence), using only entities that exist in the model - never invent partners, results or numbers. Markdown, one numbered item per finding, most severe first."""


# ------------------------------------------------------------- C: evaluation
def esr_calibration(instrument, refresh=False):
    """Calibration digest from the applicant's past ESRs (papers/evaluations
    folder in the index). Cached per instrument under answers/proposal_eval/.
    Returns (digest dict or None, hits)."""
    PROP_EVAL_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", instrument.lower())[:40]
    cache = PROP_EVAL_DIR / f"calibration_{slug}.json"
    if not refresh and cache.exists():
        try:
            rec = _rwjson.loads(cache.read_text(encoding="utf-8"))
            if rec.get("digest"):
                return rec["digest"], []
        except Exception:
            pass
    if not index_ok:
        return None, []
    hits, seen = [], set()
    for q in ("weaknesses of the proposal", "excellence weaknesses methodology",
              "impact weaknesses dissemination exploitation", "implementation work plan "
              "weaknesses resources", "score threshold criterion", instrument):
        try:
            for h in retrieve(q, 8, where_extra={"source": "evaluations"}):
                k = (h["meta"].get("file"), h["meta"].get("page_start"), h["text"][:60])
                if k not in seen:
                    seen.add(k)
                    hits.append(h)
        except Exception:
            continue
    if not hits:
        return None, []
    ctx = build_context(hits[:40])
    raw = rw_call(ESR_CALIB_SYSTEM.replace("{instrument}", instrument),
                  f"EXCERPTS FROM THE APPLICANT'S PAST EVALUATION REPORTS:\n\n{ctx}\n\n"
                  "Build the calibration digest now (JSON only).", 5000)
    digest = _rw_json_block(raw) or {}
    if digest:
        try:
            cache.write_text(_rwjson.dumps({"instrument": instrument, "digest": digest,
                                            "date": f"{datetime.datetime.now():%Y-%m-%d}",
                                            "files": sorted({h["meta"].get("file", "") for h in hits})},
                                           indent=1), encoding="utf-8")
        except Exception:
            pass
    return (digest or None), hits


def esr_calibration_text(digest):
    if not digest:
        return ("No calibration digest is available (no past ESRs indexed under "
                "papers/evaluations) - evaluate from the criteria alone.")
    lines = [f"CALIBRATION FROM THE APPLICANT'S {digest.get('n_reports', '?')} PAST ESR(s) - "
             "check whether these weaknesses recur and say so explicitly:"]
    for w in (digest.get("recurring_weaknesses") or [])[:12]:
        q = "; ".join(str(x) for x in (w.get("quotes") or [])[:2])
        lines.append(f"- [{w.get('criterion', '?')}] {w.get('pattern', '')}: \"{q[:300]}\"")
    if digest.get("scoring_habits"):
        lines.append("Scoring habits: " + " | ".join(str(x) for x in digest["scoring_habits"][:5]))
    if digest.get("threshold_language"):
        lines.append("Threshold language: " + " | ".join(str(x) for x in digest["threshold_language"][:5]))
    if digest.get("instrument_specific"):
        lines.append("Instrument-specific expectations: " + " | ".join(
            str(x) for x in digest["instrument_specific"][:5]))
    return "\n".join(lines)


def render_calibrated_evaluation(instrument, criteria, _prop_retrieve):
    st.caption("Mock ESR in the panel's format, with per-criterion scores against "
               "thresholds. Calibrated on your own past ESRs (papers/evaluations): "
               "the evaluator is told what earlier panels penalised and must flag "
               "repeat offences.")
    pe_up = st.file_uploader("Proposal draft (docx/pdf/txt/md)",
                             type=["docx", "pdf", "txt", "md"], key="pe_up")
    pe_ptxt, pe_pname = ("", "")
    if pe_up is None:
        pe_ptxt, pe_pname = project_file_picker("Proposal draft", "pe_proj_pick")
    c1, c2 = st.columns(2)
    with c1:
        use_cal = st.checkbox("Calibrate on my past ESRs", value=True,
                              disabled=not index_ok, key="pe_cal")
    with c2:
        if st.button("↻ Rebuild the calibration digest", key="pe_cal_refresh",
                     disabled=not (index_ok and api_key.strip())):
            with st.spinner("Reading your past evaluation reports..."):
                try:
                    d, _ = esr_calibration(instrument, refresh=True)
                    st.success("Digest rebuilt." if d else "No past ESRs found in the index.")
                except Exception as e:
                    st.error(f"Calibration failed: {e}")
    if st.button("Evaluate proposal", type="primary", key="pe_go",
                 disabled=(pe_up is None and not pe_ptxt)):
        if not api_key.strip():
            st.error("Needs the API key or Max backend (sidebar).")
        else:
            pe_name = pe_up.name if pe_up else pe_pname
            try:
                prop_text = (extract_uploaded_text(pe_up).strip() if pe_up else pe_ptxt.strip())
            except Exception as e:
                st.error(f"Could not read '{pe_name}': {e}")
                prop_text = ""
            if prop_text:
                prop_text = prop_text[:180_000]
                digest, cal_hits = (None, [])
                if use_cal:
                    with st.spinner("Calibrating on your past ESRs..."):
                        try:
                            digest, cal_hits = esr_calibration(instrument)
                        except Exception as e:
                            st.warning(f"Calibration skipped: {e}")
                hits = _prop_retrieve(prop_text[:1500])
                ctx = ""
                if hits:
                    ctx = ("\n\nEXCERPTS (papers and/or the applicant's past proposals & "
                           "evaluation reports - identify by file path):\n\n" + build_context(hits))
                sys_p = (PROP_EVAL_V2_SYSTEM.replace("{instrument}", instrument)
                         .replace("{criteria}", criteria)
                         .replace("{calibration}", esr_calibration_text(digest)))
                with st.spinner(f"{model_label} is evaluating..."):
                    try:
                        esr = rw_call(sys_p, f"THE PROPOSAL ('{pe_name}'):\n\n{prop_text}{ctx}", 9000)
                    except Exception as e:
                        st.error(f"Claude error: {e}")
                        esr = None
                if esr:
                    esr = esr.replace("<<<END ESR>>>", "").strip()
                    scores = _rw_json_block(esr) or {}
                    st.session_state["last_esr"] = {"esr": esr, "instrument": instrument,
                                                    "name": pe_name, "text": prop_text,
                                                    "scores": scores, "calibrated": bool(digest)}
                    qa = record_qa(f"[MOCK ESR - {instrument}] {pe_name}", esr,
                                   hits + cal_hits, do_autosave)
                    st.session_state["last_esr_qa"] = qa
    le = st.session_state.get("last_esr")
    if le:
        st.markdown("---")
        sc = le.get("scores") or {}
        if sc.get("criteria"):
            rows = []
            for c in sc["criteria"]:
                try:
                    s, t = float(c.get("score", 0)), float(c.get("threshold", 3))
                except (TypeError, ValueError):
                    s, t = 0.0, 3.0
                rows.append({"criterion": c.get("name"), "score": s, "threshold": t,
                             "status": "✅ above" if s >= t else "❌ below",
                             "weaknesses": len(c.get("weaknesses") or [])})
            st.dataframe(rows, use_container_width=True, hide_index=True)
            tot = sc.get("total")
            st.markdown(f"**Total {tot} / threshold {sc.get('threshold_total', '?')}** - "
                        f"{sc.get('verdict', '')}"
                        + ("  \n*Calibrated on your past ESRs.*" if le.get("calibrated") else ""))
            if sc.get("repeat_offences"):
                st.warning("Repeat offences vs your past evaluations:\n" + "\n".join(
                    f"- past: \"{str(r.get('past'))[:160]}\" → now: {str(r.get('now'))[:160]}"
                    for r in sc["repeat_offences"]))
            wk = [{"criterion": c.get("name"), "weakness": w.get("text"),
                   "proposal text": str(w.get("quote"))[:120], "fix": w.get("fix")}
                  for c in sc["criteria"] for w in (c.get("weaknesses") or [])
                  if isinstance(w, dict)]
            if wk:
                with st.expander(f"Weakness → fix table ({len(wk)})", expanded=True):
                    st.dataframe(wk, use_container_width=True, hide_index=True)
        st.markdown(_rw_strip_json(le["esr"]))
        qa = st.session_state.get("last_esr_qa")
        if qa:
            if qa.get("hits"):
                show_sources(qa["hits"], key_prefix="pe")
            st.download_button("⬇️ ESR as Word",
                               data=qa_to_docx_bytes([qa], title="Mock Evaluation"),
                               file_name=f"mock_ESR_{le['name'].rsplit('.', 1)[0]}.docx",
                               mime="application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document", key="pe_dl")
        st.markdown(f"**🛠️ Weakness-fix loop** - last evaluation: *{le['name']}* "
                    f"({le['instrument']}).")
        if st.button("Draft fixes for every weakness", key="esr_fix_go",
                     disabled=not api_key.strip()):
            fx_hits = _prop_retrieve(le["esr"][:1500], 8, 6)
            fx_ctx = (("\n\nEXCERPTS (cite as [n]):\n\n" + build_context(fx_hits))
                      if fx_hits else "")
            umsg = (f"THE PROPOSAL ('{le['name']}'):\n\n{le['text'][:120000]}\n\n"
                    f"THE MOCK EVALUATION REPORT:\n\n{le['esr']}{fx_ctx}")
            with st.spinner(f"{model_label} is drafting the fixes..."):
                try:
                    fixes = rw_call(PROP_FIXES_SYSTEM, umsg, 12000)
                except Exception as e:
                    st.error(f"Claude error: {e}")
                    fixes = None
            if fixes:
                record_qa(f"[ESR FIXES] {le['name']}", fixes, fx_hits, do_autosave)
                st.markdown("---")
                st.markdown(fixes)
        render_followup(
            "esr_" + _rwhash.sha1(le["text"][:5000].encode("utf-8", "replace")).hexdigest()[:10],
            "Discuss the evaluation and revise the proposal",
            {"proposal": le["text"], "esr": le["esr"]},
            sources_text="", pool_texts=[le["text"], le["esr"]],
            hint="Ask the evaluator to explain a score, argue back, or say \"revise the "
                 "proposal for weakness 2 of Impact\" - edits are applied to a working "
                 "copy of the proposal that you can download as Word. Numbers you state "
                 "here count as sources.")


# ------------------------------------------------------- D: consistency audit
def _pm_ids(model, key):
    return [str(x.get("id")) for x in (model.get(key) or []) if isinstance(x, dict) and x.get("id")]


def prop_consistency_checks(model):
    """Deterministic cross-checks over the extracted model. Returns
    (rows [{severity, check, item, detail}], coverage matrix markdown)."""
    rows = []
    dur = model.get("duration_months") or 0
    objs = [o for o in (model.get("objectives") or []) if isinstance(o, dict)]
    wps = [w for w in (model.get("work_packages") or []) if isinstance(w, dict)]
    dels = [d for d in (model.get("deliverables") or []) if isinstance(d, dict)]
    mss = [m for m in (model.get("milestones") or []) if isinstance(m, dict)]
    risks = [r for r in (model.get("risks") or []) if isinstance(r, dict)]
    partners = [p for p in (model.get("partners") or []) if isinstance(p, dict)]
    wp_ids = _pm_ids(model, "work_packages")
    obj_ids = _pm_ids(model, "objectives")

    def add(sev, check, item, detail=""):
        rows.append({"severity": sev, "check": check, "item": item, "detail": detail})

    # duplicates
    for key in ("objectives", "work_packages", "deliverables", "milestones", "risks"):
        ids = _pm_ids(model, key)
        for i in sorted({x for x in ids if ids.count(x) > 1}):
            add("MAJOR", "Duplicate identifier", i, f"appears {ids.count(i)} times in {key}")
    # objectives
    covered = {}
    for w in wps:
        for o in w.get("objectives") or []:
            covered.setdefault(str(o), []).append(str(w.get("id")))
    for o in objs:
        if not covered.get(str(o.get("id"))):
            add("MAJOR", "Objective addressed by no work package", str(o.get("id")),
                str(o.get("text", ""))[:120])
        if not (o.get("kpis") or []):
            add("MAJOR", "Objective without a KPI / means of verification", str(o.get("id")),
                str(o.get("text", ""))[:120])
    for oid, ws in covered.items():
        if oid not in obj_ids:
            add("MINOR", "Work package cites an unknown objective", oid, ", ".join(ws))
    # work packages
    del_by_wp, ms_by_wp, risk_by_wp = {}, {}, {}
    for d in dels:
        del_by_wp.setdefault(str(d.get("wp")), []).append(d)
    for m in mss:
        for w in m.get("wps") or []:
            ms_by_wp.setdefault(str(w), []).append(m)
    for r in risks:
        for w in r.get("wps") or []:
            risk_by_wp.setdefault(str(w), []).append(r)
    pm_total_wp = 0.0
    for w in wps:
        wid = str(w.get("id"))
        s, e = w.get("start_month"), w.get("end_month")
        if not (w.get("objectives") or []):
            add("MAJOR", "Work package linked to no objective", wid, str(w.get("title", ""))[:80])
        if not del_by_wp.get(wid):
            add("MAJOR", "Work package without a deliverable", wid, str(w.get("title", ""))[:80])
        if not ms_by_wp.get(wid):
            add("MINOR", "Work package without a milestone", wid, "")
        if not risk_by_wp.get(wid):
            add("MINOR", "Work package without an identified risk", wid, "")
        if not w.get("lead"):
            add("MINOR", "Work package without a lead partner", wid, "")
        if isinstance(s, (int, float)) and isinstance(e, (int, float)):
            if e < s:
                add("MAJOR", "Work package ends before it starts", wid, f"M{s}-M{e}")
            if dur and e > dur:
                add("MAJOR", "Work package runs past the project end", wid, f"M{e} > M{dur}")
        else:
            add("MINOR", "Work package without start/end months", wid, "")
        try:
            pm_total_wp += float(w.get("person_months") or 0)
        except (TypeError, ValueError):
            pass
        for dep in w.get("depends_on") or []:
            if str(dep) not in wp_ids:
                add("MINOR", "Dependency on an unknown work package", wid, str(dep))
            else:
                dw = next(x for x in wps if str(x.get("id")) == str(dep))
                if isinstance(dw.get("start_month"), (int, float)) and isinstance(s, (int, float)) \
                        and dw["start_month"] > s and isinstance(dw.get("end_month"), (int, float)) \
                        and dw["end_month"] > (e or 0):
                    add("MINOR", "Depends on a work package that ends later", wid,
                        f"{wid} depends on {dep} (M{dw['start_month']}-M{dw['end_month']})")
    # deliverables / milestones timing
    wp_span = {str(w.get("id")): (w.get("start_month"), w.get("end_month")) for w in wps}
    for d in dels:
        wid, m = str(d.get("wp")), d.get("month")
        if wid not in wp_ids:
            add("MAJOR", "Deliverable assigned to an unknown work package", str(d.get("id")), wid)
        elif isinstance(m, (int, float)):
            s, e = wp_span.get(wid, (None, None))
            if isinstance(e, (int, float)) and m > e:
                add("MAJOR", "Deliverable due after its work package ends", str(d.get("id")),
                    f"M{m} > M{e} ({wid})")
            if isinstance(s, (int, float)) and m < s:
                add("MAJOR", "Deliverable due before its work package starts", str(d.get("id")),
                    f"M{m} < M{s} ({wid})")
            if dur and m > dur:
                add("MAJOR", "Deliverable due after the project end", str(d.get("id")), f"M{m}")
        else:
            add("MINOR", "Deliverable without a due month", str(d.get("id")), "")
    for m in mss:
        if not (m.get("means_of_verification") or "").strip():
            add("MAJOR", "Milestone without means of verification", str(m.get("id")),
                str(m.get("title", ""))[:80])
        if not (m.get("wps") or []):
            add("MINOR", "Milestone linked to no work package", str(m.get("id")), "")
        mm = m.get("month")
        if isinstance(mm, (int, float)) and dur and mm > dur:
            add("MAJOR", "Milestone after the project end", str(m.get("id")), f"M{mm}")
    # risks
    for r in risks:
        if not (r.get("mitigation") or "").strip():
            add("MAJOR", "Risk without mitigation", str(r.get("id")), str(r.get("text", ""))[:80])
        for w in r.get("wps") or []:
            if str(w) not in wp_ids:
                add("MINOR", "Risk refers to an unknown work package", str(r.get("id")), str(w))
    # effort
    pm_total_partner = 0.0
    for p in partners:
        try:
            pm_total_partner += float(p.get("person_months") or 0)
        except (TypeError, ValueError):
            pass
    if pm_total_wp and pm_total_partner:
        diff = abs(pm_total_wp - pm_total_partner) / max(pm_total_wp, pm_total_partner)
        if diff > 0.05:
            add("MAJOR", "Effort mismatch: WP person-months vs partner person-months",
                f"{pm_total_wp:g} vs {pm_total_partner:g} PM", f"{diff * 100:.0f} % apart")
    # timeline gaps (months no WP is active)
    if wps and dur:
        active = set()
        for w in wps:
            s, e = w.get("start_month"), w.get("end_month")
            if isinstance(s, (int, float)) and isinstance(e, (int, float)):
                active.update(range(int(s), int(e) + 1))
        gaps = [m for m in range(1, int(dur) + 1) if m not in active]
        if gaps:
            add("MINOR", "Months with no active work package", f"{len(gaps)} month(s)",
                ", ".join(f"M{g}" for g in gaps[:12]))
    order = {"MAJOR": 0, "MINOR": 1}
    rows.sort(key=lambda r: order[r["severity"]])
    # coverage matrix
    if objs and wps:
        head = "| Objective | " + " | ".join(str(w.get("id")) for w in wps) + " | KPIs |"
        sep = "|---|" + "---|" * len(wps) + "---|"
        lines = [head, sep]
        for o in objs:
            oid = str(o.get("id"))
            cells = ["●" if oid in (w.get("objectives") or []) else "" for w in wps]
            lines.append(f"| {oid} | " + " | ".join(cells) + f" | {len(o.get('kpis') or [])} |")
        matrix = "\n".join(lines)
    else:
        matrix = "(no objectives or work packages extracted)"
    return rows, matrix


def render_consistency_audit(instrument):
    st.caption("Extracts the implementation logic of your draft (objectives, KPIs, work "
               "packages, tasks, deliverables, milestones, risks, effort, budget) into a "
               "structured model, then cross-checks it mechanically: every objective "
               "covered by a WP, every WP with deliverables, milestones, a risk and a "
               "lead; dates inside the project; effort totals that agree. The model "
               "also feeds the Gantt & WP figures.")
    ca_up = st.file_uploader("Proposal draft (docx/pdf/txt/md)",
                             type=["docx", "pdf", "txt", "md"], key="ca_up")
    ca_txt, ca_name = "", ""
    if ca_up is not None:
        try:
            ca_txt, ca_name = extract_uploaded_text(ca_up).strip(), ca_up.name
        except Exception as e:
            st.error(f"Could not read '{ca_up.name}': {e}")
    else:
        ca_txt, ca_name = project_file_picker("Proposal draft", "ca_proj_pick")
    if st.button("🔎 Extract model and audit", type="primary", key="ca_go",
                 disabled=not (ca_txt and api_key.strip())):
        with st.spinner(f"{model_label} is extracting the implementation model..."):
            try:
                raw = rw_call(PROP_MODEL_SYSTEM,
                              f"THE PROPOSAL DRAFT ('{ca_name}'):\n\n{ca_txt[:180000]}\n\n"
                              "Extract the model now (JSON only).", 9000)
                pm = _rw_json_block(raw) or {}
            except Exception as e:
                st.error(f"Claude error: {e}")
                pm = {}
        if pm:
            rows, matrix = prop_consistency_checks(pm)
            st.session_state["prop_model"] = pm
            st.session_state["prop_audit"] = {"rows": rows, "matrix": matrix, "name": ca_name}
            md = (f"# Consistency audit - {ca_name}\n\n"
                  f"*{sum(1 for r in rows if r['severity'] == 'MAJOR')} major · "
                  f"{sum(1 for r in rows if r['severity'] == 'MINOR')} minor findings*\n\n"
                  "| Severity | Check | Item | Detail |\n|---|---|---|---|\n"
                  + "\n".join(f"| {r['severity']} | {r['check']} | {r['item']} | "
                              f"{str(r['detail']).replace('|', '/')} |" for r in rows)
                  + f"\n\n## Objective × work-package coverage\n\n{matrix}\n\n"
                  f"## Extracted model\n\n```json\n{_rwjson.dumps(pm, indent=1)[:20000]}\n```")
            record_qa(f"[CONSISTENCY AUDIT - {instrument}] {ca_name}", md, [], do_autosave)
    au = st.session_state.get("prop_audit")
    if au:
        st.markdown("---")
        n_major = sum(1 for r in au["rows"] if r["severity"] == "MAJOR")
        (st.error if n_major else st.success)(
            f"{n_major} major and {len(au['rows']) - n_major} minor findings in {au['name']}")
        if au["rows"]:
            st.dataframe(au["rows"], use_container_width=True, hide_index=True)
        st.markdown("**Objective × work-package coverage**")
        st.markdown(au["matrix"])
        pm = st.session_state.get("prop_model") or {}
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🛠️ Draft fixes for the findings", key="ca_fix",
                         disabled=not (au["rows"] and api_key.strip())):
                with st.spinner("Drafting fixes..."):
                    try:
                        fx = rw_call(PROP_CONSISTENCY_FIX_SYSTEM,
                                     f"MODEL:\n{_rwjson.dumps(pm, indent=1)[:30000]}\n\nFINDINGS:\n"
                                     + "\n".join(f"- [{r['severity']}] {r['check']}: {r['item']} "
                                                 f"{r['detail']}" for r in au["rows"]), 6000)
                        st.session_state["prop_audit_fixes"] = fx
                    except Exception as e:
                        st.error(f"Claude error: {e}")
        with c2:
            st.caption("The extracted model is now available in 'Gantt & WP figures'.")
        if st.session_state.get("prop_audit_fixes"):
            st.markdown(st.session_state["prop_audit_fixes"])
        with st.expander("Extracted model (JSON)"):
            st.json(pm)
        render_followup(
            "ca_" + _rwhash.sha1(_rwjson.dumps(pm, sort_keys=True, default=str)
                                 .encode("utf-8")).hexdigest()[:10],
            "Discuss the audit",
            {"findings": "\n".join(f"- [{r['severity']}] {r['check']}: {r['item']} "
                                    f"{r['detail']}" for r in au["rows"]),
             "model": _rwjson.dumps(pm, indent=1)[:60000]},
            pool_texts=[_rwjson.dumps(pm)], downloads=False,
            hint="Ask why something was flagged or how to restructure the work plan; "
                 "edits here apply to the findings/model text only.")


# --------------------------------------------------- E: Gantt / WP figures
def _prop_fig_paths(sig, name):
    d = PROP_FIG_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", sig)[:40]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.png", d / f"{name}.svg", d / f"{name}.pdf"


def prop_gantt_source(model):
    wps = [[str(w.get("id")), str(w.get("title", ""))[:34], int(w.get("start_month") or 1),
            int(w.get("end_month") or model.get("duration_months") or 12),
            str(w.get("lead") or ""), float(w.get("person_months") or 0)]
           for w in model.get("work_packages") or [] if isinstance(w, dict)]
    mss = [[str(m.get("id")), str(m.get("title", ""))[:30], int(m.get("month") or 0),
            [str(x) for x in (m.get("wps") or [])]]
           for m in model.get("milestones") or [] if isinstance(m, dict) and m.get("month")]
    dls = [[str(d.get("id")), int(d.get("month") or 0), str(d.get("wp"))]
           for d in model.get("deliverables") or [] if isinstance(d, dict) and d.get("month")]
    dur = int(model.get("duration_months") or max([w[3] for w in wps] or [12]))
    return (
        "DATA = {}\n"
        f"WPS = {_rwjson.dumps(wps)}\n"
        f"MSS = {_rwjson.dumps(mss)}\n"
        f"DLS = {_rwjson.dumps(dls)}\n"
        f"DUR = {dur}\n"
        "def draw(fig, plt, np, mpl):\n"
        "    ax = fig.add_subplot(111)\n"
        "    n = len(WPS)\n"
        "    cols = ['#0072B2', '#009E73', '#E69F00', '#CC79A7', '#56B4E9', '#D55E00', '#8C4A2F', '#808080']\n"
        "    for y0 in range(0, DUR, 12):\n"
        "        if (y0 // 12) % 2 == 1:\n"
        "            ax.axvspan(y0, min(y0 + 12, DUR), color='#F3F3F3', zorder=0, linewidth=0)\n"
        "    ypos = {}\n"
        "    for i, wp in enumerate(WPS):\n"
        "        y = n - 1 - i\n"
        "        ypos[wp[0]] = y\n"
        "        ax.barh(y, wp[3] - wp[2] + 1, left=wp[2] - 1, height=0.56, color=cols[i % len(cols)],\n"
        "                edgecolor='#1A1A1A', linewidth=0.5, zorder=2)\n"
        "        if wp[5] > 0:\n"
        "            ax.text(wp[3] + 0.4, y, f'{wp[5]:g} PM', va='center', ha='left', fontsize=6.5, color='#4D4D4D')\n"
        "    for m in MSS:\n"
        "        ys = [ypos[w] for w in m[3] if w in ypos] or [n - 0.4]\n"
        "        for y in ys:\n"
        "            ax.plot(m[2], y, marker='D', markersize=5, color='#1A1A1A', zorder=4)\n"
        "        ax.annotate(m[0], (m[2], max(ys)), xytext=(0, 7), textcoords='offset points',\n"
        "                    ha='center', fontsize=6.5, color='#1A1A1A')\n"
        "    for d in DLS:\n"
        "        if d[2] in ypos:\n"
        "            ax.plot(d[1], ypos[d[2]] - 0.36, marker='^', markersize=4, color='#4D4D4D',\n"
        "                    markerfacecolor='white', zorder=4)\n"
        "    ax.set_yticks(list(range(n)))\n"
        "    ax.set_yticklabels([f'{WPS[n - 1 - i][0]}  {WPS[n - 1 - i][1]}' for i in range(n)], fontsize=7)\n"
        "    ax.set_xlim(0, DUR + 4)\n"
        "    ax.set_ylim(-0.8, n - 0.2)\n"
        "    step = 6 if DUR <= 48 else 12\n"
        "    ticks = list(range(0, DUR + 1, step))\n"
        "    ax.set_xticks(ticks)\n"
        "    ax.set_xticklabels([f'M{t}' for t in ticks], fontsize=7)\n"
        "    ax.set_xlabel('Project month')\n"
        "    ax.tick_params(axis='y', length=0)\n"
        "    ax.spines['left'].set_visible(False)\n"
        "    h1 = mpl.lines.Line2D([], [], marker='D', color='#1A1A1A', linestyle='none', markersize=5, label='Milestone')\n"
        "    h2 = mpl.lines.Line2D([], [], marker='^', color='#4D4D4D', markerfacecolor='white', linestyle='none', markersize=4, label='Deliverable')\n"
        "    ax.legend(handles=[h1, h2], loc='lower right', fontsize=6.5, ncol=2)\n")


def prop_wp_diagram_source(model):
    wps = [[str(w.get("id")), str(w.get("title", ""))[:40], int(w.get("start_month") or 1),
            int(w.get("end_month") or model.get("duration_months") or 12),
            str(w.get("lead") or ""), [str(x) for x in (w.get("depends_on") or [])]]
           for w in model.get("work_packages") or [] if isinstance(w, dict)]
    n = max(1, len(wps))
    per_row = 3 if n <= 6 else 4
    rows = (n + per_row - 1) // per_row
    H = 26 * (rows - 1) + 17 + 8          # square units: height_mm = width_mm * H / 100
    return (
        "DATA = {}\n"
        f"WPS = {_rwjson.dumps(wps)}\n"
        "def draw(fig, plt, np, mpl):\n"
        "    ax = fig.add_subplot(111)\n"
        "    ax.set_axis_off(); ax.set_xlim(0, 100)\n"
        "    n = len(WPS)\n"
        f"    per_row = {per_row}\n"
        f"    H = {H}\n"
        "    ax.set_ylim(0, H)\n"
        "    ax.set_aspect('equal')\n"
        "    w, h = 100 / per_row - 6, 17\n"
        "    pos = {}\n"
        "    cols = ['#DCEBF7', '#E8F4EA', '#FBF1DC', '#F5E6EF', '#E4F2FA', '#F9E4DA', '#EFE6E2', '#EEEEEE']\n"
        "    edges = ['#0072B2', '#009E73', '#E69F00', '#CC79A7', '#56B4E9', '#D55E00', '#8C4A2F', '#808080']\n"
        "    for i, wp in enumerate(WPS):\n"
        "        r, c = i // per_row, i % per_row\n"
        "        x = 3 + c * (100 / per_row)\n"
        "        y = H - 4 - h - r * 26\n"
        "        ax.add_patch(mpl.patches.FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0.6',\n"
        "                     facecolor=cols[i % len(cols)], edgecolor=edges[i % len(edges)], linewidth=1.0))\n"
        "        words = wp[1].split()\n"
        "        lines, cur = [], ''\n"
        "        for word in words:\n"
        "            if len(cur) + len(word) + 1 > 22 and cur:\n"
        "                lines.append(cur); cur = word\n"
        "            else:\n"
        "                cur = (cur + ' ' + word).strip()\n"
        "        if cur:\n"
        "            lines.append(cur)\n"
        "        title = chr(10).join(lines[:2])\n"
        "        ax.text(x + w / 2, y + h - 3.2, wp[0], ha='center', va='center', fontsize=8, fontweight='bold')\n"
        "        ax.text(x + w / 2, y + h / 2 - 0.5, title, ha='center', va='center', fontsize=7)\n"
        "        ax.text(x + w / 2, y + 2.6, f'M{wp[2]}-M{wp[3]}' + (f'  |  {wp[4]}' if wp[4] else ''),\n"
        "                ha='center', va='center', fontsize=6.5, color='#4D4D4D')\n"
        "        pos[wp[0]] = (x, y, w, h)\n"
        "    for wp in WPS:\n"
        "        for dep in wp[5]:\n"
        "            if dep in pos and wp[0] in pos:\n"
        "                x1, y1, w1, h1 = pos[dep]\n"
        "                x2, y2, w2, h2 = pos[wp[0]]\n"
        "                a = (x1 + w1 / 2, y1) if y1 > y2 else (x1 + w1, y1 + h1 / 2)\n"
        "                b = (x2 + w2 / 2, y2 + h2) if y1 > y2 else (x2, y2 + h2 / 2)\n"
        "                ax.add_patch(mpl.patches.FancyArrowPatch(a, b, arrowstyle='-|>', mutation_scale=9,\n"
        "                             linewidth=0.8, color='#4D4D4D', connectionstyle='arc3,rad=0.15',\n"
        "                             shrinkA=2, shrinkB=2))\n"), H


def prop_effort_source(model):
    wps = [[str(w.get("id")), float(w.get("person_months") or 0)]
           for w in model.get("work_packages") or [] if isinstance(w, dict)]
    return (
        "DATA = {}\n"
        f"WPS = {_rwjson.dumps(wps)}\n"
        "def draw(fig, plt, np, mpl):\n"
        "    ax = fig.add_subplot(111)\n"
        "    ids = [w[0] for w in WPS]\n"
        "    pm = [w[1] for w in WPS]\n"
        "    ax.bar(range(len(ids)), pm, color='#0072B2', edgecolor='#1A1A1A', linewidth=0.5, width=0.65)\n"
        "    for i, v in enumerate(pm):\n"
        "        ax.text(i, v + max(pm + [1]) * 0.02, f'{v:g}', ha='center', va='bottom', fontsize=7)\n"
        "    ax.set_xticks(range(len(ids))); ax.set_xticklabels(ids, fontsize=7)\n"
        "    ax.set_ylabel('Effort (person-months)')\n"
        "    ax.set_ylim(0, max(pm + [1]) * 1.18)\n")


def prop_workplan_docx(model, figs):
    doc = Document()
    doc.add_heading("Work plan", level=1)
    for name, label in (("gantt", "Gantt chart"), ("wp_diagram", "Work-package structure"),
                        ("effort", "Effort per work package")):
        p = figs.get(name)
        if p and Path(p).exists():
            doc.add_picture(str(p), width=Inches(_png_width_in(p)))
            doc.add_paragraph(label).runs[0].italic = True

    def table(head, rows):
        t = doc.add_table(rows=1, cols=len(head))
        t.style = "Table Grid"
        for j, c in enumerate(head):
            t.rows[0].cells[j].text = str(c)
            for r_ in t.rows[0].cells[j].paragraphs[0].runs:
                r_.font.bold = True
        for row in rows:
            cells = t.add_row().cells
            for j, v in enumerate(row):
                cells[j].text = "" if v is None else str(v)
        doc.add_paragraph()
    doc.add_heading("Work packages", level=2)
    table(["WP", "Title", "Lead", "Start", "End", "PM", "Objectives"],
          [[w.get("id"), w.get("title"), w.get("lead"), w.get("start_month"), w.get("end_month"),
            w.get("person_months"), ", ".join(str(x) for x in (w.get("objectives") or []))]
           for w in model.get("work_packages") or [] if isinstance(w, dict)])
    doc.add_heading("Deliverables", level=2)
    table(["ID", "Title", "WP", "Month", "Type"],
          [[d.get("id"), d.get("title"), d.get("wp"), d.get("month"), d.get("type")]
           for d in model.get("deliverables") or [] if isinstance(d, dict)])
    doc.add_heading("Milestones", level=2)
    table(["ID", "Title", "WPs", "Month", "Means of verification"],
          [[m.get("id"), m.get("title"), ", ".join(str(x) for x in (m.get("wps") or [])),
            m.get("month"), m.get("means_of_verification")]
           for m in model.get("milestones") or [] if isinstance(m, dict)])
    if model.get("partners"):
        doc.add_heading("Effort and budget per partner", level=2)
        table(["Partner", "Role", "Person-months", "Budget (EUR)"],
              [[p.get("name"), p.get("role"), p.get("person_months"), p.get("budget_eur")]
               for p in model.get("partners") or [] if isinstance(p, dict)])
    if model.get("risks"):
        doc.add_heading("Risk register", level=2)
        table(["ID", "Risk", "WPs", "Likelihood", "Impact", "Mitigation"],
              [[r.get("id"), r.get("text"), ", ".join(str(x) for x in (r.get("wps") or [])),
                r.get("likelihood"), r.get("impact"), r.get("mitigation")]
               for r in model.get("risks") or [] if isinstance(r, dict)])
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def prop_render_workplan_figures(model, sig, width_mm=160.0):
    """Deterministic figures (trusted code) through the figure sandbox:
    same style card, PNG + SVG + PDF. Returns {name: png path} and errors."""
    out, errors = {}, {}
    n_wp = max(1, len(model.get("work_packages") or []))
    wp_src, wp_H = prop_wp_diagram_source(model)
    specs = [("gantt", prop_gantt_source(model), min(120.0, 22 + 9 * n_wp)),
             ("wp_diagram", wp_src, min(180.0, width_mm * wp_H / 100.0)),
             ("effort", prop_effort_source(model), 55.0)]
    for name, src, h in specs:
        png, svg, pdf = _prop_fig_paths(sig, name)
        res = fig_render(src, png, svg, width="double", width_mm=width_mm, height_mm=h,
                         meta_desc=f"GrapheAI work-plan figure: {name}", pdf_path=pdf)
        if res.get("ok"):
            out[name] = str(png)
        else:
            errors[name] = res.get("error", "render failed")[:400]
    return out, errors


def _prop_model_from_editors(wp_df, ms_df, dl_df, dur):
    import pandas as pd
    model = {"duration_months": int(dur), "objectives": [], "deliverables": [],
             "milestones": [], "risks": [], "partners": []}
    wps = []
    for _, r in wp_df.iterrows():
        if not str(r.get("WP", "")).strip():
            continue
        deps = [x.strip() for x in str(r.get("Depends on", "") or "").split(",") if x.strip()]
        wps.append({"id": str(r["WP"]).strip(), "title": str(r.get("Title", "")),
                    "lead": str(r.get("Lead", "") or ""),
                    "start_month": int(pd.to_numeric(r.get("Start"), errors="coerce") or 1),
                    "end_month": int(pd.to_numeric(r.get("End"), errors="coerce") or dur),
                    "person_months": float(pd.to_numeric(r.get("PM"), errors="coerce") or 0),
                    "objectives": [], "depends_on": deps, "tasks": []})
    model["work_packages"] = wps
    for _, r in ms_df.iterrows():
        if str(r.get("ID", "")).strip():
            model["milestones"].append({"id": str(r["ID"]).strip(), "title": str(r.get("Title", "")),
                                        "month": int(pd.to_numeric(r.get("Month"), errors="coerce") or 0),
                                        "wps": [x.strip() for x in str(r.get("WPs", "") or "").split(",") if x.strip()],
                                        "means_of_verification": str(r.get("Verification", "") or "")})
    for _, r in dl_df.iterrows():
        if str(r.get("ID", "")).strip():
            model["deliverables"].append({"id": str(r["ID"]).strip(), "title": str(r.get("Title", "")),
                                          "wp": str(r.get("WP", "") or "").strip(),
                                          "month": int(pd.to_numeric(r.get("Month"), errors="coerce") or 0),
                                          "type": str(r.get("Type", "") or "")})
    return model


def render_workplan_figures():
    import pandas as pd
    st.caption("Gantt chart, work-package structure diagram and effort chart from one "
               "structured work plan - either the model extracted by the Consistency "
               "audit or tables you fill in here - plus the WP / deliverable / "
               "milestone / effort tables as a Word file. Deterministic drawing in the "
               "journal style card (no model call), 300-dpi PNG + SVG + PDF.")
    src = st.radio("Source", ["Model from the Consistency audit", "Tables I fill in here"],
                   horizontal=True, key="wpf_src",
                   index=0 if st.session_state.get("prop_model") else 1)
    model = None
    if src.startswith("Model"):
        model = st.session_state.get("prop_model")
        if not model:
            st.info("Run the Consistency audit first (it extracts the model), or fill in "
                    "the tables.")
    else:
        dur = st.selectbox("Duration (months)", [24, 30, 36, 42, 48, 60], index=2, key="wpf_dur")
        wp_default = pd.DataFrame([
            {"WP": "WP1", "Title": "Management and coordination", "Lead": "", "Start": 1,
             "End": dur, "PM": 6.0, "Depends on": ""},
            {"WP": "WP2", "Title": "", "Lead": "", "Start": 1, "End": 18, "PM": 24.0, "Depends on": ""},
            {"WP": "WP3", "Title": "", "Lead": "", "Start": 7, "End": 30, "PM": 24.0, "Depends on": "WP2"},
            {"WP": "WP4", "Title": "", "Lead": "", "Start": 19, "End": dur, "PM": 18.0, "Depends on": "WP3"},
        ])
        wp_df = st.data_editor(wp_default, num_rows="dynamic", use_container_width=True,
                               key="wpf_wp")
        c1, c2 = st.columns(2)
        with c1:
            ms_df = st.data_editor(pd.DataFrame([{"ID": "MS1", "Title": "", "Month": 12,
                                                  "WPs": "WP2", "Verification": ""}]),
                                   num_rows="dynamic", use_container_width=True, key="wpf_ms")
        with c2:
            dl_df = st.data_editor(pd.DataFrame([{"ID": "D2.1", "Title": "", "WP": "WP2",
                                                  "Month": 18, "Type": "report"}]),
                                   num_rows="dynamic", use_container_width=True, key="wpf_dl")
        model = _prop_model_from_editors(wp_df, ms_df, dl_df, dur)
    width_mm = st.select_slider("Figure width (mm)", [89, 120, 160, 170, 183], value=160,
                                key="wpf_w", help="160 mm fits the A4 text column of EU "
                                                  "templates; 183 mm is a journal double column.")
    if st.button("📊 Draw the work-plan figures", type="primary", key="wpf_go",
                 disabled=not (model and model.get("work_packages"))):
        sig = _rwhash.sha1(_rwjson.dumps(model, sort_keys=True, default=str)
                           .encode("utf-8")).hexdigest()[:10]
        with st.spinner("Rendering..."):
            figs, errors = prop_render_workplan_figures(model, sig, float(width_mm))
        st.session_state["wpf_last"] = {"figs": figs, "errors": errors, "model": model, "sig": sig}
    last = st.session_state.get("wpf_last")
    if last:
        for name, label in (("gantt", "Gantt chart"), ("wp_diagram", "Work-package structure"),
                            ("effort", "Effort per work package")):
            if name in last["figs"]:
                st.markdown(f"**{label}**")
                st.image(last["figs"][name], use_container_width=True)
                p = Path(last["figs"][name])
                d1, d2, d3 = st.columns(3)
                for col, ext in ((d1, "png"), (d2, "svg"), (d3, "pdf")):
                    fp = p.with_suffix(f".{ext}")
                    if fp.exists():
                        with col:
                            st.download_button(f"⬇️ {ext.upper()}", fp.read_bytes(),
                                               file_name=fp.name, key=f"wpf_dl_{name}_{ext}")
            elif name in last["errors"]:
                st.error(f"{label}: {last['errors'][name]}")
        try:
            buf = prop_workplan_docx(last["model"], last["figs"])
            st.download_button("⬇️ Work plan tables + figures (Word)", buf,
                               file_name=f"workplan_{last['sig']}.docx",
                               mime="application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document", key="wpf_docx")
        except Exception as e:
            st.warning(f"Word export failed: {e}")
        st.caption(f"Files under answers/proposal_figs/{last['sig']}/")


# ==========================================================================
# Follow-up conversation on an outcome: discuss the manuscript / article /
# proposal / ESR / response letter that a pipeline produced, point out what
# is wrong, add facts, and have exact edits applied to the working
# documents - with the same number guard as everywhere else (a number the
# model introduces must exist in the sources or in your own messages).
# ==========================================================================
FU_DIR = ANSWERS_DIR / "followups"

FU_SYSTEM = """\
You are the senior scientific editor who produced the WORKING DOCUMENT(S) below for the author, now in a follow-up conversation about them. The author may ask questions, say what is wrong, supply new facts or results, or ask for revisions - of a sentence, a section or the whole approach.

RULES
- Ground every statement in the working documents, the SOURCES given and the author's own messages. Never invent data, results, references or changes. A number you write must appear in the documents, the sources or the author's messages; otherwise write [AUTHOR: ...] for it.
- When the author asks for a change, MAKE it as exact edits (the harness applies them and rebuilds the document):
<<<EDITS>>>
[{"doc": "<document name>", "find": "<verbatim substring of that document, 30-400 characters, occurring exactly once>", "replace_with": "<the new text that replaces it>", "why": "<one line>"},
 {"doc": "<document name>", "section": "<the exact heading line of a section>", "replace_with": "<the complete new section, starting with its heading line>", "why": "<one line>"}]
<<<END EDITS>>>
Use "find" edits for local changes and "section" edits when a whole section must be rewritten. Keep everything that should stay; keep the author's voice, the citation markers and the [AUTHOR: ...] markers. Do not repeat the whole document in your reply.
- Before the block, reply briefly in prose: what you changed and why, or the answer to the question, or what you need from the author. If the author only asks a question, answer it and do not output an EDITS block.
- If the author gives a new result or fact, use it exactly as stated and say where it now appears.
- If a request conflicts with the evidence (e.g. inflating a number, dropping a hedge the data require), say so plainly and propose the honest version instead of complying silently."""


class _FuBox:
    def write(self, *a, **k):
        pass

    def update(self, *a, **k):
        pass


def fu_load(key):
    try:
        p = FU_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)[:60]}.json"
        if p.exists():
            return _rwjson.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def fu_save(key, fu):
    try:
        FU_DIR.mkdir(parents=True, exist_ok=True)
        p = FU_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)[:60]}.json"
        p.write_text(_rwjson.dumps(fu, default=str), encoding="utf-8")
    except Exception:
        pass


def fu_parse(raw):
    """(reply_text, edits list) from the model output."""
    block, _ = _rw_between(raw, "<<<EDITS>>>", "<<<END EDITS>>>")
    reply = raw
    edits = []
    if block is not None:
        reply = raw.split("<<<EDITS>>>")[0].strip()
        txt = re.sub(r"^```(json)?|```$", "", block.strip(), flags=re.M).strip()
        try:
            edits = _rwjson.loads(txt)
        except Exception:
            m = re.search(r"\[.*\]", txt, re.S)
            try:
                edits = _rwjson.loads(m.group(0)) if m else []
            except Exception:
                edits = []
        if isinstance(edits, dict):
            edits = edits.get("edits") or [edits]
        edits = [e for e in edits if isinstance(e, dict)]
    return reply.strip(), edits


def _fu_heading_key(line):
    return re.sub(r"^[#\s]*(\d+(\.\d+)*\.?\s*)?", "", line.strip()).strip().lower()


def fu_apply_parts(parts, edits, pool):
    """Apply edits to an ordered dict of text parts (name -> text). 'find'
    edits must match exactly once across all parts; 'section' edits replace
    the part whose heading matches (or the heading-to-next-heading span
    inside a part). New numbers must be in `pool`. Returns
    (parts, applied, skipped)."""
    applied, skipped = [], []
    for e in edits:
        repl = str(e.get("replace_with", ""))
        if e.get("section"):
            hk = _fu_heading_key(str(e["section"]))
            target, span = None, None
            for name, txt in parts.items():
                lines = txt.split("\n")
                for i, ln in enumerate(lines):
                    if ln.lstrip().startswith("#") and _fu_heading_key(ln) == hk:
                        level = len(ln) - len(ln.lstrip("#"))
                        j = i + 1
                        while j < len(lines):
                            l2 = lines[j]
                            if l2.lstrip().startswith("#") and \
                                    (len(l2) - len(l2.lstrip("#"))) <= level:
                                break
                            j += 1
                        target, span = name, (i, j)
                        break
                if target:
                    break
            if not target:
                skipped.append(dict(e, reason=f"section heading '{e['section']}' not found"))
                continue
            lines = parts[target].split("\n")
            old = "\n".join(lines[span[0]:span[1]])
            new_nums = rx_numbers_strict(repl) - rx_numbers_strict(old)
            bad = {n for n in new_nums if n not in pool}
            if bad:
                skipped.append(dict(e, reason="new number(s) not in the sources or your "
                                    "messages: " + ", ".join(sorted(bad))))
                continue
            if not repl.lstrip().startswith("#"):
                repl = lines[span[0]] + "\n\n" + repl
            parts[target] = "\n".join(lines[:span[0]] + [repl.rstrip("\n")] + lines[span[1]:])
            applied.append(e)
            continue
        find = str(e.get("find", ""))
        if len(find) < 12 or len(find) > 800 or find == repl:
            skipped.append(dict(e, reason="find text too short/long or identical"))
            continue
        hits = [n for n, t in parts.items() if find in t]
        total = sum(t.count(find) for t in parts.values())
        if total != 1:
            skipped.append(dict(e, reason=f"find text occurs {total} times (needs exactly 1)"))
            continue
        ok, bad = rx_number_ok(find, repl, pool)
        if not ok:
            skipped.append(dict(e, reason="new number(s) not in the sources or your "
                                "messages: " + ", ".join(sorted(bad))))
            continue
        parts[hits[0]] = parts[hits[0]].replace(find, repl, 1)
        applied.append(e)
    return parts, applied, skipped


def fu_pool(texts):
    pool = set()
    for t in texts:
        pool |= rx_numbers_strict(t or "")
    return pool


# ------------------------------------------------------------ host adapters
def fu_apply_rw(state, edits, pool):
    """Rewrite job: edits target 'manuscript' = front matter + sections.
    Rebuilds audit and bundle with no model call."""
    stg = state["stages"]
    parts = {"__front": stg["front"]}
    for s in stg["manifest"]["sections"]:
        parts[s["id"]] = stg["sections_out"][s["id"]]["text"]
    parts, applied, skipped = fu_apply_parts(parts, edits, pool)
    if applied:
        stg["front"] = parts.pop("__front")
        for sid, txt in parts.items():
            stg["sections_out"][sid]["text"] = txt
        stg.setdefault("followup_log", []).extend(
            f"- {e.get('why', '')}: \"{str(e.get('find') or e.get('section'))[:70]}…\"" for e in applied)
        for k in ("bundle", "audit", "audit_after", "checklist"):
            stg.pop(k, None)
        rw_run(state, _FuBox())
    return applied, skipped


def fu_apply_rv(state, edits, pool):
    stg = state["stages"]
    parts = {"__front": stg["front"]}
    for s in stg["manifest"]["sections"]:
        parts[s["id"]] = stg["sections_out"][s["id"]]["text"]
    parts, applied, skipped = fu_apply_parts(parts, edits, pool)
    if applied:
        stg["front"] = parts.pop("__front")
        for sid, txt in parts.items():
            stg["sections_out"][sid]["text"] = txt
        stg.pop("bundle", None)
        rv_run(state, _FuBox())
    return applied, skipped


def fu_apply_cp(state, edits, pool):
    stg = state["stages"]
    parts = {s["id"]: stg["sections_out"][s["id"]]["text"] for s in stg["manifest"]["sections"]}
    parts, applied, skipped = fu_apply_parts(parts, edits, pool)
    if applied:
        for sid, txt in parts.items():
            stg["sections_out"][sid]["text"] = txt
        stg.pop("bundle", None)
        cp_run(state, _FuBox())
    return applied, skipped


def fu_apply_rx(state, edits, pool):
    stg = state["stages"]
    parts = {"letter": stg["letter_md"], "revised_manuscript": "\n\n".join(stg["revised_paragraphs"])}
    for n, paras in (stg.get("si_revised") or {}).items():
        parts[f"revised {n}"] = "\n\n".join(paras)
    # an edit that names a document is applied inside that document only
    # (the letter quotes applied changes, so a find text may occur twice
    # across parts while being unique in its own document)
    applied, skipped = [], []
    for e in edits:
        want = str(e.get("doc") or "").strip().lower()
        target = next((n for n in parts if want and (want == n.lower() or want in n.lower()
                                                     or n.lower() in want)), None)
        if target:
            sub, a, s = fu_apply_parts({target: parts[target]}, [e], pool)
            parts[target] = sub[target]
        else:
            parts, a, s = fu_apply_parts(parts, [e], pool)
        applied += a
        skipped += s
    if applied:
        docs = rx_docs(state)
        stg["letter_md"] = parts["letter"]
        stg["revised_paragraphs"] = [p for p in re.split(r"\n\s*\n", parts["revised_manuscript"]) if p.strip()]
        stg["diff"] = rx_diff_paragraphs(docs["manuscript"], stg["revised_paragraphs"])
        for n in list((stg.get("si_revised") or {})):
            new = [p for p in re.split(r"\n\s*\n", parts[f"revised {n}"]) if p.strip()]
            stg["si_revised"][n] = new
            stg.setdefault("si_diff", {})[n] = rx_diff_paragraphs(docs.get(n, []), new)
        rx_bundle(state)
        rx_snapshot(state, "chat edit")
        rx_save_state(state)
    return applied, skipped


# ------------------------------------------------------------------ the UI
def render_followup(key, title, docs, sources_text="", pool_texts=(), apply_fn=None,
                    hint="", downloads=True):
    """docs: {name: text} - the current working documents (for hosts with
    apply_fn they are re-read from the job on every turn; otherwise the chat
    keeps its own revised copies and offers them for download)."""
    st.markdown("---")
    st.markdown(f"#### 💬 {title}")
    st.caption(hint or "Ask about the result, say what is wrong, add facts, or ask for "
                       "changes - edits are applied exactly and the document is rebuilt. "
                       "Numbers you state in the chat count as sources.")
    store = st.session_state.setdefault("_followups", {})
    fu = store.get(key) or fu_load(key) or {"messages": [], "docs": None, "user_texts": []}
    store[key] = fu
    if apply_fn is not None or fu.get("docs") is None:
        fu["docs"] = dict(docs)
    for m in fu["messages"][-30:]:
        with st.chat_message("user" if m["role"] == "user" else "assistant"):
            st.markdown(m["content"])
    msg = st.text_area("Your message", key=f"fu_in_{key}", height=90,
                       placeholder="e.g. The discussion overstates the stability result - "
                                   "hedge it and cite the SI statistics instead. / Our new "
                                   "certified value is 24.6 %; update the abstract. / "
                                   "Rewrite section 3 around the interface mechanism.")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        send = st.button("Send", type="primary", key=f"fu_send_{key}",
                         disabled=not (msg.strip() and api_key.strip()))
    with c2:
        if st.button("Clear chat", key=f"fu_clear_{key}"):
            fu["messages"], fu["user_texts"] = [], []
            if apply_fn is None:
                fu["docs"] = dict(docs)
            fu_save(key, fu)
            st.rerun()
    with c3:
        if downloads and apply_fn is None and fu.get("docs"):
            name = st.selectbox("Download revised", list(fu["docs"]), key=f"fu_dlsel_{key}",
                                label_visibility="collapsed")
            try:
                d = Document()
                md_to_docx(d, fu["docs"][name])
                b = io.BytesIO()
                d.save(b)
                st.download_button(f"⬇️ {name} (Word)", b.getvalue(),
                                   file_name=f"{name}_revised.docx", key=f"fu_dl_{key}")
            except Exception:
                pass
    if send:
        fu["messages"].append({"role": "user", "content": msg.strip()})
        fu["user_texts"].append(msg.strip())
        pool = fu_pool(list(pool_texts) + fu["user_texts"] + list(fu["docs"].values()))
        budget = 150000
        doc_txt = ""
        for name, txt in fu["docs"].items():
            take = txt[:max(4000, budget // max(1, len(fu["docs"])))]
            doc_txt += f"\n\n=== WORKING DOCUMENT '{name}' ===\n{take}"
            if len(take) < len(txt):
                doc_txt += f"\n[... {len(txt) - len(take)} more characters not shown]"
        hist = fu["messages"][-11:-1]
        hist_txt = "\n\n".join(f"{'AUTHOR' if m['role'] == 'user' else 'EDITOR'}: {m['content'][:3000]}"
                               for m in hist)
        umsg = (doc_txt + (f"\n\n=== SOURCES (the only permitted origin of numbers, besides "
                           f"the author's messages) ===\n{sources_text[:40000]}" if sources_text else "")
                + (f"\n\n=== CONVERSATION SO FAR ===\n{hist_txt}" if hist_txt else "")
                + f"\n\n=== AUTHOR'S NEW MESSAGE ===\n{msg.strip()}\n\nReply now.")
        try:
            with st.spinner("Thinking..."):
                raw = rw_call(FU_SYSTEM, umsg, 9000)
        except Exception as e:
            st.error(f"Claude error: {e}")
            fu["messages"].pop()
            fu["user_texts"].pop()
            return
        reply, edits = fu_parse(raw)
        note = ""
        if edits:
            try:
                if apply_fn is not None:
                    applied, skipped = apply_fn(edits, pool)
                else:
                    parts, applied, skipped = fu_apply_parts(dict(fu["docs"]), edits, pool)
                    fu["docs"] = parts
            except Exception as e:
                applied, skipped = [], [dict(x, reason=f"apply failed: {e}") for x in edits]
            note = f"\n\n*Applied {len(applied)} edit(s)"
            if skipped:
                note += f"; {len(skipped)} not applied: " + "; ".join(
                    f"\"{str(s.get('find') or s.get('section', ''))[:40]}…\" - {s.get('reason')}"
                    for s in skipped[:6])
            note += ".*"
        fu["messages"].append({"role": "assistant", "content": (reply or "(no reply text)") + note})
        fu_save(key, fu)
        st.rerun()


# ------------------------------------------------- Word Track Changes export
# A .docx whose differences against the original are real Word revisions
# (w:ins / w:del), so co-authors review with Word's Track Changes pane.
def _tc_plain(md_par):
    """Markdown paragraph -> (style, plain text)."""
    s = md_par.strip()
    m = re.match(r"^(#{1,4})\s+(.*)$", s)
    if m:
        return f"Heading {len(m.group(1))}", m.group(2).strip()
    s = re.sub(r"!\[.*?\]\([^)]*\)", "", s)                 # figure lines
    s = re.sub(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`", lambda x: x.group(1) or x.group(2) or x.group(3), s)
    return None, s.strip()


def _tc_run(par, text, kind, author, when, rev_id):
    """Append a normal / inserted / deleted run to a paragraph."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    if kind == "equal":
        par.add_run(text)
        return
    wrap = OxmlElement("w:ins" if kind == "insert" else "w:del")
    wrap.set(qn("w:id"), str(rev_id))
    wrap.set(qn("w:author"), author)
    wrap.set(qn("w:date"), when)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t" if kind == "insert" else "w:delText")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    wrap.append(r)
    par._p.append(wrap)


def _tc_mark_paragraph(par, kind, author, when, rev_id):
    """Mark a whole paragraph as inserted/deleted (paragraph mark revision)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = par._p.get_or_add_pPr()
    rPr = OxmlElement("w:rPr")
    mark = OxmlElement("w:ins" if kind == "insert" else "w:del")
    mark.set(qn("w:id"), str(rev_id))
    mark.set(qn("w:author"), author)
    mark.set(qn("w:date"), when)
    rPr.append(mark)
    pPr.append(rPr)


def tracked_changes_docx(before_pars, after_pars, title="", author="GrapheAI editor",
                         note=""):
    """Build a Document with Word revisions turning `before_pars` into
    `after_pars` (lists of markdown/plain paragraphs). Word-level diffs
    inside changed paragraphs; whole-paragraph insertions/deletions where
    text was added or removed. Returns the Document."""
    import difflib
    doc = Document()
    when = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = [1000]

    def nid():
        rid[0] += 1
        return rid[0]
    if title:
        doc.add_heading(title, level=1)
    if note:
        doc.add_paragraph(note).runs[0].italic = True
    b_clean = [_tc_plain(p) for p in before_pars if _tc_plain(p)[1]]
    a_clean = [_tc_plain(p) for p in after_pars if _tc_plain(p)[1]]
    b_txt = [t for _, t in b_clean]
    a_txt = [t for _, t in a_clean]
    sm = difflib.SequenceMatcher(a=b_txt, b=a_txt, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j1, j2):
                style, txt = a_clean[k]
                par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                par.add_run(txt)
        elif tag == "delete":
            for k in range(i1, i2):
                style, txt = b_clean[k]
                par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                _tc_run(par, txt, "delete", author, when, nid())
                _tc_mark_paragraph(par, "delete", author, when, nid())
        elif tag == "insert":
            for k in range(j1, j2):
                style, txt = a_clean[k]
                par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                _tc_run(par, txt, "insert", author, when, nid())
                _tc_mark_paragraph(par, "insert", author, when, nid())
        else:  # replace: pair paragraphs, word-level diff; extras as ins/del
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                bi, aj = i1 + k, j1 + k
                if bi < i2 and aj < j2:
                    style, atxt = a_clean[aj]
                    btxt = b_clean[bi][1]
                    par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                    aw, bw = btxt.split(), atxt.split()
                    wsm = difflib.SequenceMatcher(a=aw, b=bw, autojunk=False)
                    for wt, a1, a2, c1, c2 in wsm.get_opcodes():
                        if wt == "equal":
                            _tc_run(par, " ".join(aw[a1:a2]) + " ", "equal", author, when, 0)
                        elif wt == "delete":
                            _tc_run(par, " ".join(aw[a1:a2]) + " ", "delete", author, when, nid())
                        elif wt == "insert":
                            _tc_run(par, " ".join(bw[c1:c2]) + " ", "insert", author, when, nid())
                        else:
                            _tc_run(par, " ".join(aw[a1:a2]) + " ", "delete", author, when, nid())
                            _tc_run(par, " ".join(bw[c1:c2]) + " ", "insert", author, when, nid())
                elif bi < i2:
                    style, txt = b_clean[bi]
                    par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                    _tc_run(par, txt, "delete", author, when, nid())
                    _tc_mark_paragraph(par, "delete", author, when, nid())
                else:
                    style, txt = a_clean[aj]
                    par = doc.add_paragraph(style=style) if style else doc.add_paragraph()
                    _tc_run(par, txt, "insert", author, when, nid())
                    _tc_mark_paragraph(par, "insert", author, when, nid())
    return doc


def tracked_changes_bytes(before_text, after_text, title="", note=""):
    before = [p for p in re.split(r"\n\s*\n", before_text or "") if p.strip()]
    after = [p for p in re.split(r"\n\s*\n", after_text or "") if p.strip()]
    doc = tracked_changes_docx(before, after, title=title, note=note)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

# ------------------------------------------------------- submission packet
# Cover letter, editor summary, reviewer suggestions (profiles, never
# invented names), author statements, plus a mechanical check of the
# rewritten manuscript against the journal's limits.
JOURNAL_LIMITS = {
    # approximate, per the author guides as of RW_PROFILES_VERIFIED - verify
    "Nature family (Nature Energy / Materials / Communications)":
        {"main_words": 5000, "abstract_words": 150, "display_items": 8, "refs": 50,
         "title_chars": 75, "note": "Nature Energy Articles: ~3,000-5,000 words main text, "
                                     "abstract <= 150 words, <= 8 display items, ~50 refs; "
                                     "Communications differ - verify"},
    "Science / Science Advances":
        {"main_words": 4500, "abstract_words": 125, "display_items": 6, "refs": 40,
         "title_chars": 96, "note": "Science Research Articles: <= 4,500 words incl. refs, notes "
                                     "and captions; abstract <= 125 words; Advances up to 15,000"},
    "Cell Press (Joule / Matter)":
        {"main_words": 7000, "abstract_words": 150, "display_items": 8, "refs": 80,
         "title_chars": 100, "note": "Joule: Summary <= 150 words, Context & Scale ~150 words, "
                                      "no strict main-text limit, <= 8 display items typical"},
    "RSC Energy & Environmental Science":
        {"main_words": 8000, "abstract_words": 250, "display_items": 10, "refs": 120,
         "title_chars": 120, "note": "EES: no fixed length for papers; abstract <= 250 words"},
    "Wiley Advanced Materials / Advanced Energy Materials":
        {"main_words": 6000, "abstract_words": 200, "display_items": 8, "refs": 100,
         "title_chars": 120, "note": "Advanced Materials research articles: abstract <= 200 words; "
                                      "length flexible; ToC entry required"},
    "ACS Energy Letters / JACS":
        {"main_words": 3000, "abstract_words": 150, "display_items": 5, "refs": 60,
         "title_chars": 100, "note": "ACS Energy Letters Letters: ~3,000 words, <= 5 display "
                                      "items; JACS Articles are longer - verify"},
    "Generic high-impact journal":
        {"main_words": 5000, "abstract_words": 150, "display_items": 8, "refs": 60,
         "title_chars": 100, "note": "generic defaults"},
}

SUBMISSION_SYSTEM = """\
You prepare the submission packet for a manuscript going to {JOURNAL} ({ARTICLE_TYPE}), from the rewritten manuscript, its editorial plan (headline, positioning map) and the integrity summary. Everything you write must be supported by those texts; never invent results, names, affiliations, emails or prior correspondence - where the authors must supply something write [AUTHOR: ...].

Write, with these exact markdown headings:
## Cover letter to the editor
Addressed to the Editor (name as [AUTHOR: editor name]); 250-350 words: what the manuscript shows (the headline in the plan's words with its strongest number and qualifier), why it matters to this journal's readership specifically (name the journal's scope; no flattery), what is new relative to the closest precedents (use the positioning map's [Ln]/P-ID papers by their titles - never invent), the statements the journal requires (not under consideration elsewhere, all authors approved, competing interests [AUTHOR: confirm]), and a closing line. Signed [AUTHOR: corresponding author, affiliation].
## Editor summary (100 words)
For a non-specialist editor: problem, advance, evidence, implication - only ledger-backed numbers.
## Why this journal
3-4 sentences mapping the paper to the journal profile's stated scope and to the display-item strategy.
## Suggested reviewers
5 reviewer PROFILES: expertise needed, why, and which cited paper's author group fits ("corresponding author of [L3] / ref [12]" by title) - names, emails and affiliations only as [AUTHOR: fill in]; note conflicts to avoid (recent co-authors, same institution).
## Reviewers to exclude
A template with [AUTHOR: ...] and the reasons journals accept (direct competitors, conflicts).
## Author contributions (CRediT)
Roles listed with [AUTHOR: initials] placeholders, derived from what the manuscript actually did (synthesis, device fabrication, characterisation named in Methods pointer, analysis, writing).
## Statements
Competing interests, data availability (consistent with the reporting checklist: say what exists), code availability, funding [AUTHOR: grant numbers], acknowledgements template.
## Pre-submission checklist
Items from the limits check below marked pass/fail plus the journal-specific items of the profile (ToC graphic, highlights, Context & Scale etc.), each as a checkbox line.
Finish with <<<END PACKET>>>."""


def sub_limits_check(state):
    """Mechanical comparison of the rewritten manuscript with the journal
    limits (approximate; the profile's note says what to verify)."""
    stg, opts = state["stages"], state["opts"]
    lim = JOURNAL_LIMITS.get(opts["journal"], JOURNAL_LIMITS["Generic high-impact journal"])
    md = stg.get("manuscript_md", "")
    front = stg.get("front", "")
    body = "\n\n".join(stg["sections_out"][s["id"]]["text"] for s in stg["manifest"]["sections"])
    body_plain = RW_AUTHOR_RE.sub("", FIG_IMG_RE.sub("", re.sub(r"^#.*$", "", body, flags=re.M)))
    main_words = len(body_plain.split())
    m = re.search(r"## Abstract\s*\n(.*?)(?=\n## |\Z)", front, re.S)
    abstract_words = len(m.group(1).split()) if m else 0
    t = re.search(r"^# (.+)$", front, re.M)
    title_chars = len(t.group(1).strip()) if t else 0
    labels = {lab for k, lab in _rw_labels(md) if k == "fig"}
    n_display = len(labels) + len({lab for k, lab in _rw_labels(md) if k == "table"})
    n_display += sum(1 for f in (stg.get("figures") or {}).values() if f.get("ok"))
    refs = {c for c in _rw_citations(md) if c.isdigit()}
    n_refs = (max(int(c) for c in refs) if refs else 0) + len(rw_lib_cited(md))
    rows = [
        {"item": "Main text words (excl. front matter, methods, refs)", "value": main_words,
         "limit": lim["main_words"], "status": "OK" if main_words <= lim["main_words"] else "OVER"},
        {"item": "Abstract words", "value": abstract_words, "limit": lim["abstract_words"],
         "status": "OK" if 0 < abstract_words <= lim["abstract_words"] else ("MISSING" if not abstract_words else "OVER")},
        {"item": "Display items (figures + tables incl. proposed)", "value": n_display,
         "limit": lim["display_items"], "status": "OK" if n_display <= lim["display_items"] else "OVER"},
        {"item": "References (highest marker + library additions)", "value": n_refs,
         "limit": lim["refs"], "status": "OK" if n_refs <= lim["refs"] else "OVER"},
        {"item": "Title characters", "value": title_chars, "limit": lim["title_chars"],
         "status": "OK" if 0 < title_chars <= lim["title_chars"] else ("MISSING" if not title_chars else "OVER")},
    ]
    return rows, lim["note"]


def render_submission_packet(state):
    """Results view (rewrite): limits table + one-call packet with download."""
    stg = state["stages"]
    st.markdown("#### 📮 Submission packet")
    rows, note = sub_limits_check(state)
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"Limits are approximate ({note}); verified {RW_PROFILES_VERIFIED} - "
               "confirm against the current author guide.")
    key = f"sub_{state['sig']}"
    if st.button("✉️ Write the submission packet (cover letter, editor summary, reviewer "
                 "profiles, statements)", key=key + "_go", disabled=not api_key.strip()):
        plan = _rw_strip_json(stg["plan"])
        audit = stg.get("audit_after") or stg.get("audit") or {"counts": {}}
        chk = stg.get("checklist") or {"rows": []}
        umsg = (f"=== REWRITTEN MANUSCRIPT ===\n{stg['manuscript_md'][:90000]}\n\n"
                f"=== EDITORIAL PLAN (headline, positioning map, decisions) ===\n{plan[:25000]}\n\n"
                f"=== JOURNAL PROFILE ===\n{JOURNAL_PROFILES[state['opts']['journal']]['text']}\n\n"
                f"=== LIMITS CHECK ===\n" + "\n".join(
                    f"- {r['item']}: {r['value']} / {r['limit']} -> {r['status']}" for r in rows)
                + "\n\n=== INTEGRITY SUMMARY ===\nmechanical flags: "
                f"{audit.get('counts')}; reporting checklist: "
                + "; ".join(f"{r['item']}: {r['status']}" for r in chk.get("rows", []))
                + "\n\nWrite the submission packet now.")
        sysm = (SUBMISSION_SYSTEM.replace("{JOURNAL}", state["opts"]["journal"])
                .replace("{ARTICLE_TYPE}", state["opts"]["article_type"]))
        try:
            with st.spinner("Writing the packet..."):
                out = rw_call(sysm, umsg, 6000).replace("<<<END PACKET>>>", "").strip()
            stg["submission_packet"] = out
            rw_save_state(state)
            record_qa(f"[SUBMISSION PACKET - {state['opts']['journal']}] "
                      f"{state['inputs']['ms_name']}", out, [], do_autosave)
        except Exception as e:
            st.error(f"Claude error: {e}")
    if stg.get("submission_packet"):
        st.markdown(RW_AUTHOR_RE.sub(lambda m: f"**{m.group(0)}**", stg["submission_packet"]))
        d = Document()
        d.add_heading("Submission packet", level=1)
        md_to_docx(d, stg["submission_packet"])
        b = io.BytesIO()
        d.save(b)
        st.download_button("⬇️ Submission packet (Word)", b.getvalue(),
                           file_name="submission_packet.docx", key=key + "_dl")

# ------------------------------------------- watch alerts vs open jobs
# New papers found by the Watch tab are checked against the manuscripts,
# articles, proposals and responses in progress: which job they touch,
# which claim, and what to do about it.
WATCH_IMPACT_SYSTEM = """\
You are the author's research editor. You receive NEW PAPERS found by a literature watch (title, journal, year, abstract when available) and the author's OPEN JOBS (documents in progress: a manuscript rewrite with its headline, a review with its thesis, a proposal with its core message, a response letter). For each paper decide whether it matters for any open job, and how:
- "supports": strengthens a claim (cite it);
- "competes": prior or concurrent work that reduces a novelty/priority claim (must be cited and positioned against);
- "contradicts": evidence against a claim (address it);
- "method": a protocol, standard or analysis the job should adopt or mention;
- "none".
Judge only from the given texts; say "uncertain" when the abstract is missing. Never invent findings of the paper.
OUTPUT: ONLY a JSON object {"impacts": [{"paper": "<title>", "job": "<job id>", "relation": "supports|competes|contradicts|method|none", "confidence": "high|medium|low|uncertain", "touches": "<the claim/sentence of the job it touches, quoted or paraphrased>", "action": "<one concrete instruction: cite in Introduction paragraph 2; soften 'first' in the abstract; add to positioning map; ignore>"}]} - include every paper once with its most relevant job (relation none if nothing)."""


def watch_open_jobs():
    """Summaries of jobs in progress or recently completed (last 60 days)."""
    import time
    jobs = []
    cutoff = time.time() - 60 * 86400
    for d, kind in ((RW_DIR, "manuscript rewrite"), (RV_DIR, "review/proposal"),
                    (RX_DIR, "response to reviewers")):
        try:
            for p in d.glob("*/state.json"):
                if p.stat().st_mtime < cutoff:
                    continue
                s = _rwjson.loads(p.read_text(encoding="utf-8"))
                stg, opts = s.get("stages", {}), s.get("opts", {})
                man = stg.get("manifest", {}) or {}
                if kind == "manuscript rewrite":
                    label = s.get("inputs", {}).get("ms_name", p.parent.name)
                    focus = man.get("headline") or man.get("title_recommended") or ""
                    extra = f"journal {opts.get('journal', '')}"
                elif kind == "response to reviewers":
                    label = s.get("inputs", {}).get("ms_name", p.parent.name)
                    focus = (s.get("inputs", {}).get("reviews", "") or "")[:400]
                    extra = "revision in progress"
                else:
                    label = (opts.get("synopsis") or opts.get("brief") or p.parent.name)[:120]
                    focus = man.get("thesis") or man.get("core_message") or ""
                    extra = opts.get("venue") or opts.get("doc_type") or ""
                sec_heads = "; ".join(str(x.get("heading", "")) for x in (man.get("sections") or [])[:10])
                jobs.append({"id": f"{kind[:3].upper()}-{p.parent.name[:10]}", "kind": kind,
                             "label": label, "focus": focus, "extra": extra,
                             "sections": sec_heads, "status": s.get("status", "")})
        except Exception:
            continue
    return jobs


def render_watch_impact(found):
    """Button + table under the Watch results."""
    jobs = watch_open_jobs()
    if not jobs:
        st.caption("No manuscripts, reviews, proposals or responses in progress in the "
                   "last 60 days - nothing to check the new papers against.")
        return
    st.markdown(f"**🧭 {len(jobs)} document(s) in progress** - check whether the new papers "
                "touch them.")
    if st.button("Check the new papers against my open documents", key="watch_impact_go",
                 disabled=not api_key.strip()):
        papers = "\n\n".join(
            f"- TITLE: {w.get('title', '?')}\n  JOURNAL: {w.get('journal', '?')} ({w.get('year', '?')})"
            + (f"\n  ABSTRACT: {str(w.get('abstract'))[:1200]}" if w.get("abstract") else
               "\n  ABSTRACT: (not available)")
            for _t, w in found[:40])
        jobs_txt = "\n\n".join(
            f"- JOB {j['id']} ({j['kind']}; {j['extra']}; status {j['status']}): {j['label']}\n"
            f"  FOCUS: {j['focus'][:600]}\n  SECTIONS: {j['sections'][:400]}" for j in jobs)
        try:
            with st.spinner("Reading the new papers against your open documents..."):
                raw = rw_call(WATCH_IMPACT_SYSTEM,
                              f"NEW PAPERS:\n{papers}\n\nOPEN JOBS:\n{jobs_txt}\n\n"
                              "Assess the impacts now (JSON only).", 6000)
            imp = (_rw_json_block(raw) or {}).get("impacts") or []
            st.session_state["watch_impacts"] = [i for i in imp if isinstance(i, dict)]
            record_qa("[WATCH IMPACT] new papers vs open documents",
                      "\n".join(f"- {i.get('paper')} -> {i.get('job')} ({i.get('relation')}, "
                                f"{i.get('confidence')}): {i.get('action')}"
                                for i in st.session_state["watch_impacts"]), [], do_autosave)
        except Exception as e:
            st.error(f"Claude error: {e}")
    imps = st.session_state.get("watch_impacts")
    if imps:
        hot = [i for i in imps if i.get("relation") not in (None, "none")]
        (st.warning if hot else st.success)(
            f"{len(hot)} of {len(imps)} new paper(s) touch a document in progress."
            if hot else "None of the new papers touches a document in progress.")
        if hot:
            st.dataframe([{"paper": str(i.get("paper"))[:80], "job": i.get("job"),
                           "relation": i.get("relation"), "confidence": i.get("confidence"),
                           "touches": str(i.get("touches"))[:120], "action": i.get("action")}
                          for i in hot], use_container_width=True, hide_index=True)


# ==========================================================================
# Check my revision - audit of an author-made revision + response letter
# against the reviewer reports: point coverage, claimed changes verified
# in the actual diff, unrequested changes, unsourced numbers, letter
# consistency and tone, simulated second-round reviewer reaction.
# ==========================================================================
RC_DIR = ANSWERS_DIR / "revision_checks"

RC_MAP_SYSTEM = """\
You map an authors' response letter onto the numbered reviewer points. You receive the points (id, reviewer, text) and the full response letter. For each point find the passage of the letter that answers it (verbatim excerpt, up to 900 characters; empty if the letter never addresses it) and list the concrete claims the authors make about changes ("we added ...", "we now show ...", "Section 3.2 was rewritten", "new Fig. S12"), each with the location the authors name.
OUTPUT: ONLY a JSON object {"responses": [{"id": "R1.1", "found": true, "excerpt": "...", "claims": [{"claim": "one sentence", "location": "as named by the authors, or empty"}], "stance": "accepted | partially accepted | rebutted | deferred | unclear"}]}"""

RC_AUDIT_SYSTEM = """\
You are a meticulous handling editor checking an authors' revision before it goes back to the reviewers. For each reviewer point you receive: the point, the authors' response excerpt and claims, and the ACTUAL CHANGES between the manuscript as reviewed and the revised manuscript (numbered diff blocks: before / after), plus the supporting files if given. Judge only from this material; never assume a change exists because the letter says so.

For each point decide:
- verdict: "addressed" (response adequate and every claimed change is visible in the diff or the revised text), "partial" (addressed in part, or a claimed change is weaker than claimed), "claim_not_found" (the letter claims a change that the diff and the revised manuscript do not contain), "rebuttal_ok" (the authors decline with evidence a fair reviewer would accept), "rebuttal_weak" (decline without adequate evidence or with a tone that will irritate), "not_addressed" (no response, or a response that ignores what was asked).
- evidence: the diff block numbers and a short quote from the revised text that support the verdict (or "no matching change" for claim_not_found).
- reviewer_satisfied: your honest probability (0-1) that this reviewer accepts the response as is.
- issues: concrete problems (missing location pointer, number in the letter absent from the manuscript, over-claiming, defensive wording, requested item silently skipped ...).
- fix: the single most useful concrete fix (what to add to the letter or the manuscript), or empty.
OUTPUT: ONLY a JSON object {"audits": [{"id": "R1.1", "verdict": "...", "evidence": "...", "reviewer_satisfied": 0.7, "issues": ["..."], "fix": "..."}]}"""

RC_DIFFMAP_SYSTEM = """\
You receive numbered change blocks (before / after) between a manuscript as reviewed and its revision, and the list of reviewer points. For each block name the point ids it serves (empty list if the change answers no reviewer request: an unrequested change the authors must declare to the editor), whether it introduces a new claim, result or number ("new_content": true/false), and a 6-12 word description.
OUTPUT: ONLY a JSON object {"blocks": [{"n": 1, "points": ["R1.1"], "new_content": false, "what": "..."}]}"""

RC_SUMMARY_SYSTEM = """\
You are the handling editor of {JOURNAL} writing an internal pre-check of a revised manuscript and its response letter before sending them back to the reviewers, and simulating each reviewer's second-round reaction. You receive the per-point audit table, the deterministic checks (unrequested changes, numbers in the letter absent from the manuscript, new numbers in the revision without a source, figure/table references that do not resolve, tone counts) and the letter's opening.
Write, in markdown:
## Editor's verdict
Predicted outcome after this round (accept / minor revision / major revision / reject) with a one-paragraph justification citing point ids.
## Reviewer 1 (and 2, 3 ...) - likely reaction
For each reviewer: two to five sentences in the reviewer's voice on what satisfies them and what still does not, naming point ids.
## Ranked fixes before submission
Numbered list, most important first, each one concrete (what to write where), covering: claims not found in the manuscript, points not addressed, weak rebuttals, unrequested changes to declare, numbers to reconcile, tone. At most 12 items.
## Letter tone
Three sentences on the letter's tone with one quoted phrase to change if any.
Base everything on the provided material; never invent a reviewer request or a change."""


def rc_save_state(state):
    try:
        d = RC_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(_rwjson.dumps(state, default=str), encoding="utf-8")
    except Exception:
        pass


def rc_load_last_state():
    try:
        cands = sorted(RC_DIR.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            return _rwjson.loads(cands[0].read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


RC_FIGREF_RE = re.compile(r"\b(?:Fig(?:ure)?s?\.?|Table|Supplementary (?:Fig(?:ure)?|Table|Note))\s?(S?\d+[a-z]?)",
                          re.IGNORECASE)
RC_DEFENSIVE = ("the reviewer is wrong", "the reviewer is mistaken", "fails to understand",
                "misunderstands", "clearly", "obviously", "as any expert knows", "it is well known",
                "we strongly disagree", "the reviewer did not read", "trivial", "unfounded",
                "we do not see the point", "this criticism is not valid")
RC_OBSEQUIOUS = ("we are extremely grateful", "invaluable", "insightful comments", "we deeply appreciate",
                 "we sincerely thank", "excellent suggestion", "the reviewer is absolutely right",
                 "we are very grateful", "we humbly")


def _rc_figrefs(text):
    out = set()
    for m in RC_FIGREF_RE.finditer(text or ""):
        out.add(m.group(1).upper())
    return out


def rc_deterministic(state):
    """Diff, numbers and references that need no model."""
    inp, stg = state["inputs"], state["stages"]
    orig = list(inp["orig_paragraphs"])
    rev = list(inp["rev_paragraphs"])
    diffs = rx_diff_paragraphs(orig, rev)
    src_pool = rx_numbers_strict(inp["orig_text"]) | rx_numbers_strict(inp.get("si_text", "")) \
        | rx_numbers_strict(inp.get("notes", "")) | rx_numbers_strict(inp["letter"])
    rev_new = rx_numbers_strict(inp["rev_text"]) - rx_numbers_strict(inp["orig_text"])
    unsourced = sorted(n for n in rev_new if n not in src_pool)
    letter_nums = rx_numbers_strict(inp["letter"])
    ms_pool = rx_numbers_strict(inp["rev_text"]) | rx_numbers_strict(inp.get("si_text", "")) \
        | rx_numbers_strict(inp.get("notes", "")) | rx_numbers_strict(inp["reviews"])
    letter_missing = sorted(n for n in letter_nums if n not in ms_pool)
    refs_letter = _rc_figrefs(inp["letter"])
    refs_docs = _rc_figrefs(inp["rev_text"]) | _rc_figrefs(inp.get("si_text", ""))
    refs_missing = sorted(r for r in refs_letter if r not in refs_docs)
    low = inp["letter"].lower()
    defensive = [k for k in RC_DEFENSIVE if k in low]
    obsequious = [k for k in RC_OBSEQUIOUS if k in low]
    author_marks = RW_AUTHOR_RE.findall(inp["letter"] + inp["rev_text"])
    stg["det"] = {"diffs": diffs, "n_diffs": len(diffs),
                  "words_orig": len(inp["orig_text"].split()), "words_rev": len(inp["rev_text"].split()),
                  "words_letter": len(inp["letter"].split()),
                  "unsourced_numbers": unsourced, "letter_numbers_missing": letter_missing,
                  "refs_missing": refs_missing, "defensive": defensive, "obsequious": obsequious,
                  "thanks": low.count("thank"), "author_markers": author_marks[:20]}
    return stg["det"]


def _rc_diff_text(diffs, cap_blocks=60, cap_chars=700):
    parts = []
    for i, d in enumerate(diffs[:cap_blocks], 1):
        parts.append(f"[block {i}] BEFORE: {d['before'][:cap_chars]}\nAFTER: {d['after'][:cap_chars]}")
    if len(diffs) > cap_blocks:
        parts.append(f"[... {len(diffs) - cap_blocks} more changed paragraphs not shown]")
    return "\n\n".join(parts) or "(no paragraph differs between the two manuscripts)"


def rc_run(state, status_box):
    """points -> letter map -> per-point audit -> change map -> editor
    summary -> report."""
    _RW_UI["box"] = status_box
    inp, opts, stg = state["inputs"], state["opts"], state["stages"]

    def step(msg):
        status_box.write(msg)
        rw_log(state, msg)

    if "det" not in stg:
        step("Comparing the two manuscripts and checking numbers and references...")
        rc_deterministic(state)
        rc_save_state(state)
    if "points" not in stg:
        step("Splitting the reviews into points...")
        raw = rw_call(RX_POINTS_SYSTEM, f"DECISION LETTER AND REVIEWS:\n\n{inp['reviews'][:120000]}", 6000)
        pts = [p for p in ((_rw_json_block(raw) or {}).get("points") or []) if isinstance(p, dict) and p.get("text")]
        for i, p in enumerate(pts, 1):
            p.setdefault("id", f"P.{i}")
            p.setdefault("reviewer", 1)
            p.setdefault("type", "clarification")
            p.setdefault("severity", "minor")
        if not pts:
            raise RuntimeError("No reviewer points could be extracted - check the reviews text.")
        stg["points"] = pts
        rc_save_state(state)
    pts = stg["points"]
    pts_txt = "\n".join(f"[{p['id']}] reviewer {p['reviewer']} · {p['type']} · {p['severity']}: {p['text']}"
                        for p in pts)
    if "map" not in stg:
        step("Mapping the response letter onto the points...")
        raw = rw_call(RC_MAP_SYSTEM, f"POINTS:\n{pts_txt}\n\n=== RESPONSE LETTER ===\n{inp['letter'][:100000]}", 9000)
        got = {str(r.get("id")): r for r in ((_rw_json_block(raw) or {}).get("responses") or [])
               if isinstance(r, dict)}
        stg["map"] = {p["id"]: got.get(p["id"]) or {"id": p["id"], "found": False, "excerpt": "",
                                                    "claims": [], "stance": "unclear"} for p in pts}
        rc_save_state(state)
    diff_txt = _rc_diff_text(stg["det"]["diffs"])
    stg.setdefault("audits", {})
    todo = [p for p in pts if p["id"] not in stg["audits"]]
    batch, n_b = 6, max(1, (len(todo) + 5) // 6)
    for bi in range(0, len(todo), batch):
        grp = todo[bi:bi + batch]
        step(f"Auditing points {bi // batch + 1}/{n_b} ({', '.join(p['id'] for p in grp)})...")
        blocks = []
        for p in grp:
            m = stg["map"][p["id"]]
            blocks.append(f"[{p['id']}] reviewer {p['reviewer']} · {p['type']} · {p['severity']}\n"
                          f"POINT: {p['text']}\nRESPONSE EXCERPT: {m.get('excerpt') or '(the letter does not address this point)'}\n"
                          f"CLAIMS: {_rwjson.dumps(m.get('claims') or [])[:2000]}\nSTANCE: {m.get('stance', '')}")
        umsg = ("POINTS AND RESPONSES:\n\n" + "\n\n".join(blocks)
                + f"\n\n=== ACTUAL CHANGES (manuscript as reviewed -> revised) ===\n{diff_txt[:60000]}"
                + f"\n\n=== REVISED MANUSCRIPT (excerpt) ===\n{inp['rev_text'][:50000]}"
                + (f"\n\n=== SUPPORTING FILES ===\n{inp['si_text'][:30000]}" if inp.get("si_text") else "")
                + (f"\n\n=== AUTHORS' NOTES ===\n{inp['notes'][:8000]}" if inp.get("notes") else "")
                + "\n\nAudit these points now (JSON only).")
        raw = rw_call(RC_AUDIT_SYSTEM, umsg, 9000)
        got = {str(a.get("id")): a for a in ((_rw_json_block(raw) or {}).get("audits") or []) if isinstance(a, dict)}
        for p in grp:
            a = got.get(p["id"]) or {"id": p["id"], "verdict": "not_addressed", "evidence": "(no audit returned)",
                                     "reviewer_satisfied": 0.0, "issues": ["the model returned no audit"], "fix": ""}
            a.setdefault("issues", [])
            try:
                a["reviewer_satisfied"] = float(a.get("reviewer_satisfied") or 0.0)
            except Exception:
                a["reviewer_satisfied"] = 0.0
            stg["audits"][p["id"]] = a
        rc_save_state(state)
    if "diffmap" not in stg:
        step("Mapping every change to a reviewer request...")
        diffs = stg["det"]["diffs"]
        if diffs:
            raw = rw_call(RC_DIFFMAP_SYSTEM, f"POINTS:\n{pts_txt}\n\n=== CHANGE BLOCKS ===\n{diff_txt[:80000]}", 8000)
            blocks = {int(b.get("n")): b for b in ((_rw_json_block(raw) or {}).get("blocks") or [])
                      if isinstance(b, dict) and str(b.get("n", "")).isdigit()}
        else:
            blocks = {}
        stg["diffmap"] = [{"n": i, "points": list((blocks.get(i) or {}).get("points") or []),
                           "new_content": bool((blocks.get(i) or {}).get("new_content")),
                           "what": str((blocks.get(i) or {}).get("what") or "")}
                          for i in range(1, min(len(diffs), 60) + 1)]
        rc_save_state(state)
    if "summary" not in stg:
        step("Editor's verdict and reviewer reactions...")
        det = stg["det"]
        table = "\n".join(f"[{p['id']}] R{p['reviewer']} {p['severity']} · verdict {stg['audits'][p['id']]['verdict']} · "
                          f"satisfied {stg['audits'][p['id']]['reviewer_satisfied']:.2f} · issues: "
                          f"{'; '.join(str(x) for x in stg['audits'][p['id']].get('issues', []))[:300]} · fix: "
                          f"{str(stg['audits'][p['id']].get('fix', ''))[:200]}" for p in pts)
        unreq = [b for b in stg["diffmap"] if not b["points"]]
        checks = (f"Unrequested changes ({len(unreq)}): " + "; ".join(f"block {b['n']}: {b['what']}" for b in unreq[:15])
                  + f"\nNumbers in the letter absent from the revised manuscript/SI/notes: {', '.join(det['letter_numbers_missing'][:20]) or 'none'}"
                  f"\nNew numbers in the revision without a source: {', '.join(det['unsourced_numbers'][:20]) or 'none'}"
                  f"\nFigure/table references in the letter not found in the revised documents: {', '.join(det['refs_missing'][:20]) or 'none'}"
                  f"\nDefensive phrases: {', '.join(det['defensive']) or 'none'} · obsequious phrases: {', '.join(det['obsequious']) or 'none'}"
                  f"\nLetter: {det['words_letter']} words; manuscript {det['words_orig']} -> {det['words_rev']} words; "
                  f"{det['n_diffs']} changed paragraphs; [AUTHOR] markers left: {len(det['author_markers'])}")
        umsg = (f"PER-POINT AUDIT:\n{table[:40000]}\n\nDETERMINISTIC CHECKS:\n{checks}\n\n"
                f"LETTER OPENING:\n{inp['letter'][:2500]}")
        stg["summary"] = rw_call(RC_SUMMARY_SYSTEM.replace("{JOURNAL}", opts.get("journal") or "the journal"), umsg, 5000)
        rc_save_state(state)
    if "report" not in stg:
        step("Assembling the report...")
        rc_report(state)
        try:
            record_qa(f"[REVISION CHECK] {inp.get('rev_name', '')}", stg["report"], [], do_autosave)
        except Exception as e:
            rw_log(state, f"record_qa failed: {e}")
        state["status"] = "complete"
        rc_save_state(state)


RC_VERDICT_LABEL = {"addressed": "✅ addressed", "partial": "🟡 partial", "claim_not_found": "❌ claim not in manuscript",
                    "rebuttal_ok": "✅ rebuttal with evidence", "rebuttal_weak": "🟠 weak rebuttal",
                    "not_addressed": "❌ not addressed"}


def rc_report(state):
    inp, stg = state["inputs"], state["stages"]
    pts, det = stg["points"], stg["det"]
    counts = {}
    for p in pts:
        v = stg["audits"][p["id"]]["verdict"]
        counts[v] = counts.get(v, 0) + 1
    unreq = [b for b in stg["diffmap"] if not b["points"]]
    lines = [f"# Revision check - {inp.get('rev_name', '')}", "",
             f"*{len(pts)} reviewer points · " + " · ".join(f"{RC_VERDICT_LABEL.get(k, k)}: {n}" for k, n in sorted(counts.items()))
             + f" · {det['n_diffs']} changed paragraphs, {len(unreq)} unrequested · mean reviewer satisfaction "
             f"{(sum(stg['audits'][p['id']]['reviewer_satisfied'] for p in pts) / max(1, len(pts))):.2f}*", "",
             "> Verdicts come from the actual differences between the two manuscripts, not from what the letter claims.", "",
             stg["summary"].strip(), "", "---", "", "## Point-by-point audit", ""]
    for p in pts:
        a, m = stg["audits"][p["id"]], stg["map"][p["id"]]
        lines.append(f"**{p['id']}** (reviewer {p['reviewer']}, {p['type']}, {p['severity']}) - "
                     f"{RC_VERDICT_LABEL.get(a['verdict'], a['verdict'])} · satisfied {a['reviewer_satisfied']:.0%}")
        lines.append(f"*Reviewer:* {p['text'].strip()}")
        lines.append(f"*Your response:* {(m.get('excerpt') or '(none found in the letter)').strip()[:700]}")
        lines.append(f"*Evidence:* {str(a.get('evidence', '')).strip()}")
        if a.get("issues"):
            lines.append("*Issues:* " + "; ".join(str(x) for x in a["issues"]))
        if a.get("fix"):
            lines.append(f"*Fix:* {a['fix']}")
        lines.append("")
    lines += ["---", "", "## Changes in the manuscript and what they answer", ""]
    for b in stg["diffmap"]:
        d = det["diffs"][b["n"] - 1]
        tag = ", ".join(b["points"]) if b["points"] else "UNREQUESTED - declare to the editor"
        lines.append(f"**Change {b['n']}** [{tag}]{' · new content' if b['new_content'] else ''} - {b['what']}")
        lines.append(d["marked"][:1500])
        lines.append("")
    if det["n_diffs"] > len(stg["diffmap"]):
        lines.append(f"*{det['n_diffs'] - len(stg['diffmap'])} further changed paragraphs were not mapped (cap).*")
    lines += ["---", "", "## Consistency checks", "",
              f"- Numbers in the letter absent from the revised manuscript, SI, notes or reviews: "
              f"{', '.join(det['letter_numbers_missing']) or 'none'}",
              f"- New numbers in the revision with no source (original, SI, notes, letter): "
              f"{', '.join(det['unsourced_numbers']) or 'none'}",
              f"- Figure/table references in the letter not found in the revised documents: "
              f"{', '.join(det['refs_missing']) or 'none'}",
              f"- Defensive phrases: {', '.join(det['defensive']) or 'none'}",
              f"- Obsequious phrases: {', '.join(det['obsequious']) or 'none'}",
              f"- 'thank' appears {det['thanks']} time(s); letter {det['words_letter']} words; manuscript "
              f"{det['words_orig']} → {det['words_rev']} words",
              f"- [AUTHOR: ...] markers still present: {len(det['author_markers'])}"]
    stg["report"] = "\n".join(lines)
    try:
        d = RC_DIR / state["sig"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "revision_check.md").write_text(stg["report"], encoding="utf-8")
        doc = Document()
        doc.add_heading("Revision check", level=1)
        md_to_docx(doc, stg["report"])
        doc.save(d / "revision_check.docx")
        state["docx"] = str(d / "revision_check.docx")
    except Exception as e:
        rw_log(state, f"docx failed: {e}")


def render_revision_check_panel():
    st.markdown(
        "**Check my revision.** You revised the manuscript yourself and wrote the response "
        "letter - upload the manuscript as reviewed, your revised manuscript, your response "
        "letter and the reviews. GrapheAI compares the two manuscripts paragraph by "
        "paragraph, maps your letter onto every reviewer point, verifies each claimed change "
        "against the real differences, flags unrequested changes, unsourced numbers and "
        "dangling figure references, judges the letter's tone, and simulates each reviewer's "
        "second-round reaction with a predicted decision and a ranked fix list.")
    c1, c2 = st.columns(2)
    with c1:
        o_up = st.file_uploader("Manuscript as reviewed (required)", type=["docx", "pdf", "txt", "md"], key="rc_orig")
        r_up = st.file_uploader("Your revised manuscript (required)", type=["docx", "pdf", "txt", "md"], key="rc_rev")
        si_ups = st.file_uploader("Revised supporting files (SI, captions - optional, several)",
                                  type=["docx", "pdf", "txt", "md"], key="rc_si", accept_multiple_files=True)
    with c2:
        l_up = st.file_uploader("Your response letter (file)", type=["docx", "pdf", "txt", "md"], key="rc_letter")
        l_txt = st.text_area("...or paste the response letter", height=100, key="rc_letter_txt")
        rv_ups = st.file_uploader("Decision letter + reviews (one or several files)",
                                  type=["docx", "pdf", "txt", "md"], key="rc_reviews", accept_multiple_files=True)
        rv_txt = st.text_area("...or paste the reviews", height=100, key="rc_reviews_txt")
    notes = st.text_area("Notes for the checker (optional): new data you have, constraints, what you "
                         "deliberately did not do", height=80, key="rc_notes")
    journal = st.text_input("Journal", key="rc_journal", placeholder="e.g. Joule")

    def _read(up):
        try:
            return extract_uploaded_text(up)
        except Exception as e:
            st.error(f"Could not read {up.name}: {e}")
            return ""

    orig_text = _read(o_up) if o_up is not None else ""
    rev_text = _read(r_up) if r_up is not None else ""
    letter = (_read(l_up) if l_up is not None else "")
    if l_txt.strip():
        letter = (letter + "\n\n" + l_txt.strip()).strip()
    reviews = "\n\n".join([f"=== FILE: {u.name} ===\n{_read(u)}" for u in (rv_ups or [])] + ([rv_txt.strip()] if rv_txt.strip() else []))
    si_text = "\n\n".join(f"=== FILE: {u.name} ===\n{_read(u)}" for u in (si_ups or []))
    if orig_text and rev_text:
        st.caption(f"as reviewed: {len(orig_text.split()):,} words · revised: {len(rev_text.split()):,} words · "
                   f"letter: {len(letter.split()):,} words · reviews: {len(reviews.split()):,} words")

    state = st.session_state.get("rc")
    if state is None:
        last = rc_load_last_state()
        if last:
            if st.button(f"↩️ Reopen the last check ({last['inputs'].get('rev_name', '?')}, {last.get('status')})",
                         key="rc_reopen"):
                st.session_state["rc"] = last
                st.rerun()

    def _start():
        op, _ = rx_mark_sections(orig_text)
        rp, _ = rx_mark_sections(rev_text)
        sig = "rc_" + _rwhash.sha1((orig_text + rev_text + letter + reviews).encode("utf-8", "replace")).hexdigest()[:12]
        return {"sig": sig, "status": "running", "stages": {}, "log": [],
                "opts": {"journal": journal.strip()},
                "inputs": {"orig_name": o_up.name, "rev_name": r_up.name, "orig_text": orig_text, "rev_text": rev_text,
                           "orig_paragraphs": op, "rev_paragraphs": rp, "letter": letter, "reviews": reviews,
                           "si_text": si_text, "notes": notes.strip()}}

    def _drive(s_):
        with st.status("Checking the revision...", expanded=True) as box:
            try:
                rc_run(s_, box)
                box.update(label="Revision check complete", state="complete")
            except Exception as e:
                rw_log(s_, f"ERROR: {e}")
                s_["status"] = "error"
                rc_save_state(s_)
                box.update(label=f"Stopped: {e}", state="error")
                st.error(f"Stopped: {e}. Progress is saved - press Resume.")
        st.session_state["rc"] = s_

    b1, b2 = st.columns([2, 1])
    with b1:
        if state and state.get("status") in ("error", "running") and state["stages"]:
            if st.button("▶️ Resume", type="primary", key="rc_resume"):
                state["status"] = "running"
                _drive(state)
                st.rerun()
        elif st.button("🔍 Check my revision", type="primary", key="rc_go",
                       disabled=not (orig_text and rev_text and letter.strip() and reviews.strip() and api_key.strip())):
            _drive(_start())
            st.rerun()
    with b2:
        if state and st.button("🗑️ Start over", key="rc_clear"):
            st.session_state.pop("rc", None)
            st.rerun()
    state = st.session_state.get("rc")
    if not state:
        return
    if state["status"] != "complete":
        with st.expander("Pipeline log"):
            st.text("\n".join(state.get("log", [])))
        return

    stg, det, pts = state["stages"], state["stages"]["det"], state["stages"]["points"]
    st.markdown("---")
    counts = {}
    for p in pts:
        v = stg["audits"][p["id"]]["verdict"]
        counts[v] = counts.get(v, 0) + 1
    ok = counts.get("addressed", 0) + counts.get("rebuttal_ok", 0)
    bad = counts.get("claim_not_found", 0) + counts.get("not_addressed", 0) + counts.get("rebuttal_weak", 0)
    unreq = [b for b in stg["diffmap"] if not b["points"]]
    m = st.columns(5)
    m[0].metric("Points", len(pts))
    m[1].metric("Addressed", ok, f"{counts.get('partial', 0)} partial" if counts.get("partial") else None)
    m[2].metric("Problems", bad, "claims not found / not addressed / weak" if bad else None, delta_color="inverse")
    m[3].metric("Unrequested changes", len(unreq), delta_color="inverse")
    m[4].metric("Numbers to reconcile", len(det["letter_numbers_missing"]) + len(det["unsourced_numbers"]),
                delta_color="inverse")
    t = st.tabs(["🧑‍⚖️ Verdict & fixes", "📋 Point by point", "🔀 Changes", "🔎 Consistency", "🧭 Log"])
    with t[0]:
        st.markdown(stg["summary"])
    with t[1]:
        st.dataframe([{"id": p["id"], "reviewer": p["reviewer"], "severity": p["severity"],
                       "verdict": RC_VERDICT_LABEL.get(stg["audits"][p["id"]]["verdict"], stg["audits"][p["id"]]["verdict"]),
                       "satisfied": f"{stg['audits'][p['id']]['reviewer_satisfied']:.0%}",
                       "stance": stg["map"][p["id"]].get("stance", ""),
                       "fix": str(stg["audits"][p["id"]].get("fix", ""))[:160]} for p in pts],
                     use_container_width=True, hide_index=True)
        for p in pts:
            a, mp = stg["audits"][p["id"]], stg["map"][p["id"]]
            with st.expander(f"{p['id']} · {RC_VERDICT_LABEL.get(a['verdict'], a['verdict'])} · satisfied {a['reviewer_satisfied']:.0%}"):
                st.markdown(f"**Reviewer:** *{p['text']}*")
                st.markdown(f"**Your response:** {mp.get('excerpt') or '(none found)'}")
                if mp.get("claims"):
                    st.markdown("**Claims:** " + "; ".join(f"{c.get('claim', '')} ({c.get('location', '')})"
                                                          if isinstance(c, dict) else str(c) for c in mp["claims"]))
                st.markdown(f"**Evidence:** {a.get('evidence', '')}")
                if a.get("issues"):
                    st.markdown("**Issues:** " + "; ".join(str(x) for x in a["issues"]))
                if a.get("fix"):
                    st.markdown(f"**Fix:** {a['fix']}")
    with t[2]:
        if not det["diffs"]:
            st.info("The two manuscripts do not differ paragraph by paragraph.")
        for b in stg["diffmap"]:
            d = det["diffs"][b["n"] - 1]
            tag = ", ".join(b["points"]) if b["points"] else "⚠️ unrequested - declare it to the editor"
            st.markdown(f"**Change {b['n']}** [{tag}]{' · new content' if b['new_content'] else ''} - {b['what']}")
            st.markdown(d["marked"])
            st.markdown("---")
    with t[3]:
        st.markdown(f"- **Numbers in the letter not found in the revised manuscript / SI / notes / reviews:** "
                    f"{', '.join(det['letter_numbers_missing']) or 'none'}")
        st.markdown(f"- **New numbers in the revision without a source:** {', '.join(det['unsourced_numbers']) or 'none'}")
        st.markdown(f"- **Figure/table references in the letter that do not resolve:** {', '.join(det['refs_missing']) or 'none'}")
        st.markdown(f"- **Defensive phrases:** {', '.join(det['defensive']) or 'none'}  ·  **Obsequious phrases:** "
                    f"{', '.join(det['obsequious']) or 'none'}")
        st.markdown(f"- 'thank' × {det['thanks']} · letter {det['words_letter']} words · manuscript "
                    f"{det['words_orig']} → {det['words_rev']} words · [AUTHOR] markers left: {len(det['author_markers'])}")
    with t[4]:
        st.text("\n".join(state.get("log", [])))
    p = Path(state.get("docx", ""))
    if p.exists():
        st.download_button("⬇️ Revision check (Word)", p.read_bytes(), file_name=p.name,
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           key=f"rc_dl_{state['sig']}")
    st.caption(f"Saved under answers/revision_checks/{state['sig']}/")
    render_followup(
        "rc_" + state["sig"], "Discuss the check and fix the letter or the manuscript",
        {"response_letter": state["inputs"]["letter"], "revised_manuscript": state["inputs"]["rev_text"]},
        sources_text=("REVIEWS:\n" + state["inputs"]["reviews"][:20000] + "\n\nMANUSCRIPT AS REVIEWED:\n"
                      + state["inputs"]["orig_text"][:30000] + "\n\nSUPPORTING FILES:\n" + state["inputs"].get("si_text", "")[:15000]
                      + "\n\nREVISION CHECK REPORT:\n" + stg["report"][:30000]),
        pool_texts=[state["inputs"]["orig_text"], state["inputs"]["rev_text"], state["inputs"].get("si_text", ""),
                    state["inputs"].get("notes", ""), state["inputs"]["letter"]],
        hint="Ask about a verdict, or say what to change - edits are applied to your response letter or "
             "revised manuscript exactly (numbers must come from the sources or your messages) and the revised "
             "copies can be downloaded here.")


# --------------------------------------------------------------------------
# App state and sidebar
# --------------------------------------------------------------------------
st.set_page_config(page_title="Workbench - by GrapheAI",
                   page_icon="✍️", layout="wide")
if "history" not in st.session_state:
    st.session_state.history = []

# ---------------------------------------------------------------------------
# Visual identity: editorial dark - slate ink, ivory type, flame accent
# ---------------------------------------------------------------------------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Serif:ital,wght@0,500;0,600;1,400&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@500&display=swap');

/* ---- Editorial dark: slate ink, ivory type, flame accent ---- */
html, body, [class*="css"], .stMarkdown, p, li {
    font-family: 'IBM Plex Sans', 'Segoe UI', sans-serif;
    font-size: 17px;
    color: #E6EAF0;
}
.stApp { background: #12161C; }
.stMarkdown p, .stMarkdown li { line-height: 1.65; }

/* Header: journal masthead on dark */
.mw-header { border-bottom: 3px solid #FF6B3D; padding-bottom: 12px;
             margin-bottom: 10px; }
.mw-title  { font-family: 'IBM Plex Serif', Georgia, serif;
             font-size: 2.7rem; font-weight: 600; color: #F4F6F9;
             margin: 0; letter-spacing: -0.5px; }
.mw-sub    { font-size: 0.85rem; color: #8E99A8; margin-top: 6px;
             text-transform: uppercase; letter-spacing: 2px; }
.mw-sub b  { color: #FF8A5C; font-weight: 600; }

/* Tabs: hairline rail, flame underline */
.stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #242D38;
                                    gap: 2px; }
.stTabs [data-baseweb="tab"] {
    font-size: 1.07rem; font-weight: 500; padding: 12px 18px;
    color: #8E99A8;
}
.stTabs [data-baseweb="tab"]:hover { color: #FFB08F; }
.stTabs [aria-selected="true"] { color: #FF8A5C; font-weight: 600; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #FF6B3D;
                                         height: 3px; }

/* Sidebar */
section[data-testid="stSidebar"] { background: #171D25;
    border-right: 1px solid #242D38; }
section[data-testid="stSidebar"] * { font-size: 15.5px; }
section[data-testid="stSidebar"] h1 { font-family: 'IBM Plex Serif', serif;
                                      font-size: 1.45rem; color: #F4F6F9; }

/* Headings: serif, ivory */
h2 { font-family: 'IBM Plex Serif', Georgia, serif; font-weight: 600;
     color: #DFE5EC; }
h3 { font-family: 'IBM Plex Serif', Georgia, serif; font-weight: 500;
     color: #C3CBD6; }

/* Cards: forms, expanders, dataframes as panels */
[data-testid="stForm"] {
    background: #1A212B; border: 1px solid #263140; border-radius: 14px;
    padding: 1.2rem 1.4rem 1rem; box-shadow: 0 2px 8px rgba(0,0,0,.35);
}
details { border: 1px solid #263140; border-radius: 12px;
          background: #1A212B; }
[data-testid="stExpander"] summary { font-size: 1.03rem; font-weight: 500; }
[data-testid="stDataFrame"] { border: 1px solid #263140;
                              border-radius: 12px; }

/* Metrics */
[data-testid="stMetric"] {
    background: #1A212B; border: 1px solid #263140; border-radius: 12px;
    padding: .7rem .95rem; box-shadow: 0 2px 6px rgba(0,0,0,.3);
}
[data-testid="stMetricValue"] { font-family: 'IBM Plex Mono', monospace;
                                color: #FF8A5C; }
[data-testid="stMetricLabel"] { color: #8E99A8;
                                text-transform: uppercase;
                                letter-spacing: 1px; font-size: .78rem; }

/* Buttons */
.stButton>button, .stDownloadButton>button, .stFormSubmitButton>button {
    font-size: 1.02rem; font-weight: 600; border-radius: 10px;
    padding: 0.55rem 1.3rem;
}
.stButton>button:hover, .stFormSubmitButton>button:hover {
    filter: brightness(1.12);
}

/* Inputs */
.stTextArea textarea, .stTextInput input {
    font-size: 1.03rem; border-radius: 10px;
}
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='mw-header'>"
    "<p class='mw-title'>✍️ Workbench</p>"
    "<p class='mw-sub'>by <b>GrapheAI</b> · developed by "
    "<b>Dr. Anurag Krishna</b></p>"
    "</div>",
    unsafe_allow_html=True)

with st.sidebar:
    st.title("✍️ Workbench")
    st.caption("by GrapheAI · Dr. Anurag Krishna")
    backend_label = st.radio(
        "Claude access", ["API key (pay per use)",
                          "Claude Max subscription (needs Claude Code)",
                          "OpenAI API (ChatGPT models, pay per use)",
                          "ChatGPT subscription (Plus/Pro via Codex CLI)"],
        help="API key: bills per token via console.anthropic.com - works "
             "anywhere. Claude Max: routes calls through Claude Code so they "
             "count against your Max plan, but requires the Claude Code app "
             "installed (blocked on some managed/company laptops). See "
             "MAX_SETUP.txt. Personal use of your own subscription only.")
    st.session_state["backend"] = ("max" if "Max" in backend_label
                                    else "codex" if "Codex" in backend_label
                                    else "openai" if "OpenAI" in backend_label
                                    else "api")
    if st.session_state["backend"] == "codex":
        api_key = render_codex_sidebar()
    elif st.session_state["backend"] == "openai":
        api_key = render_openai_sidebar()
    elif st.session_state["backend"] == "api":
        api_key = st.text_input("Anthropic API key", type="password",
                                help="Required for answers and claim checks. "
                                     "Leave empty for search-only mode.")
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
    model_label = st.selectbox("Model", list(MODELS.keys()), index=3,
                               help="Frontier (Fable) is the default for "
                                    "professor-grade judgement. Drop to "
                                    "Sonnet for quick lookups or if the "
                                    "Max usage window runs low.")
    model = MODELS[model_label]
    st.session_state["effort"] = st.select_slider(
        "Reasoning effort", options=list(EFFORT_LEVELS),
        value=st.session_state.get("effort", "xhigh"),
        help="Thinking depth for Fable 5.1 / Opus 5 / Sonnet 5 on the API "
             "backend. xhigh = professor-grade default; max = when "
             "correctness matters more than time; low = quick lookups.")
    answer_mode = st.selectbox("Answer mode", list(ANSWER_MODES.keys()))
    top_k = st.slider("Passages to retrieve", 3, 25, config.TOP_K)
    do_rerank = st.toggle(
        "🎯 Re-rank evidence (Haiku)", value=False,
        help="Retrieves 3x candidates and lets Haiku keep only the most "
             "relevant ones. Sharper citations in Ask and Draft, a few "
             "seconds slower, fractions of a cent per query.")
    do_autosave = st.toggle("Auto-save answers (.docx + .md)", value=True)
    st.markdown("---")
    try:
        collection = load_collection()
        st.success(f"Index: {collection.count()} chunks")
        index_ok = True
    except Exception:
        st.error("Index not found. Run `python ingest.py` first.")
        index_ok = False

    u = st.session_state.get("usage")
    if u:
        line = (f"Session: {u['calls']} calls - "
                f"{u['in']:,} in / {u['out']:,} out tokens")
        if st.session_state.get("backend") not in ("max", "codex") and u.get("cost"):
            line += f"  ·  ≈ ${u['cost']:.2f}"
        st.caption(line)

    # Persistent monthly spend + optional budget cap (API mode only).
    if st.session_state.get("backend") not in ("max", "codex"):
        ms = month_spend()
        saved_budget = get_budget()
        budget = st.number_input(
            "Monthly budget $ (0 = off)", min_value=0.0,
            value=float(saved_budget), step=5.0, key="budget_input",
            help="Tracks estimated API spend this calendar month across "
                 "sessions, so cost never surprises you. Set a cap to get a "
                 "warning as you approach it. Nothing is blocked - it only "
                 "warns.")
        if budget != saved_budget:
            set_budget(budget)
        if budget > 0:
            frac = ms / budget if budget else 0
            st.progress(min(frac, 1.0),
                        text=f"This month: ${ms:.2f} / ${budget:.0f}")
            if ms >= budget:
                st.error("Over your monthly budget. Switch heavy work to "
                         "Haiku/Sonnet, or pause until next month.")
            elif frac >= 0.8:
                st.warning("Approaching your monthly budget.")
        else:
            st.caption(f"This month (API): ≈ ${ms:.2f}")

    if st.session_state.history:
        st.markdown("---")
        st.download_button(
            "⬇️ Session to Word",
            data=qa_to_docx_bytes(st.session_state.history),
            file_name=f"session_{datetime.datetime.now():%Y%m%d_%H%M}.docx",
            mime="application/vnd.openxmlformats-officedocument"
                 ".wordprocessingml.document",
            use_container_width=True)
        st.download_button(
            "⬇️ Session as Markdown",
            data=qa_to_markdown(st.session_state.history),
            file_name=f"session_{datetime.datetime.now():%Y%m%d_%H%M}.md",
            mime="text/markdown", use_container_width=True)
        if st.button("🗑️ Clear session", use_container_width=True):
            st.session_state.history = []
            st.rerun()

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab_ask, tab_draft, tab_ms, tab_prop, tab_review, tab_claims, \
    tab_extract, tab_figs, tab_library, tab_get, tab_watch, \
    tab_proj, tab_career = st.tabs(
        ["💬 Ask", "✍️ Draft", "📄 Manuscript", "🏆 Proposal", "🧐 Review",
         "✅ Claim checker", "📊 Extract", "🖼️ Figures", "📚 Library",
         "📥 Get Papers", "📬 Watch", "🗂️ Projects", "🎓 Career"])

# ------------------------------ ASK ---------------------------------------
with tab_ask:
    ask_where = restrict_search_widget("ask") if index_ok else None

    with st.form("ask", clear_on_submit=True):
        question = st.text_area("Question", height=80,
                                placeholder="e.g. Which passivation strategies "
                                            "improve Voc in p-i-n cells?")
        follow_up = st.checkbox(
            "Include previous Q&A as context",
            help="Adds your last question and answer to the prompt, so "
                 "'and for slot-die coating?' style follow-ups work.")
        deep_q = st.checkbox(
            "🔬 Deep answer (multi-pass)",
            help="For big survey questions: Claude first maps the "
                 "sub-topics, retrieves evidence for each, then writes one "
                 "long themed answer over ~40 passages. Slower and more "
                 "tokens - use for review-scale questions.")
        submitted = st.form_submit_button("Search", type="primary")

    if submitted and question.strip() and index_ok and deep_q \
            and api_key.strip():
        q = question.strip()
        try:
            answer, hits = deep_answer(api_key.strip(), q, answer_mode,
                                       model, top_k, ask_where, do_rerank)
        except Exception as e:
            st.error(f"Deep answer failed: {e}")
            answer = None
        if answer:
            record_qa(q, answer, hits, do_autosave)
    elif submitted and question.strip() and index_ok:
        q = question.strip()
        with st.spinner("Retrieving relevant passages..."):
            if do_rerank and api_key.strip():
                cand = retrieve(q, min(top_k * 3, 50), where_extra=ask_where)
                hits = rerank_hits(api_key.strip(), q, cand, top_k)
            else:
                hits = retrieve(q, top_k, where_extra=ask_where)
        if not hits:
            st.warning("No relevant passages found.")
        else:
            if api_key.strip():
                prior = ""
                if follow_up and st.session_state.history:
                    last = st.session_state.history[-1]
                    prior = (f"For context, the previous exchange was:\n"
                             f"PREVIOUS QUESTION: {last['question']}\n"
                             f"PREVIOUS ANSWER: {last['answer']}\n\n")
                user_msg = (f"{prior}Excerpts:\n\n{build_context(hits)}\n\n"
                            f"Question: {q}")
                with st.spinner(f"{model_label} is writing a cited answer..."):
                    try:
                        answer = call_claude(api_key.strip(),
                                             build_system_prompt(answer_mode),
                                             user_msg, model)
                    except Exception as e:
                        st.error(f"Claude API error: {e}")
                        answer = None
            else:
                answer = ("(Search-only mode - no API key entered. "
                          "See retrieved passages below.)")
            if answer is not None:
                record_qa(q, answer, hits, do_autosave)

    for idx, qa in enumerate(reversed(st.session_state.history)):
        st.markdown("---")
        st.subheader(qa["question"])
        st.caption(f"{qa['time']} - {len(qa['hits'])} sources")
        st.markdown(qa["answer"])
        show_sources(qa["hits"], key_prefix=f"h{idx}")
        render_reference_exporter(qa["hits"], key=f"ref{idx}")
        st.download_button(
            "⬇️ This answer as Word",
            data=qa_to_docx_bytes([qa], title="Literature Q&A"),
            file_name=f"answer_{len(st.session_state.history)-idx}.docx",
            mime="application/vnd.openxmlformats-officedocument"
                 ".wordprocessingml.document",
            key=f"dl_{idx}")

# ------------------------------ DRAFT -------------------------------------
with tab_draft:
    st.markdown("Generate **draft manuscript text** grounded in your corpus: "
                "describe what the passage should cover, and the workbench "
                "retrieves the evidence and writes cited prose. Draft output "
                "is raw material - verify every value and replace [n] with "
                "formal references before manuscript use.")
    draft_mode = st.radio(
        "Mode", ["Draft passage / long draft (as before)",
                 "📚 Review / Perspective writer (synopsis → literature map → outline → article)"],
        horizontal=True, key="draft_mode")
    if draft_mode.startswith("📚"):
        render_review_writer()
    else:
        draft_where = restrict_search_widget("draft") if index_ok else None
        with st.form("draft"):
            brief = st.text_area(
                "What should this text cover?", height=100,
                placeholder="e.g. The state of the art of fully evaporated "
                            "p-i-n perovskite solar cells: efficiencies, "
                            "crystallization control strategies, and remaining "
                            "barriers to scale-up.")
            c1, c2, c3 = st.columns(3)
            with c1:
                content_type = st.selectbox("Format", list(DRAFT_FORMATS.keys()))
            with c2:
                target_words = st.select_slider(
                    "Target length (words)",
                    options=[100, 200, 300, 500, 800, 1200, 2000, 3000, 5000,
                             8000, 12000, 16000, 20000],
                    value=300,
                    help="Up to 800 words: single pass. Above 800: automatic "
                         "section-by-section generation - Claude plans an "
                         "outline, then each section retrieves its own "
                         "evidence. Long drafts take several minutes and "
                         "more tokens.")
            with c3:
                draft_k = st.slider("Evidence passages (per section)", 8, 30, 18)
            draft_submitted = st.form_submit_button("Generate draft",
                                                    type="primary")

        if draft_submitted and brief.strip() and index_ok:
            if not api_key.strip():
                st.error("Drafting needs the API key (sidebar).")
            else:
                if target_words <= 800:
                    with st.spinner("Retrieving evidence..."):
                        if do_rerank and api_key.strip():
                            cand = retrieve(brief.strip(),
                                            min(draft_k * 3, 60),
                                            where_extra=draft_where)
                            hits = rerank_hits(api_key.strip(), brief.strip(),
                                               cand, draft_k)
                        else:
                            hits = retrieve(brief.strip(), draft_k,
                                            where_extra=draft_where)
                    if not hits:
                        st.warning("No relevant passages found.")
                        draft_text = None
                    else:
                        user_msg = (f"Excerpts:\n\n{build_context(hits)}\n\n"
                                    f"DRAFT BRIEF: {brief.strip()}")
                        with st.spinner(f"{model_label} is drafting..."):
                            try:
                                draft_text = call_claude(
                                    api_key.strip(),
                                    build_draft_prompt(content_type,
                                                       target_words),
                                    user_msg, model,
                                    max_tokens=min(int(target_words * 2.5) + 500,
                                                   8000))
                            except Exception as e:
                                st.error(f"Claude API error: {e}")
                                draft_text = None
                else:
                    progress = st.progress(0.0, text="Planning outline...")
                    try:
                        draft_text, hits = generate_long_draft(
                            api_key.strip(), brief.strip(), content_type,
                            target_words, draft_k, model, progress,
                            where=draft_where)
                    except Exception as e:
                        st.error(f"Long-draft pipeline error: {e}")
                        draft_text = None
                    finally:
                        progress.empty()
                if draft_text:
                    record_qa(f"[DRAFT - {content_type}] {brief.strip()}",
                              draft_text, hits, do_autosave)
                    st.session_state["last_draft"] = {"text": draft_text,
                                                      "hits": hits}
                    st.markdown("---")
                    st.markdown(draft_text)
                    render_reference_exporter(hits, key="draftref")
                    show_sources(hits, key_prefix="draft")
                    st.caption("Saved to the answers folder (.docx + .md) "
                               "and to the session history in the Ask tab.")

        # ---- Reference list builder ------------------------------------------
        st.markdown("---")
        st.markdown("### 📚 Reference list builder")
        last_draft = st.session_state.get("last_draft")
        if not last_draft:
            st.caption("Generate a draft above first, then build a real reference "
                       "list (via Crossref) for its [n] citations - including a "
                       ".bib file for your manuscript.")
        else:
            rb1, rb2 = st.columns([2, 2])
            with rb1:
                ref_email = st.text_input("Contact email for Crossref",
                                          value="k.anurag2011@gmail.com",
                                          key="ref_email")
            with rb2:
                ref_style = st.radio("Citation style",
                                     ["Keep [n]", "(Author, Year)"],
                                     horizontal=True, key="ref_style")
            if st.button("Build reference list", type="primary"):
                hits = last_draft["hits"]
                papers = {}          # sig -> {ns, title, file}
                for i, h in enumerate(hits, start=1):
                    sig = h["meta"]["doc_sig"]
                    p = papers.setdefault(sig, {"ns": [], "title":
                                                h["meta"]["title"],
                                                "file": h["meta"]["file"]})
                    p["ns"].append(i)
                bar = st.progress(0.0, text="Looking up on Crossref...")
                ref_lines, bib_entries = [], []
                used_keys = set()
                n_to_label = {}
                for pi, (sig, p) in enumerate(papers.items(), start=1):
                    bar.progress(pi / len(papers), text=p["title"][:60])
                    rec = crossref_lookup(p["title"], ref_email.strip())
                    nums = ",".join(str(n) for n in p["ns"])
                    ref_lines.append(f"[{nums}] "
                                     + format_reference(rec, p["title"], p["file"]))
                    if rec:
                        key = bibtex_key(rec, used_keys)
                        bib_entries.append(bibtex_entry(rec, key))
                        fam = (rec.get("authors") or [{}])[0].get("family",
                                                                  "Unknown")
                        label = f"({fam} et al., {rec.get('year') or 'n.d.'})"
                    else:
                        label = f"[{p['ns'][0]}]"
                    for n in p["ns"]:
                        n_to_label[n] = label
                bar.empty()

                out_text = last_draft["text"]
                if ref_style == "(Author, Year)":
                    out_text = re.sub(
                        r"\[(\d+)\]",
                        lambda m: n_to_label.get(int(m.group(1)), m.group(0)),
                        out_text)
                refs_md = (out_text + "\n\n## References\n\n"
                           + "\n".join(f"- {ln}" for ln in ref_lines))
                st.session_state["refs_output"] = {
                    "md": refs_md, "bib": "\n\n".join(bib_entries)}

            refs_out = st.session_state.get("refs_output")
            if refs_out:
                st.markdown(refs_out["md"])
                rd1, rd2 = st.columns(2)
                with rd1:
                    st.download_button(
                        "⬇️ Draft + references (.md)", data=refs_out["md"],
                        file_name="draft_with_references.md",
                        mime="text/markdown", use_container_width=True)
                with rd2:
                    st.download_button(
                        "⬇️ Bibliography (.bib)", data=refs_out["bib"],
                        file_name="references.bib", mime="text/plain",
                        use_container_width=True)
                st.caption("Entries marked [VERIFY] had an uncertain Crossref "
                           "match - check them before submission.")

# ------------------------------ MANUSCRIPT --------------------------------
REBUTTAL_TONES = {
    "Cordial and confident":
        "Warm and collegial; concede readily where the reviewer is right.",
    "Neutral and formal":
        "Strictly professional; minimal pleasantries.",
    "Firm where justified":
        "Polite but push back with evidence where the reviewer is factually "
        "wrong; concede only what is actually deficient.",
}

REBUTTAL_SYSTEM = """\
You draft the authors' point-by-point response to peer review of a
scientific manuscript. Inputs: the manuscript text, the referee report(s),
and optionally numbered literature excerpts.

Produce a complete response letter:
- A brief opening paragraph to the editor (2-3 sentences, appreciative but
  not obsequious).
- Then every reviewer comment in order, numbered per reviewer
  ("Comment R1.1", "Comment R2.3", ...): first the comment quoted or
  faithfully condensed, then "Response:" with a specific, substantive
  answer.
- End with "Summary of changes" - a numbered list of every manuscript
  change promised above.

Rules:
- NEVER invent experimental data, measurements, simulations, or figures.
  Where new data would be needed, build the response around a placeholder
  like [AUTHOR: insert XRD comparison here].
- Where the manuscript already addresses the comment, say where (section /
  figure) and restate the argument in 1-2 sentences.
- Propose concrete text changes: quote the original sentence, then give
  the revised wording.
- Cite the provided excerpts as [n] where literature supports a response;
  never fabricate references. If no excerpt fits, write
  [AUTHOR: add supporting reference].
- Tone: {tone}"""

COVER_SYSTEM = """\
You write journal submission cover letters for research manuscripts.
From the manuscript (and the target journal, if named) write a one-page
letter: salutation "Dear Editor,"; paragraph 1 states the title and that
the work is submitted for consideration, plus a one-sentence essence of
the finding; paragraphs 2-3 give what was done, the key quantitative
results with units, why it advances the field, and why it fits this
journal's scope and readership; the closing paragraph states the work is
original, not under consideration elsewhere, and names the corresponding
author.

Rules: every scientific claim must come from the manuscript - no invented
numbers, no empty superlatives; at most 350 words; use
[AUTHOR: ...] placeholders for names, addresses, and anything you cannot
know. Do not suggest reviewers unless the user asked."""

ABSTRACT_SYSTEM = """\
You distill a scientific manuscript into submission front-matter.
Produce exactly:

1. ABSTRACT - one paragraph, at most {limit} words: context (1-2
   sentences), what was done, the key quantitative results with units,
   and the wider implication. No citations; define or avoid abbreviations.
2. HIGHLIGHTS - 5 bullets, each at most 85 characters, each a finding
   (with a number where possible), not a topic.
3. TOC SENTENCE - 3 candidate one-sentence teasers (max 25 words each)
   for a table-of-contents / graphical-abstract entry.

Use only what is in the manuscript; keep every number faithful."""

with tab_ms:
    st.markdown("**Submission toolkit** - the last mile of a manuscript: "
                "the point-by-point response to referees, the cover letter, "
                "and abstract/highlights. Upload the manuscript once, then "
                "use any tool. Everything is a draft for your judgement - "
                "verify all statements before sending.")
    ms_up = st.file_uploader("Manuscript (.docx / .pdf / .txt / .md)",
                             type=["docx", "pdf", "txt", "md"], key="ms_up")
    ms_text = ""
    ms_srcname = ""
    if ms_up is not None:
        try:
            ms_text = extract_uploaded_text(ms_up)[:120000]
            ms_srcname = ms_up.name
            st.caption(f"Manuscript loaded: {len(ms_text):,} characters.")
        except Exception as e:
            st.error(f"Could not read the manuscript file: {e}")
    if not ms_text:
        ptxt, pname = project_file_picker("Manuscript", "ms_proj_pick")
        if ptxt:
            ms_text = ptxt[:120000]
            ms_srcname = pname
            st.caption(f"From project: {pname} - "
                       f"{len(ms_text):,} characters.")

    ms_tool = st.radio("Tool", ["Response to reviewers", "Cover letter",
                                "Abstract & highlights"],
                       horizontal=True, key="ms_tool")

    if ms_tool == "Response to reviewers":
        rev_ups = st.file_uploader(
            "Referee report(s) (.docx / .pdf / .txt / .md - several files ok)",
            type=["docx", "pdf", "txt", "md"], accept_multiple_files=True,
            key="ms_rev_up")
        rev_paste = st.text_area("...and/or paste report text here",
                                 height=160, key="ms_rev_txt")
        rev_parts = []
        for f in rev_ups or []:
            try:
                rev_parts.append(f"=== {f.name} ===\n"
                                 + extract_uploaded_text(f)[:30000])
            except Exception as e:
                st.warning(f"Could not read {f.name}: {e}")
        if rev_paste.strip():
            rev_parts.append(rev_paste.strip()[:30000])
        rev_text = "\n\n".join(rev_parts)[:60000]

        rt1, rt2 = st.columns([1, 1])
        with rt1:
            ms_tone = st.selectbox("Tone", list(REBUTTAL_TONES.keys()),
                                   key="ms_tone")
        with rt2:
            ms_ground = st.checkbox(
                "Search my library for supporting citations",
                value=index_ok, disabled=not index_ok, key="ms_ground",
                help="Retrieves passages relevant to the reviewers' points "
                     "so responses can cite real literature as [n].")
        if st.button("📄 Draft response letter", type="primary",
                     key="ms_rebut_go",
                     disabled=not (ms_text and rev_text)):
            hits = []
            evidence = ""
            if ms_ground and index_ok:
                with st.spinner("Retrieving supporting literature..."):
                    hits = retrieve(rev_text[:1800], 12)
                    if hits:
                        evidence = ("\n\nLITERATURE EXCERPTS (cite as [n]):"
                                    "\n\n" + build_context(hits))
            user_msg = (f"MANUSCRIPT:\n\n{ms_text[:60000]}\n\n"
                        f"REFEREE REPORTS:\n\n{rev_text}{evidence}")
            sys_p = REBUTTAL_SYSTEM.format(tone=REBUTTAL_TONES[ms_tone])
            with st.spinner(f"{model_label} is drafting the response..."):
                try:
                    out = call_claude(api_key.strip(), sys_p, user_msg,
                                      model, max_tokens=12000)
                except Exception as e:
                    st.error(f"Claude error: {e}")
                    out = None
            if out:
                record_qa("[RESPONSE TO REVIEWERS] "
                          + (ms_srcname or "manuscript"),
                          out, hits, do_autosave)
                st.markdown("---")
                st.markdown(out)
        if not (ms_text and rev_text):
            st.caption("Needs both the manuscript and at least one referee "
                       "report.")

    elif ms_tool == "Cover letter":
        cl1, cl2 = st.columns([1, 2])
        with cl1:
            ms_journal = st.text_input("Target journal", key="ms_journal",
                                       placeholder="e.g. Joule")
        with cl2:
            ms_emph = st.text_input(
                "Anything to emphasise (optional)", key="ms_emph",
                placeholder="e.g. first certified >30% tandem on textured Si")
        if st.button("✉️ Draft cover letter", type="primary",
                     key="ms_cover_go", disabled=not ms_text):
            user_msg = (f"TARGET JOURNAL: {ms_journal.strip() or 'not named'}"
                        f"\nEMPHASISE: {ms_emph.strip() or '-'}"
                        f"\n\nMANUSCRIPT:\n\n{ms_text[:60000]}")
            with st.spinner(f"{model_label} is writing..."):
                try:
                    out = call_claude(api_key.strip(), COVER_SYSTEM,
                                      user_msg, model, max_tokens=2000)
                except Exception as e:
                    st.error(f"Claude error: {e}")
                    out = None
            if out:
                record_qa(f"[COVER LETTER] {ms_journal.strip() or 'draft'}",
                          out, [], do_autosave)
                st.markdown("---")
                st.markdown(out)
        if not ms_text:
            st.caption("Upload the manuscript first.")

    else:  # Abstract & highlights
        ms_limit = st.select_slider("Abstract word limit",
                                    options=[100, 150, 200, 250, 300, 350],
                                    value=250, key="ms_limit")
        if st.button("🧾 Generate abstract & highlights", type="primary",
                     key="ms_abs_go", disabled=not ms_text):
            with st.spinner(f"{model_label} is distilling..."):
                try:
                    out = call_claude(
                        api_key.strip(),
                        ABSTRACT_SYSTEM.format(limit=ms_limit),
                        f"MANUSCRIPT:\n\n{ms_text[:90000]}",
                        model, max_tokens=2500)
                except Exception as e:
                    st.error(f"Claude error: {e}")
                    out = None
            if out:
                record_qa("[ABSTRACT & HIGHLIGHTS] "
                          + (ms_srcname or "manuscript"),
                          out, [], do_autosave)
                st.markdown("---")
                st.markdown(out)
        if not ms_text:
            st.caption("Upload the manuscript first.")

# ------------------------------ PROPOSAL ----------------------------------
COMPLIANCE_SYSTEM = """\
You audit a grant proposal draft against the OFFICIAL CALL TEXT (topic
description: expected outcomes, scope, specific conditions). The mock
evaluator judges quality; you judge FIT.

Produce (markdown):
1. **Compliance table** - one row per distinct expected outcome, scope
   element, or condition found in the call text:
   | Call requirement (quoted/condensed) | Where the draft addresses it | Verdict | Fix |
   Verdict is exactly one of: COVERED / WEAK / MISSING.
   "Where" names the draft section or quotes a phrase; "-" if missing.
   "Fix" is one concrete sentence on what to add or change.
2. **Scope check** - 2-4 sentences: is the project within scope, and any
   drift that could get it ruled out of scope.
3. **Top 5 actions** - ranked by likely score impact.

Rules: requirements come ONLY from the call text; judgements come ONLY
from the draft; never invent draft content; keep every call requirement -
do not merge or drop any."""

SOTA_SYSTEM = """\
You write the "State of the art and beyond" section of a competitive
research proposal (Horizon Europe style), from numbered literature
excerpts and the applicant's project idea.

Structure (markdown, about {words} words):
1. **State of the art** - organised by 3-5 themes, not by paper; dense
   with citations [n]; state the key quantitative benchmarks (record
   values, stability durations, scales) with numbers from the excerpts.
2. **Limitations and open challenges** - the specific gaps the field has
   not solved, each grounded in the excerpts [n].
3. **Beyond the state of the art** - how THE PROPOSED IDEA advances each
   gap: what will be done differently, why it is credible, and what
   quantitative advance it targets. Flag any first-ever claim with
   [AUTHOR: verify novelty].
4. A closing markdown table:
   | Aspect | State of the art today | This project's advance |

Rules: every factual claim about the field cites [n]; never invent
values or references; the "beyond" part may use the idea's own targets
but must not fabricate preliminary results; where the idea text lacks a
needed target, write [AUTHOR: add target]."""

WORKPLAN_SYSTEM = """\
You structure the implementation of a {instrument} proposal into a work
plan: exactly {n_wp} work packages over {months} months.

Produce (markdown):
1. **WP overview table**: | WP | Title | Months | Objective (one line) |
   Include a management WP and, where appropriate for the instrument, a
   dissemination & exploitation WP.
2. **Per work package**: heading "WPx - Title (Mstart-Mend)", the
   objective, then a task table
   | Task | Title | Months | Description (1-2 lines) |
   with 2-4 tasks each, and the WP's deliverables
   | Deliverable | Title | Month | Type (R/DEM/DEC/OTHER) |
3. **Milestone table** (5-8 project-level):
   | Milestone | Title | Month | Means of verification |
4. **Dependencies** - one short paragraph on the critical path.

Rules: build ONLY on the provided objectives/methodology - no invented
scientific content beyond reasonable task decomposition; keep numbering
consistent (T1.1, D2.3, MS4); spread deliverables and milestones
realistically across the {months} months; mark anything the author must
decide as [AUTHOR: ...]."""

PROP_FIXES_SYSTEM = """\
You turn a mock evaluation report into ready-to-paste revisions of the
proposal. Inputs: the proposal draft, the evaluation report, optionally
numbered literature excerpts.

For EVERY weakness, shortcoming, or point deduction in the report, in
the report's order:
### Weakness n
- **The finding** - quote or faithfully condense it.
- **The fix strategy** - 1-2 sentences.
- **Revised text** - the ready-to-paste replacement or insertion,
  marked with << >> around genuinely new wording; name where it goes
  (section/paragraph). Cite excerpts [n] where they support the fix.

End with a priority list: which three fixes move the score most.

Rules: never invent data, partners, or preliminary results - use
[AUTHOR: ...] placeholders where facts are needed; keep the proposal's
terminology and targets consistent; address every weakness, drop none."""

with tab_prop:
    st.markdown("Draft, refine, and **mock-evaluate grant proposals** "
                "(Horizon Europe, EIC, MSCA), grounded in your paper corpus "
                "and your **own past proposals and evaluation reports**.")
    st.caption("⚠️ Confidentiality: proposal text is sent to the Anthropic "
               "API for processing (not used for training by default). For "
               "consortium material under NDA, check you may process it via "
               "a cloud API. Criteria/thresholds evolve - always verify "
               "against the current call's work programme.")

    p1, p2 = st.columns([2, 2])
    with p1:
        instrument = st.selectbox("Instrument", list(INSTRUMENTS.keys()))
    with p2:
        prop_action = st.radio("Action",
                               ["Mock evaluation", "Call compliance",
                                "SOTA & beyond", "Work plan",
                                "Consistency audit", "Gantt & WP figures",
                                "Draft section",
                                "Complete / expand my draft",
                                "Refine section", "Revise full draft"],
                               horizontal=True)
    prop_where = restrict_search_widget("prop") if index_ok else None
    g_papers = st.checkbox("Ground in papers corpus", value=True,
                           disabled=not index_ok)
    g_prop = st.checkbox(
        "Use my past proposals & evaluations",
        value=True, disabled=not index_ok,
        help="Retrieves from the papers\\proposals, papers\\evaluations and "
             "papers\\proposal_docs folders (add files there and re-index).")

    criteria = INSTRUMENTS[instrument]

    def _prop_retrieve(query, k_papers=10, k_prop=8):
        hits = []
        if g_papers and index_ok:
            if do_rerank and api_key.strip():
                cand = retrieve(query, min(k_papers * 3, 40),
                                where_extra=prop_where)
                hits += rerank_hits(api_key.strip(), query, cand, k_papers)
            else:
                hits += retrieve(query, k_papers, where_extra=prop_where)
        if g_prop and index_ok:
            try:
                hits += retrieve(query, k_prop,
                                 where_extra={"source":
                                              {"$in": PROPOSAL_SOURCES}})
            except Exception:
                pass
        return hits

    # ---------------- Mock evaluation (ESR-calibrated) ----------------
    if prop_action == "Mock evaluation":
        render_calibrated_evaluation(instrument, criteria, _prop_retrieve)

    # ---------------- Consistency audit ----------------
    elif prop_action == "Consistency audit":
        render_consistency_audit(instrument)

    # ---------------- Gantt & WP figures ----------------
    elif prop_action == "Gantt & WP figures":
        render_workplan_figures()

    # ---------------- Call compliance ----------------
    elif prop_action == "Call compliance":
        st.caption("Checks your draft against the OFFICIAL call/topic text "
                   "(expected outcomes, scope, conditions). The mock ESR "
                   "judges quality - this judges FIT, which is where "
                   "proposals get ruled out.")
        cc_up = st.file_uploader("Proposal draft (docx/pdf/txt/md)",
                                 type=["docx", "pdf", "txt", "md"],
                                 key="cc_up")
        cc_call_up = st.file_uploader(
            "Call / topic text - the expected outcomes and scope from the "
            "work programme (docx/pdf/txt/md)",
            type=["docx", "pdf", "txt", "md"], key="cc_call_up")
        cc_call_paste = st.text_area("...and/or paste the call text here",
                                     height=140, key="cc_call_txt")
        call_text = ""
        if cc_call_up is not None:
            try:
                call_text = extract_uploaded_text(cc_call_up)[:40000]
            except Exception as e:
                st.error(f"Could not read the call text: {e}")
        if cc_call_paste.strip():
            call_text = (call_text + "\n\n"
                         + cc_call_paste.strip())[:40000]
        if st.button("🎯 Check compliance", type="primary", key="cc_go",
                     disabled=not (cc_up is not None
                                   and call_text.strip())):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                try:
                    cc_text = extract_uploaded_text(cc_up).strip()[:150000]
                except Exception as e:
                    st.error(f"Could not read '{cc_up.name}': {e}")
                    cc_text = ""
                if cc_text:
                    umsg = (f"OFFICIAL CALL TEXT:\n\n{call_text}\n\n"
                            f"THE PROPOSAL DRAFT ('{cc_up.name}'):\n\n"
                            f"{cc_text}")
                    with st.spinner(f"{model_label} is auditing the fit "
                                    "(Opus/Fable recommended)..."):
                        try:
                            rep = call_claude(api_key.strip(),
                                              COMPLIANCE_SYSTEM, umsg,
                                              model, max_tokens=8000)
                        except Exception as e:
                            st.error(f"Claude API error: {e}")
                            rep = None
                    if rep:
                        record_qa(f"[CALL COMPLIANCE] {cc_up.name}", rep,
                                  [], do_autosave)
                        st.markdown("---")
                        st.markdown(rep)

    # ---------------- SOTA & beyond ----------------
    elif prop_action == "SOTA & beyond":
        st.caption("Writes the 'State of the art and beyond' section from "
                   "your paper library: themed SOTA with citations, the "
                   "open gaps, and how YOUR idea advances each gap - "
                   "closing with a SOTA-vs-this-project table.")
        so_idea = st.text_area(
            "Your project idea (2-10 sentences)", height=120, key="so_idea",
            placeholder="e.g. Slot-die coated perovskite-silicon tandems on "
                        "industrial CZ wafers, targeting >28% at M36 via "
                        "additive-controlled crystallization and "
                        "vacuum-assisted drying...")
        so_up = st.file_uploader("Objectives / concept note (optional)",
                                 type=["docx", "pdf", "txt", "md"],
                                 key="so_up")
        so_extra = ""
        if so_up is not None:
            try:
                so_extra = extract_uploaded_text(so_up).strip()[:30000]
            except Exception as e:
                st.error(f"Could not read '{so_up.name}': {e}")
        sc1, sc2 = st.columns(2)
        with sc1:
            so_words = st.select_slider("Target length (words)",
                                        options=[500, 800, 1200, 1500],
                                        value=800, key="so_words")
        with sc2:
            so_breadth = st.slider("Evidence passages", 15, 40, 30,
                                   key="so_breadth")
        if st.button("🧭 Write SOTA & beyond", type="primary", key="so_go",
                     disabled=not so_idea.strip()):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                import json as _json
                idea = so_idea.strip()
                try:
                    raw = call_claude(api_key.strip(), DEEP_PLAN_SYSTEM,
                                      f"QUESTION: {idea}", model,
                                      max_tokens=400)
                    raw = re.sub(r"^```(json)?|```$", "", raw.strip(),
                                 flags=re.M).strip()
                    subs = [s for s in _json.loads(raw)
                            if isinstance(s, str)][:5]
                except Exception:
                    subs = []
                so_queries = [idea] + subs
                per_k = max(6, so_breadth // max(len(so_queries), 1) + 4)
                registry, so_hits = {}, []
                prog = st.progress(0.0, text="Scanning the library...")
                for si, sq in enumerate(so_queries, start=1):
                    prog.progress(si / (len(so_queries) + 1),
                                  text=f"Retrieving: {sq[:60]}")
                    for h in _prop_retrieve(sq, per_k, 4):
                        hkey = (h["meta"]["file"],
                                h["meta"]["page_start"], h["text"][:80])
                        if hkey not in registry:
                            registry[hkey] = 1
                            so_hits.append(h)
                so_hits = so_hits[:so_breadth]
                prog.progress(1.0, text="Writing the section...")
                umsg = (f"EXCERPTS:\n\n{build_context(so_hits)}\n\n"
                        f"THE PROPOSED PROJECT IDEA:\n{idea}"
                        + (f"\n\nADDITIONAL PROJECT MATERIAL:\n{so_extra}"
                           if so_extra else ""))
                try:
                    sota = call_claude(api_key.strip(),
                                       SOTA_SYSTEM.format(words=so_words),
                                       umsg, model, max_tokens=8000)
                except Exception as e:
                    st.error(f"Claude API error: {e}")
                    sota = None
                prog.empty()
                if sota:
                    record_qa(f"[SOTA & BEYOND] {idea[:60]}", sota,
                              so_hits, do_autosave)
                    st.session_state["last_sota"] = {"text": sota,
                                                     "hits": so_hits}
        if st.session_state.get("last_sota"):
            st.markdown("---")
            st.markdown(st.session_state["last_sota"]["text"])
            render_reference_exporter(
                st.session_state["last_sota"]["hits"], key="sotaref")

    # ---------------- Work plan ----------------
    elif prop_action == "Work plan":
        st.caption("Generates the implementation structure - work packages, "
                   "tasks, deliverables, milestones - from your objectives "
                   "and methodology. The output is a proposal skeleton, not "
                   "truth: adjust months and dependencies to your reality.")
        wp_up = st.file_uploader(
            "Objectives + methodology (docx/pdf/txt/md)",
            type=["docx", "pdf", "txt", "md"], key="wp_up")
        wp_paste = st.text_area("...and/or paste them here", height=140,
                                key="wp_txt")
        wp_src = ""
        if wp_up is not None:
            try:
                wp_src = extract_uploaded_text(wp_up).strip()[:60000]
            except Exception as e:
                st.error(f"Could not read '{wp_up.name}': {e}")
        if wp_paste.strip():
            wp_src = (wp_src + "\n\n" + wp_paste.strip())[:60000]
        wc1, wc2 = st.columns(2)
        with wc1:
            wp_n = st.slider("Work packages", 3, 8, 5, key="wp_n")
        with wc2:
            wp_months = st.selectbox("Duration (months)",
                                     [24, 36, 42, 48], index=1,
                                     key="wp_months")
        if st.button("🗂️ Generate work plan", type="primary", key="wp_go",
                     disabled=not wp_src.strip()):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                sys_p = WORKPLAN_SYSTEM.format(n_wp=wp_n,
                                               months=wp_months,
                                               instrument=instrument)
                with st.spinner(f"{model_label} is structuring the work "
                                "plan..."):
                    try:
                        wp_out = call_claude(
                            api_key.strip(), sys_p,
                            f"PROJECT MATERIAL:\n\n{wp_src}",
                            model, max_tokens=8000)
                    except Exception as e:
                        st.error(f"Claude API error: {e}")
                        wp_out = None
                if wp_out:
                    record_qa(f"[WORK PLAN - {instrument}] {wp_n} WPs / "
                              f"{wp_months} months", wp_out, [],
                              do_autosave)
                    st.markdown("---")
                    st.markdown(wp_out)

    # ---------------- Draft section(s) ----------------
    elif prop_action == "Draft section":
        st.caption("Upload what you already have (objectives, methodology, "
                   "concept notes) and tick the sections you still need — "
                   "they'll be drafted consistently with your material and "
                   "with each other.")
        pd_up = st.file_uploader(
            "Existing proposal material (optional)",
            type=["docx", "pdf", "txt", "md"], key="pd_up",
            help="e.g. your objectives and methodology. Drafted sections stay "
                 "consistent with this: same targets, terminology and "
                 "numbers, and they won't repeat what it already covers.")
        have_text = ""
        if pd_up is not None:
            try:
                have_text = extract_uploaded_text(pd_up).strip()[:80000]
                st.caption(f"{pd_up.name} — {len(have_text.split()):,} words "
                           "will be used as context.")
            except Exception as e:
                st.error(f"Could not read '{pd_up.name}': {e}")

        pd_sections = st.multiselect(
            "Sections to draft", list(PROPOSAL_SECTIONS.keys()),
            default=["State of the art & beyond"], key="pd_sections",
            help="Pick several — each is written separately, told what the "
                 "others cover so they don't overlap.")
        pd_brief = st.text_area(
            "Brief / extra context (optional if you uploaded material)",
            height=100, key="pd_brief",
            placeholder="e.g. SoA and beyond for fully evaporated "
                        "perovskite-Si tandems; our target: >28% at "
                        ">100 cm2 with <5% relative loss after 1000 h "
                        "damp heat.")
        pd_words = st.slider("Target length per section (words)",
                             200, 2000, 600, step=50, key="pd_words")

        ready = bool(pd_sections) and (pd_brief.strip() or have_text)
        n_sec = len(pd_sections)
        label = (f"Draft {n_sec} section{'s' if n_sec != 1 else ''}"
                 if n_sec else "Draft sections")
        if st.button(label, type="primary", disabled=not ready):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                bar = st.progress(0.0, text="Drafting…")
                results, all_hits = [], []
                for si, sect in enumerate(pd_sections, start=1):
                    bar.progress((si - 1) / n_sec,
                                 text=f"Section {si}/{n_sec}: {sect}")
                    seed = " ".join(filter(None, [
                        sect, pd_brief.strip(), have_text[:800]]))
                    hits = _prop_retrieve(seed, 14, 8)
                    all_hits.extend(hits)
                    ctx = ("\n\nEXCERPTS:\n\n" + build_context(hits)
                           if hits else "")
                    sys_p = (PROP_DRAFT_SYSTEM.format(instrument=instrument,
                                                      criteria=criteria)
                             + f"\n\nSECTION TYPE: {sect}\n"
                             + PROPOSAL_SECTIONS[sect]
                             + f"\nTarget length: about {pd_words} words.")
                    if n_sec > 1:
                        others = [s for s in pd_sections if s != sect]
                        sys_p += (
                            "\n\nThis section belongs to a set being drafted "
                            "together: " + "; ".join(pd_sections)
                            + f".\nWrite ONLY '{sect}'. The other sections "
                            f"({', '.join(others)}) are written separately - "
                            "do not cover or repeat their content, but stay "
                            "consistent with them.")
                    if have_text:
                        sys_p += (
                            "\n\nThe author has supplied existing proposal "
                            "material. Stay consistent with it: reuse its "
                            "terminology, targets and numbers, never "
                            "contradict it, and do not re-explain what it "
                            "already covers. Refer to it where the section "
                            "logically builds on it.")
                    user = f"SECTION BRIEF: {pd_brief.strip() or sect}"
                    if have_text:
                        user += ("\n\nEXISTING PROPOSAL MATERIAL FROM THE "
                                 f"AUTHOR ('{pd_up.name}'):\n\n{have_text}")
                    user += ctx
                    try:
                        sec_text = call_claude(
                            api_key.strip(), sys_p, user, model,
                            max_tokens=min(int(pd_words * 2.5) + 800, 12000))
                    except Exception as e:
                        st.error(f"Claude API error on '{sect}': {e}")
                        sec_text = None
                    if sec_text:
                        results.append((sect, sec_text))
                bar.empty()

                if results:
                    combined = "\n\n".join(f"## {s}\n\n{t}"
                                           for s, t in results)
                    qa = record_qa(
                        f"[PROPOSAL DRAFT - {instrument}] "
                        + ", ".join(s for s, _ in results),
                        combined, all_hits, do_autosave)
                    st.markdown("---")
                    st.success(f"Drafted {len(results)} section(s).")
                    for sect, text in results:
                        with st.expander(sect, expanded=len(results) == 1):
                            st.markdown(text)
                    st.download_button(
                        "⬇️ All sections as Word",
                        data=qa_to_docx_bytes([qa],
                                              title="Proposal sections"),
                        file_name="proposal_sections.docx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document")
                    if all_hits:
                        show_sources(qa["hits"], key_prefix="pd")
                    st.caption("Raw material — rework it in your own voice, "
                               "and replace [n] with your real reference "
                               "numbers before submission.")

    # ---------------- Revise full draft ----------------
    elif prop_action == "Revise full draft":
        st.caption("Upload your **complete proposal draft** and revise it as "
                   "a whole. Paste evaluator or colleague feedback (e.g. a "
                   "mock ESR from this tab) and the revision targets exactly "
                   "what was criticised.")
        rv_up = st.file_uploader("Full proposal draft",
                                 type=["docx", "pdf", "txt", "md"],
                                 key="rv_up")
        e1, e2 = st.columns(2)
        with e1:
            rv_esr_up = st.file_uploader(
                "ESR / evaluation report (file, optional)",
                type=["docx", "pdf", "txt", "md"], key="rv_esr_up",
                help="Upload the real ESR PDF or a mock one from this tab.")
        with e2:
            rv_mat_ups = st.file_uploader(
                "Material to incorporate (optional, several files ok)",
                type=["docx", "pdf", "txt", "md"], key="rv_mat_ups",
                accept_multiple_files=True,
                help="New results, partially rewritten sections, colleague "
                     "edits, notes — the revision weaves them into the "
                     "right place; where they conflict with the draft, "
                     "your new material wins.")
        rv_feedback = st.text_area(
            "…or paste feedback here (adds to the uploaded ESR)",
            height=100, key="rv_feedback",
            placeholder="Paste ESR weaknesses, reviewer comments, or your "
                        "own notes on what must improve…")
        rv_focus = st.text_input(
            "Revision goals (optional)",
            placeholder="e.g. quantify all impact claims; tighten Excellence "
                        "to 12 pages; align KPIs across sections",
            key="rv_focus")
        rv_mode = st.radio(
            "Output",
            ["Revision plan (fast — one call)",
             "Full rewrite (section by section)"],
            horizontal=True, key="rv_mode",
            help="The plan tells you exactly what to change, with rewrites "
                 "for the key passages — best first step. The full rewrite "
                 "revises the entire text part by part and reassembles it; "
                 "longer and costs more, changes marked in bold.")

        rv_text, rv_name = "", ""
        if rv_up is not None:
            try:
                rv_text = extract_uploaded_text(rv_up).strip()
                rv_name = rv_up.name
                n_parts = len(split_proposal(rv_text))
                st.caption(f"{rv_name} — {len(rv_text.split()):,} words"
                           + (f" · full rewrite ≈ {n_parts} calls"
                              if "rewrite" in rv_mode else ""))
            except Exception as e:
                st.error(f"Could not read '{rv_up.name}': {e}")

        rv_esr_text = ""
        if rv_esr_up is not None:
            try:
                rv_esr_text = extract_uploaded_text(rv_esr_up).strip()[:40000]
                st.caption(f"ESR: {rv_esr_up.name} — "
                           f"{len(rv_esr_text.split()):,} words")
            except Exception as e:
                st.error(f"Could not read ESR '{rv_esr_up.name}': {e}")

        rv_material = ""
        if rv_mat_ups:
            mat_parts, budget = [], 60000
            for mf in rv_mat_ups:
                try:
                    mtxt = extract_uploaded_text(mf).strip()
                except Exception as e:
                    st.error(f"Could not read '{mf.name}': {e}")
                    continue
                take = mtxt[:min(len(mtxt), budget)]
                if not take:
                    continue
                mat_parts.append(f"--- FILE: {mf.name} ---\n{take}")
                budget -= len(take)
                if budget <= 0:
                    st.warning("Material capped at ~60k characters — "
                               "largest files were truncated.")
                    break
            rv_material = "\n\n".join(mat_parts)
            if rv_material:
                st.caption(f"Material: {len(rv_mat_ups)} file(s), "
                           f"{len(rv_material.split()):,} words loaded")

        if st.button("Revise proposal", type="primary", disabled=not rv_text):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                feedback_all = "\n\n".join(
                    t for t in (rv_esr_text, rv_feedback.strip()) if t)
                gseed = (feedback_all or rv_focus.strip() or rv_text[:1500])
                hits = _prop_retrieve(gseed[:1500], 10, 6)
                ctx = ("\n\nSUPPORTING EXCERPTS (cite as [n]):\n\n"
                       + build_context(hits) if hits else "")
                goals = ""
                if feedback_all:
                    goals += (f"\n\nEVALUATOR FEEDBACK TO ADDRESS:\n"
                              f"{feedback_all}")
                if rv_focus.strip():
                    goals += f"\n\nREVISION GOALS: {rv_focus.strip()}"
                if rv_material:
                    goals += ("\n\nAUTHOR-SUPPLIED REVISION MATERIAL TO "
                              "INCORPORATE (where it conflicts with the "
                              "draft, this material wins):\n\n" + rv_material)

                if rv_mode.startswith("Revision plan"):
                    sys_p = PROP_REVISE_PLAN_SYSTEM.format(
                        instrument=instrument, criteria=criteria)
                    body = (f"THE FULL DRAFT ('{rv_name}'):\n\n"
                            f"{rv_text[:180000]}{goals}{ctx}")
                    with st.spinner(f"{model_label} is building the "
                                    "revision plan…"):
                        try:
                            out = call_claude(api_key.strip(), sys_p, body,
                                              model, max_tokens=12000)
                        except Exception as e:
                            st.error(f"Claude API error: {e}")
                            out = None
                    if out:
                        qa = record_qa(f"[REVISION PLAN - {instrument}] "
                                       f"{rv_name}", out, hits, do_autosave)
                        st.markdown("---")
                        st.markdown(out)
                        st.download_button(
                            "⬇️ Plan as Word",
                            data=qa_to_docx_bytes([qa],
                                                  title="Revision plan"),
                            file_name="revision_plan.docx",
                            mime="application/vnd.openxmlformats-"
                                 "officedocument.wordprocessingml.document")
                        if hits:
                            show_sources(qa["hits"], key_prefix="rv")

                else:  # Full rewrite, part by part
                    parts = split_proposal(rv_text)
                    sys_p = PROP_REWRITE_CHUNK_SYSTEM.format(
                        instrument=instrument, criteria=criteria)
                    bar = st.progress(0.0, text="Rewriting…")
                    revised, failed = [], 0
                    for pi, part in enumerate(parts, start=1):
                        bar.progress((pi - 1) / len(parts),
                                     text=f"Part {pi}/{len(parts)}…")
                        prev_tail = (revised[-1][-1200:] if revised else "")
                        body = (f"{goals}{ctx}\n\n"
                                + (f"TAIL OF THE PREVIOUS (ALREADY REVISED) "
                                   f"PART, FOR CONTINUITY:\n…{prev_tail}\n\n"
                                   if prev_tail else "")
                                + f"PART {pi} OF {len(parts)} TO REVISE:\n\n"
                                + part)
                        try:
                            rev = call_claude(api_key.strip(), sys_p, body,
                                              model, max_tokens=8000)
                            revised.append(rev.strip())
                        except Exception as e:
                            st.warning(f"Part {pi} failed ({e}) — keeping "
                                       "the original text for that part.")
                            revised.append(part)
                            failed += 1
                    bar.empty()
                    full = "\n\n".join(revised)
                    qa = record_qa(f"[FULL REWRITE - {instrument}] {rv_name}",
                                   full, hits, do_autosave)
                    st.markdown("---")
                    st.success(f"Rewrote {len(parts) - failed} of "
                               f"{len(parts)} parts. Changes are in bold; "
                               "[AUTHOR: …] marks where your input is "
                               "needed.")
                    with st.expander("Read the revised draft",
                                     expanded=False):
                        st.markdown(full)
                    st.download_button(
                        "⬇️ Revised draft as Word",
                        data=qa_to_docx_bytes([qa], title="Revised proposal"),
                        file_name="revised_proposal.docx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document")
                    if hits:
                        show_sources(qa["hits"], key_prefix="rv")

    # -------- Complete / expand my draft, and Refine section --------
    else:
        completing = prop_action.startswith("Complete")
        if completing:
            st.caption("Upload or paste a **partial** section — bullets, "
                       "notes, half-written prose, [TODO] gaps — and get it "
                       "finished. Your own sentences are preserved; anything "
                       "newly written is marked so you can review it.")
        else:
            st.caption("Upload or paste a section you've written and get it "
                       "sharpened against the instrument's criteria.")

        src = st.radio("Input", ["Upload a file", "Paste text"],
                       horizontal=True, key="pc_src")
        pr_text, src_name = "", ""
        if src == "Upload a file":
            pc_up = st.file_uploader("Partial draft / section",
                                     type=["docx", "pdf", "txt", "md"],
                                     key="pc_up")
            if pc_up is not None:
                try:
                    pr_text = extract_uploaded_text(pc_up).strip()
                    src_name = pc_up.name
                    st.caption(f"{pc_up.name} — {len(pr_text.split()):,} words")
                except Exception as e:
                    st.error(f"Could not read '{pc_up.name}': {e}")
        else:
            pr_text = st.text_area(
                "Your text", height=220, key="pc_text",
                placeholder="Paste your partial section — bullets and notes "
                            "are fine.").strip()
            src_name = "pasted text"

        c1, c2 = st.columns([2, 1])
        with c1:
            pr_focus = st.text_input(
                "Optional instructions",
                placeholder=("e.g. keep my objectives, expand the methodology"
                             if completing
                             else "e.g. objectives are too vague; "
                                  "strengthen KPIs"),
                key="pc_focus")
        with c2:
            pc_sect = st.selectbox("Section type",
                                   ["(auto)"] + list(PROPOSAL_SECTIONS.keys()),
                                   key="pc_sect")
        pc_words = 0
        if completing:
            pc_words = st.slider("Target length (words)", 300, 3000, 900,
                                 step=100, key="pc_words")

        btn_label = ("Complete my draft" if completing else "Refine section")
        if st.button(btn_label, type="primary", disabled=not pr_text):
            if not api_key.strip():
                st.error("Needs the API key (sidebar).")
            else:
                hits = _prop_retrieve(pr_text[:1500], 12 if completing else 8,
                                      8 if completing else 6)
                ctx = ("\n\nSUPPORTING EXCERPTS:\n\n" + build_context(hits)
                       if hits else "")
                if completing:
                    sys_p = PROP_COMPLETE_SYSTEM.format(instrument=instrument,
                                                        criteria=criteria)
                    sys_p += f"\nTarget length: about {pc_words} words."
                else:
                    sys_p = PROP_REFINE_SYSTEM.format(instrument=instrument,
                                                      criteria=criteria)
                if pc_sect != "(auto)":
                    sys_p += (f"\n\nSECTION TYPE: {pc_sect}\n"
                              + PROPOSAL_SECTIONS[pc_sect])
                if pr_focus.strip():
                    sys_p += f"\nAuthor's instructions: {pr_focus.strip()}"
                verb = "completing" if completing else "refining"
                with st.spinner(f"{model_label} is {verb}..."):
                    try:
                        ref_text = call_claude(
                            api_key.strip(), sys_p,
                            f"THE AUTHOR'S DRAFT ({src_name}):\n\n"
                            f"{pr_text[:120000]}{ctx}",
                            model,
                            max_tokens=(min(int(pc_words * 3) + 1000, 12000)
                                        if completing else 8000))
                    except Exception as e:
                        st.error(f"Claude API error: {e}")
                        ref_text = None
                if ref_text:
                    tag = "COMPLETE" if completing else "REFINE"
                    qa = record_qa(f"[PROPOSAL {tag} - {instrument}] "
                                   f"{src_name}", ref_text, hits, do_autosave)
                    st.markdown("---")
                    if completing:
                        st.caption("Text wrapped in << >> is newly written — "
                                   "review it before it goes into the "
                                   "proposal. Everything else is yours.")
                    st.markdown(ref_text)
                    st.download_button(
                        "⬇️ Section as Word",
                        data=qa_to_docx_bytes([qa], title="Proposal section"),
                        file_name=f"proposal_{tag.lower()}.docx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document")
                    if hits:
                        show_sources(qa["hits"], key_prefix="pc")

# ------------------------------ REVIEW ------------------------------------
with tab_review:
    st.markdown("Upload a **draft manuscript, thesis chapter, abstract, or "
                "response letter** and get expert feedback or revisions. "
                "Optionally the review is grounded in **your own corpus**, so "
                "novelty and positioning are judged against the papers you "
                "have actually indexed.")
    rw_mode = st.radio(
        "Mode", ["Review / respond (report, line edits, structure, polish, rebuttal)",
                 "🧬 Rewrite for a high-impact journal (manuscript + SI + files, staged)",
                 "📨 Respond to reviewers (staged: points → grounded responses → "
                 "applied changes → before/after diff)",
                 "🔍 Check my revision (my revised manuscript + my response letter vs the reviews)"],
        horizontal=True, key="rev_mode")
    if rw_mode.startswith("🧬"):
        render_rewrite_panel()
    elif rw_mode.startswith("📨"):
        render_revision_panel()
    elif rw_mode.startswith("🔍"):
        render_revision_check_panel()
    else:
        up = st.file_uploader("Document to review",
                              type=["docx", "pdf", "txt", "md"],
                              help="Word, PDF, or plain text / markdown.")
        RESP_MODE = "Response to reviewers (point-by-point letter)"
        r1, r2 = st.columns([2, 2])
        with r1:
            review_mode = st.selectbox("Review type",
                                       list(REVIEW_MODES.keys()) + [RESP_MODE])
        with r2:
            review_focus = st.text_input(
                "Optional instructions to the reviewer",
                placeholder="e.g. focus on the discussion; target journal: Joule")
        reviewer_comments = ""
        if review_mode == RESP_MODE:
            st.info("Upload your **manuscript** above, and provide the "
                    "**reviewers' comments** below. You'll get a point-by-point "
                    "response letter with [AUTHOR ACTION] placeholders where "
                    "your input is needed.")
            rc_up = st.file_uploader("Reviewer comments (file, optional)",
                                     type=["docx", "pdf", "txt", "md"],
                                     key="rc_up")
            reviewer_comments = st.text_area(
                "Reviewer comments (paste here, or upload above)", height=160,
                placeholder="Paste the decision letter / reviewer comments...")
            if rc_up is not None and not reviewer_comments.strip():
                try:
                    reviewer_comments = extract_uploaded_text(rc_up)
                    st.caption(f"Using comments from '{rc_up.name}' "
                               f"({len(reviewer_comments.split()):,} words).")
                except Exception as e:
                    st.error(f"Could not read '{rc_up.name}': {e}")
        ground = st.checkbox(
            "Ground the review in my corpus",
            value=False, disabled=not index_ok,
            help="Retrieves the most relevant passages from your indexed papers "
                 "and asks the reviewer to judge novelty/positioning against "
                 "them and flag missing comparisons.")

        if st.button("Review document", type="primary", disabled=up is None):
            if not api_key.strip():
                st.error("Reviewing needs the API key (sidebar).")
            elif review_mode == RESP_MODE and not reviewer_comments.strip():
                st.error("Paste or upload the reviewer comments first.")
            else:
                try:
                    doc_text = extract_uploaded_text(up).strip()
                except Exception as e:
                    st.error(f"Could not read '{up.name}': {e}")
                    doc_text = ""
                n_words = len(doc_text.split())
                if doc_text and n_words < 30:
                    st.warning("Almost no text found in this file (scanned PDF? "
                               "needs OCR).")
                elif doc_text:
                    st.caption(f"{up.name} - {n_words:,} words")
                    MAX_CHARS = 150_000
                    if len(doc_text) > MAX_CHARS:
                        st.warning(f"Document is very long; reviewing the first "
                                   f"{MAX_CHARS:,} characters "
                                   f"(~{len(doc_text.split()):,} words total).")
                        doc_text = doc_text[:MAX_CHARS]

                    focus_note = (f"\n\nAUTHOR'S INSTRUCTIONS TO THE REVIEWER: "
                                  f"{review_focus.strip()}"
                                  if review_focus.strip() else "")

                    hits = []
                    if review_mode == "Revise & polish (rewrites the text)":
                        # Chunked rewrite so long documents fit the output window.
                        CH = 9000
                        pieces = [doc_text[i:i + CH]
                                  for i in range(0, len(doc_text), CH)]
                        bar = st.progress(0.0, text="Revising...")
                        revised_parts = []
                        try:
                            for pi, piece in enumerate(pieces, start=1):
                                bar.progress(pi / len(pieces),
                                             text=f"Revising part {pi}/{len(pieces)}...")
                                out = call_claude(
                                    api_key.strip(),
                                    REVIEW_MODES[review_mode] + focus_note,
                                    f"Passage to revise:\n\n{piece}",
                                    model, max_tokens=8000)
                                revised_parts.append(out.strip())
                            review_text = "\n\n".join(revised_parts)
                        except Exception as e:
                            st.error(f"Claude API error: {e}")
                            review_text = None
                        finally:
                            bar.empty()
                    else:
                        is_resp = review_mode == RESP_MODE
                        corpus_ctx = ""
                        if ground and index_ok:
                            ret_query = (reviewer_comments[:1500] if is_resp
                                         else doc_text[:1500])
                            with st.spinner("Retrieving related work from your "
                                            "corpus..."):
                                hits = retrieve(ret_query, min(top_k, 10))
                            if hits:
                                corpus_ctx = (
                                    "\n\nRELATED PASSAGES FROM THE AUTHOR'S "
                                    "LITERATURE CORPUS (cite as [n]"
                                    + ("; use them as evidence in rebuttals"
                                       if is_resp else
                                       "; use these to judge novelty and "
                                       "positioning and flag comparisons the "
                                       "manuscript should make but doesn't")
                                    + "):\n\n" + build_context(hits))
                        if is_resp:
                            sys_prompt = RESPONSE_SYSTEM + focus_note
                            body = (f"REVIEWER COMMENTS:\n\n{reviewer_comments}"
                                    f"\n\nTHE MANUSCRIPT ('{up.name}'):\n\n"
                                    f"{doc_text}{corpus_ctx}")
                            spin = f"{model_label} is drafting the response..."
                        else:
                            sys_prompt = REVIEW_MODES[review_mode] + focus_note
                            body = (f"THE DOCUMENT UNDER REVIEW "
                                    f"('{up.name}'):\n\n{doc_text}{corpus_ctx}")
                            spin = f"{model_label} is reviewing..."
                        with st.spinner(spin):
                            try:
                                review_text = call_claude(
                                    api_key.strip(), sys_prompt, body,
                                    model, max_tokens=8000)
                            except Exception as e:
                                st.error(f"Claude API error: {e}")
                                review_text = None

                    if review_text:
                        qa = record_qa(f"[REVIEW - {review_mode}] {up.name}",
                                       review_text, hits, do_autosave)
                        st.markdown("---")
                        st.markdown(review_text)
                        if hits:
                            show_sources(qa["hits"], key_prefix="rev")
                        st.download_button(
                            "⬇️ Review as Word",
                            data=qa_to_docx_bytes([qa], title="Document Review"),
                            file_name=f"review_{up.name.rsplit('.', 1)[0]}.docx",
                            mime="application/vnd.openxmlformats-officedocument"
                                 ".wordprocessingml.document")
                        st.caption("Saved to the answers folder and the session "
                                   "history in the Ask tab.")

# --------------------------- CLAIM CHECKER --------------------------------
with tab_claims:
    st.markdown("Paste draft sentences (one claim per line). Each is checked "
                "against your corpus and judged **SUPPORTED / PARTIALLY "
                "SUPPORTED / CONTRADICTED / NOT FOUND** with cited evidence.")
    claims_text = st.text_area("Claims", height=140, key="claims_input",
                               placeholder="Slot-die coated modules above 20% "
                                           "remain rare beyond 100 cm2.\n"
                                           "PEAI passivation improves Voc in "
                                           "p-i-n devices.")
    if st.button("Check claims", type="primary", disabled=not index_ok):
        if not api_key.strip():
            st.error("Claim checking needs the API key (sidebar).")
        else:
            claims = [c.strip() for c in claims_text.splitlines() if c.strip()]
            report_lines = [f"# Claim check - "
                            f"{datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
            for ci, claim in enumerate(claims, start=1):
                with st.spinner(f"Checking claim {ci}/{len(claims)}..."):
                    hits = retrieve(claim, top_k)
                    user_msg = (f"Excerpts:\n\n{build_context(hits)}\n\n"
                                f"CLAIM: {claim}")
                    try:
                        verdict = call_claude(api_key.strip(), CLAIM_SYSTEM,
                                              user_msg, model)
                    except Exception as e:
                        st.error(f"Claude API error: {e}")
                        break
                v_upper = verdict.upper()
                icon = ("✅" if "VERDICT: SUPPORTED" in v_upper
                        else "🟡" if "PARTIALLY" in v_upper
                        else "❌" if "CONTRADICTED" in v_upper
                        else "❓")
                st.markdown(f"### {icon} Claim {ci}: *{claim}*")
                st.markdown(verdict)
                show_sources(hits, key_prefix=f"c{ci}")
                report_lines += [f"## Claim {ci}: {claim}", "", verdict, ""]
            if len(report_lines) > 2:
                st.download_button(
                    "⬇️ Download claim report (.md)",
                    data="\n".join(report_lines),
                    file_name=f"claims_{datetime.datetime.now():%Y%m%d_%H%M}.md",
                    mime="text/markdown")

# ------------------------------ EXTRACT -----------------------------------
with tab_extract:
    st.markdown("Extract **structured data from papers into a table** - e.g. "
                "device metrics across your corpus for a benchmarking or "
                "review table. Values are taken strictly from each paper's "
                "text; missing values come back as 'n/a'.")
    if not index_ok:
        st.info("Build the index first.")
    else:
        ex_rows = library_table()
        ex_by_file = {r["File"]: r for r in ex_rows}
        ex_scope = st.radio("Choose papers",
                            ["Best match to a query", "Pick from library"],
                            horizontal=True)
        sel_papers = []          # (sig, title, file)
        if ex_scope == "Best match to a query":
            c1, c2 = st.columns([3, 1])
            with c1:
                exq = st.text_input("Topic query",
                                    placeholder="e.g. slot-die coated "
                                                "perovskite modules")
            with c2:
                exn = st.slider("Papers", 2, 30, 8)
            if exq.strip():
                seen_sigs = []
                for h in retrieve(exq.strip(), 80):
                    s = h["meta"]["doc_sig"]
                    if s not in [x[0] for x in seen_sigs]:
                        seen_sigs.append((s, h["meta"]["title"],
                                          h["meta"]["file"]))
                    if len(seen_sigs) >= exn:
                        break
                sel_papers = seen_sigs
                with st.expander(f"{len(sel_papers)} paper(s) selected"):
                    for _, t, f in sel_papers:
                        st.markdown(f"- {t}  (`{f}`)")
        else:
            picked = st.multiselect("Papers", list(ex_by_file.keys()))
            sel_papers = [(ex_by_file[f]["sig"], ex_by_file[f]["Title"], f)
                          for f in picked]

        use_preset = st.checkbox("Photovoltaic device preset fields",
                                 value=True)
        custom_fields = st.text_input(
            "Extra fields (comma-separated)",
            placeholder="e.g. hole transport layer, encapsulation method")
        ex_fields = ((PV_PRESET_FIELDS if use_preset else [])
                     + [c.strip() for c in custom_fields.split(",")
                        if c.strip()])

        if st.button("Extract table", type="primary",
                     disabled=not sel_papers or not ex_fields):
            if not api_key.strip():
                st.error("Extraction needs the API key (sidebar).")
            else:
                import json as _json
                bar = st.progress(0.0, text="Extracting...")
                table = []
                for pi, (sig, title, f) in enumerate(sel_papers, start=1):
                    bar.progress(pi / len(sel_papers),
                                 text=f"{pi}/{len(sel_papers)}: {title[:60]}")
                    text = paper_full_text(sig, 45000)
                    user_msg = (f"FIELDS: {_json.dumps(ex_fields)}\n\n"
                                f"PAPER TEXT ('{title}'):\n\n{text}")
                    row = {"Paper": title, "File": f}
                    try:
                        raw = call_claude(api_key.strip(), EXTRACT_SYSTEM,
                                          user_msg, model, max_tokens=1500)
                        raw = re.sub(r"^```(json)?|```$", "", raw.strip(),
                                     flags=re.M).strip()
                        data = _json.loads(raw)
                        for fld in ex_fields:
                            row[fld] = str(data.get(fld, "n/a"))
                    except Exception as e:
                        for fld in ex_fields:
                            row[fld] = ""
                        row[ex_fields[0]] = f"[error: {e}]"
                    table.append(row)
                bar.empty()
                st.session_state["extract_table"] = table
                save_extraction_state(table,
                                      st.session_state.get("mywork_rows"))
                if do_autosave:
                    try:
                        base = autosave_extraction(table)
                        st.toast(f"Table auto-saved: answers\\{base}"
                                 f".docx + .csv")
                    except Exception as e:
                        st.warning(f"Auto-save failed: {e}")

        ex_table = st.session_state.get("extract_table")
        if not ex_table:
            _saved = load_extraction_state()
            if _saved and _saved.get("table"):
                ex_table = _saved["table"]
                st.session_state["extract_table"] = ex_table
                if _saved.get("mywork"):
                    st.session_state.setdefault("mywork_rows",
                                                _saved["mywork"])
                st.caption("♻️ Restored your last extraction table "
                           "(auto-saved to the answers folder).")
        if ex_table:
            st.dataframe(ex_table, use_container_width=True)
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            writer = _csv.DictWriter(buf,
                                     fieldnames=list(ex_table[0].keys()))
            writer.writeheader()
            writer.writerows(ex_table)
            e1, e2 = st.columns(2)
            with e1:
                st.download_button(
                    "⬇️ Table as CSV (opens in Excel)",
                    data="\ufeff" + buf.getvalue(),
                    file_name="extraction.csv", mime="text/csv",
                    use_container_width=True)
            with e2:
                try:
                    import pandas as _pd
                    xbuf = _io.BytesIO()
                    _pd.DataFrame(ex_table).to_excel(xbuf, index=False)
                    st.download_button(
                        "⬇️ Table as Excel (.xlsx)", data=xbuf.getvalue(),
                        file_name="extraction.xlsx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".spreadsheetml.sheet",
                        use_container_width=True)
                except Exception:
                    st.caption("(.xlsx export needs `pip install openpyxl`)")
            st.caption("Verify extracted values against the sources before "
                       "using them in a manuscript - extraction is faithful "
                       "but not infallible.")

            # ---- Compare with your own lab data --------------------------
            st.markdown("---")
            st.markdown("### 🆚 Compare with your lab data")
            st.caption("Enter your own device values below ('This work'). "
                       "Add extra rows for more devices. The combined table "
                       "and a benchmarking paragraph are export-ready for a "
                       "manuscript.")
            field_cols = [c for c in ex_table[0].keys() if c != "File"]
            if "mywork_rows" not in st.session_state:
                st.session_state["mywork_rows"] = [
                    {c: ("This work" if c == "Paper" else "")
                     for c in field_cols}]
            if hasattr(st, "data_editor"):
                my_rows = st.data_editor(st.session_state["mywork_rows"],
                                         num_rows="dynamic",
                                         use_container_width=True,
                                         key="mywork_editor")
            else:
                st.warning("Editable table needs a newer Streamlit "
                           "(`pip install -U streamlit`). Paste values as "
                           "'field: value' lines instead.")
                pasted = st.text_area("Your values", height=120,
                                      key="mywork_paste",
                                      placeholder="PCE (%): 23.4\n"
                                                  "Voc (V): 1.18")
                row = {c: ("This work" if c == "Paper" else "")
                       for c in field_cols}
                for ln in pasted.splitlines():
                    if ":" in ln:
                        fkey, val = ln.split(":", 1)
                        for c in field_cols:
                            if c.lower().startswith(fkey.strip().lower()[:8]):
                                row[c] = val.strip()
                my_rows = [row]
            my_valid = [r for r in my_rows
                        if any(str(r.get(c, "")).strip()
                               for c in field_cols if c != "Paper")]
            if my_valid:
                # Keep lab-data rows restorable across restarts too.
                save_extraction_state(ex_table, my_rows)

            # Combined, numbered table: literature rows get [n], yours "-".
            combined = []
            for ci, r in enumerate(ex_table, start=1):
                combined.append({"Ref": f"[{ci}]",
                                 **{c: r.get(c, "") for c in field_cols}})
            for r in my_valid:
                combined.append({"Ref": "-",
                                 **{c: str(r.get(c, "")) for c in field_cols},
                                 "Paper": r.get("Paper") or "This work"})
            if my_valid:
                st.dataframe(combined, use_container_width=True)

                exp_cols = st.multiselect(
                    "Columns for the Word table", ["Ref"] + field_cols,
                    default=["Ref"] + field_cols, key="cmp_cols")

                cc1, cc2, cc3 = st.columns(3)
                with cc1:
                    if st.button("🧠 Write benchmarking paragraph",
                                 use_container_width=True):
                        if not api_key.strip():
                            st.error("Needs the API key (sidebar).")
                        else:
                            import json as _json2
                            tbl_txt = "\n".join(
                                _json2.dumps(row, ensure_ascii=False)
                                for row in combined)
                            with st.spinner(f"{model_label} is comparing..."):
                                try:
                                    analysis = call_claude(
                                        api_key.strip(), COMPARE_SYSTEM,
                                        f"COMPARISON TABLE (one JSON row per "
                                        f"line):\n\n{tbl_txt}",
                                        model, max_tokens=2000)
                                    st.session_state["cmp_analysis"] = analysis
                                    record_qa("[COMPARISON] This work vs "
                                              "literature", analysis, [],
                                              do_autosave)
                                    if do_autosave:
                                        try:
                                            base = autosave_extraction(
                                                combined, kind="comparison",
                                                extra_heading="Benchmarking "
                                                "discussion (draft)",
                                                extra_text=analysis)
                                            st.toast("Comparison auto-saved: "
                                                     f"answers\\{base}.docx")
                                        except Exception as e:
                                            st.warning(f"Auto-save failed: "
                                                       f"{e}")
                                except Exception as e:
                                    st.error(f"Claude API error: {e}")
                with cc2:
                    import csv as _csv2
                    import io as _io2
                    cbuf = _io2.StringIO()
                    cw = _csv2.DictWriter(cbuf,
                                          fieldnames=list(combined[0].keys()))
                    cw.writeheader()
                    cw.writerows(combined)
                    st.download_button("⬇️ Combined CSV",
                                       data="\ufeff" + cbuf.getvalue(),
                                       file_name="comparison.csv",
                                       mime="text/csv",
                                       use_container_width=True)
                with cc3:
                    cdoc = Document()
                    cdoc.add_heading("Comparison with literature", level=2)
                    wcols = exp_cols or ["Ref"] + field_cols
                    wtable = cdoc.add_table(rows=1 + len(combined),
                                            cols=len(wcols))
                    wtable.style = "Table Grid"
                    for j, c in enumerate(wcols):
                        cell = wtable.rows[0].cells[j]
                        cell.text = c
                        for p in cell.paragraphs:
                            for run in p.runs:
                                run.font.bold = True
                    for i2, row in enumerate(combined, start=1):
                        is_mine = row["Ref"] == "-"
                        for j, c in enumerate(wcols):
                            cell = wtable.rows[i2].cells[j]
                            cell.text = str(row.get(c, ""))
                            if is_mine:
                                for p in cell.paragraphs:
                                    for run in p.runs:
                                        run.font.bold = True
                    cdoc.add_paragraph(
                        "Table X. Comparison of this work with literature. "
                        "Replace [n] with the manuscript's reference "
                        "numbers.")
                    an = st.session_state.get("cmp_analysis")
                    if an:
                        cdoc.add_heading("Benchmarking discussion (draft)",
                                         level=3)
                        for para in an.split("\n"):
                            if para.strip():
                                cdoc.add_paragraph(para.strip())
                    dbuf = io.BytesIO()
                    cdoc.save(dbuf)
                    dbuf.seek(0)
                    st.download_button(
                        "⬇️ Word table (.docx)", data=dbuf,
                        file_name="comparison_table.docx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document",
                        use_container_width=True)
                an = st.session_state.get("cmp_analysis")
                if an:
                    st.markdown("---")
                    st.markdown(an)

# ------------------------------ FIGURES -----------------------------------
with tab_figs:
    fig_mode = st.radio("Mode", ["Search figures in my corpus",
                                 "🎨 Figure Studio (draw schematics, workflows, data charts)"],
                        horizontal=True, key="figs_mode")
    if fig_mode.startswith("🎨"):
        render_figure_studio()
    else:
        st.markdown("Search the **figures in your corpus** by what they show - "
                    "captions are matched semantically and the figure images "
                    "displayed.")
        if not index_ok:
            st.info("Build the index first.")
        else:
            f1, f2 = st.columns([3, 1])
            with f1:
                fq = st.text_input("What should the figure show?",
                                   placeholder="e.g. JV curves under thermal "
                                               "cycling")
            with f2:
                fk = st.slider("Results", 4, 24, 8, key="figs_k")
            if st.button("Search figures", type="primary") and fq.strip():
                st.session_state["fig_hits"] = retrieve(
                    fq.strip(), fk, where_extra={"type": "figure"})
            fig_hits = st.session_state.get("fig_hits") or []
            if fig_hits:
                fcols = st.columns(2)
                for fi, h in enumerate(fig_hits):
                    with fcols[fi % 2]:
                        m = h["meta"]
                        img = m.get("image_path", "")
                        if img and Path(img).exists():
                            st.image(img, use_container_width=True)
                        st.markdown(f"**{m['title']}** - `{m['file']}`, "
                                    f"p.{m['page_start']} "
                                    f"(similarity {h['score']:.2f})")
                        st.caption(h["text"][:300])
                        st.markdown("---")

# ------------------------------ LIBRARY -----------------------------------
with tab_library:
    if not index_ok:
        st.info("Build the index first.")
    else:
        rows = library_table()
        journals = load_journals()

        # Enrich each row with journal family, topic and year.
        for r in rows:
            r["Journal"] = journals.get(r["sig"], "—")
            topic, year = filename_meta(r["File"])
            r["Topic"] = topic
            r["Year"] = year

        st.markdown(f"**{len(rows)} papers indexed.** Search, filter, then "
                    "select one to summarise it or ask it questions.")

        with st.expander("🆚 Compare papers side-by-side"):
            import json as _json
            pick_opts = {f"{r['Title'][:75]} · {Path(r['File']).name[:35]}": r
                         for r in rows}
            cmp_pick = st.multiselect("Papers (2-10)", sorted(pick_opts),
                                      key="cmp_pick", max_selections=10)
            cmp_fields = st.text_input(
                "Fields to compare (comma-separated)",
                value="Cell architecture, Absorber composition, Deposition "
                      "method, Best PCE (%), Stability result, Key claim",
                key="cmp_fields")
            if st.button("🆚 Build comparison table", type="primary",
                         key="cmp_go", disabled=len(cmp_pick) < 2):
                if not api_key.strip():
                    st.error("Needs the API key / Max mode (sidebar).")
                else:
                    fields = [f.strip() for f in cmp_fields.split(",")
                              if f.strip()][:10]
                    results = []
                    bar = st.progress(0.0)
                    for i, lbl in enumerate(cmp_pick, 1):
                        r = pick_opts[lbl]
                        bar.progress(i / len(cmp_pick),
                                     text=r["Title"][:60])
                        txt = paper_full_text(r["sig"], 18000)
                        umsg = ("FIELDS: " + " | ".join(fields)
                                + f"\n\nPAPER: {r['Title']}\n\n{txt}")
                        try:
                            raw = call_claude(api_key.strip(),
                                              MATRIX_SYSTEM, umsg, model,
                                              max_tokens=800)
                            raw = re.sub(r"^```(json)?|```$", "",
                                         raw.strip(), flags=re.M).strip()
                            vals = _json.loads(raw)
                        except Exception:
                            vals = {}
                        row = {"Paper": r["Title"][:70]}
                        for f in fields:
                            row[f] = str(vals.get(f, "-"))[:250]
                        results.append(row)
                    bar.empty()
                    st.session_state["cmp_table"] = (fields, results)
            if st.session_state.get("cmp_table"):
                cfields, cresults = st.session_state["cmp_table"]
                import pandas as _cpd
                st.dataframe(_cpd.DataFrame(cresults),
                             use_container_width=True)
                ccols = ["Paper"] + cfields
                cd1, cd2 = st.columns(2)
                try:
                    cbuf = io.BytesIO()
                    build_table_docx(cresults, ccols,
                                     "Paper comparison").save(cbuf)
                    cd1.download_button(
                        "⬇️ Table as Word", data=cbuf.getvalue(),
                        file_name="paper_comparison.docx",
                        mime="application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document",
                        key="cmp_docx", use_container_width=True)
                except Exception:
                    pass
                cd2.download_button(
                    "⬇️ Table as CSV",
                    data=_cpd.DataFrame(cresults).to_csv(index=False)
                    .encode("utf-8-sig"),
                    file_name="paper_comparison.csv", mime="text/csv",
                    key="cmp_csv", use_container_width=True)

        if not journals:
            with st.container():
                st.info("Journal families aren't detected yet. The scan reads "
                        "each paper's first page from the index (already "
                        "stored) and reads the DOI/journal from it — no API "
                        "cost, one pass, cached afterwards.")
                if st.button("🔍 Detect journal families (one-time scan)"):
                    bar = st.progress(0.0, text="Scanning index…")
                    try:
                        found = scan_journals(
                            lambda f: bar.progress(f, text="Scanning index…"))
                        save_journals(found)
                        bar.empty()
                        st.success(f"Detected journals for {len(found)} "
                                   "papers.")
                        st.rerun()
                    except Exception as e:
                        bar.empty()
                        st.error(f"Scan failed: {e}")

        lf1, lf2, lf3, lf4 = st.columns([3, 2, 2, 2])
        with lf1:
            lsearch = st.text_input("🔎 Search by title or filename",
                                    key="lib_search",
                                    placeholder="e.g. passivation, tandem, "
                                                "slot-die")
        with lf2:
            jopts = sorted({r["Journal"] for r in rows if r["Journal"] != "—"})
            jpick = st.multiselect("Journal family", jopts, key="lib_journal")
        with lf3:
            topts = sorted({r["Topic"] for r in rows if r["Topic"]})
            tpick = st.multiselect("Topic", topts, key="lib_topic")
        with lf4:
            sort_by = st.selectbox("Sort by",
                                   ["File", "Title", "Journal", "Year",
                                    "Passages", "Figures"], key="lib_sort")

        view = rows
        if lsearch.strip():
            s = lsearch.strip().lower()
            view = [r for r in view
                    if s in str(r["Title"]).lower()
                    or s in str(r["File"]).lower()]
        if jpick:
            view = [r for r in view if r["Journal"] in jpick]
        if tpick:
            view = [r for r in view if r["Topic"] in tpick]
        reverse = sort_by in ("Passages", "Figures", "Year")
        view = sorted(view, key=lambda r: (r.get(sort_by) or ""),
                      reverse=reverse)

        if journals:
            from collections import Counter as _Counter
            cnt = _Counter(r["Journal"] for r in view)
            st.caption("  ·  ".join(f"{k}: {v}" for k, v in cnt.most_common()
                                    if k != "—"))
        st.markdown(f"**Showing {len(view)} of {len(rows)} papers.**")
        st.dataframe(
            [{k: r[k] for k in ("Title", "Journal", "Year", "Topic",
                                "File", "Passages", "Figures")}
             for r in view],
            use_container_width=True, height=320)

        if not view:
            # Never st.stop() here - it would halt the tabs rendered after
            # this one. Fall back to the full list instead.
            st.warning("No papers match these filters — the selector below "
                       "still lists everything.")
            view = rows
        options = {f"{r['File']}": r for r in view}
        chosen = st.selectbox("Paper", list(options.keys()), key="lib_pick")
        paper = options[chosen]

        col1, col2 = st.columns(2)
        with col1:
            if st.button("📄 Summarise this paper", use_container_width=True):
                if not api_key.strip():
                    st.error("Needs the API key (sidebar).")
                else:
                    col_obj = load_collection()
                    got = col_obj.get(where={"doc_sig": paper["sig"]},
                                      include=["documents", "metadatas"])
                    pairs = sorted(zip(got["documents"], got["metadatas"]),
                                   key=lambda x: x[1]["page_start"])
                    text = "\n\n".join(d for d, m in pairs)[:60000]
                    user_msg = (f"Full extracted text of one paper "
                                f"('{paper['Title']}'):\n\n{text}\n\n"
                                "Summarise this paper for an expert reader: "
                                "objective, approach, key quantitative results "
                                "with conditions, and main limitation. "
                                "Max 250 words.")
                    with st.spinner("Summarising..."):
                        summary = call_claude(
                            api_key.strip(), build_system_prompt("Standard"),
                            user_msg, model)
                    st.markdown(summary)
        with col2:
            paper_q = st.text_input("Ask this paper only", key="paper_q",
                                    placeholder="e.g. What deposition rate "
                                                "was used?")
            if st.button("Ask", use_container_width=True) and paper_q.strip():
                hits = retrieve(paper_q.strip(), min(top_k, 8),
                                doc_sig=paper["sig"])
                if not api_key.strip():
                    st.info("Search-only mode: showing passages.")
                    show_sources(hits, key_prefix="lp")
                else:
                    user_msg = (f"Excerpts (all from the same paper):\n\n"
                                f"{build_context(hits)}\n\n"
                                f"Question: {paper_q.strip()}")
                    with st.spinner("Answering from this paper..."):
                        ans = call_claude(
                            api_key.strip(), build_system_prompt(answer_mode),
                            user_msg, model)
                    st.markdown(ans)
                    show_sources(hits, key_prefix="lp")

# ---------------------------- GET PAPERS -----------------------------------
with tab_get:
    import subprocess
    import sys as _sys
    import get_papers as gp

    # Optional enhanced downloader (My-Literature bridge). If the files aren't
    # present the tab still works with the built-in OA downloader.
    try:
        import workbench_downloader as wdl
        _HAVE_WDL = True
    except Exception as _wdl_e:  # noqa: BLE001
        _HAVE_WDL = False
        _WDL_ERR = str(_wdl_e)

    st.markdown(
        "Search **OpenAlex** and download the **legal open-access PDF** "
        "where one exists (publisher OA, preprints, repository copies). "
        "Paywalled items are exported as a **DOI list** to collect through "
        "your library access (e.g. Zotero browser connector) or a "
        "publisher TDM API - no credentials are used here.")

    # Contact email for open-access lookups (Unpaywall/Crossref etiquette).
    dl_email = st.text_input(
        "Contact email for open-access lookups",
        value="k.anurag2011@gmail.com",
        help="Sent to Unpaywall/Crossref as an identifier - good etiquette, "
             "and keeps the lookups from being throttled. A university email "
             "is ideal but any working address works.")

    # Visible status so setup problems are obvious rather than silent.
    if _HAVE_WDL:
        st.caption("✅ Enhanced downloader loaded - resilient OA + the 🔒 "
                   "paywalled-fetch box will appear when a search returns "
                   "locked papers.")
    else:
        st.warning(
            "⚠️ Enhanced downloader **not** loaded, so the 🔒 paywalled-fetch "
            "box is hidden and OA downloads use the basic method.\n\n"
            "Fix: make sure `workbench_downloader.py` and the `literature` "
            "folder sit **directly** inside your Paper rag folder (not inside "
            "a `paper-rag-downloader` sub-folder), then restart the app.\n\n"
            f"Technical reason: `{_WDL_ERR}`")

    with st.form("gp_search"):
        gq = st.text_input("Topic / keywords",
                           placeholder="e.g. slot-die coating perovskite "
                                       "module stability")
        g1, g2, g3 = st.columns(3)
        with g1:
            y_from = st.number_input("From year", 1990, 2026, 2020)
        with g2:
            y_to = st.number_input("To year", 1990, 2026, 2026)
        with g3:
            g_n = st.slider("Max results", 5, 100, 25)
        gp_submitted = st.form_submit_button("Search OpenAlex",
                                             type="primary")

    if gp_submitted and gq.strip():
        try:
            with st.spinner("Searching OpenAlex..."):
                st.session_state["gp_results"] = gp.search_openalex(
                    gq.strip(), int(y_from), int(y_to), int(g_n))
        except Exception as e:
            st.error(f"OpenAlex search failed: {e}")

    results_all = st.session_state.get("gp_results") or []

    # Tag each result with its publisher (from the DOI prefix, journal fallback).
    # Degrade gracefully if literature/publishers.py is missing (older bundle).
    try:
        from literature.publishers import publisher_for_record
    except Exception:
        publisher_for_record = None
        st.caption("ℹ️ Publisher classification unavailable - the file "
                   "'literature\\publishers.py' is missing. Copy it from the "
                   "latest downloader bundle to enable it.")
    if publisher_for_record is not None:
        for w in results_all:
            w["publisher"] = publisher_for_record(w)
    else:
        for w in results_all:
            w.setdefault("publisher", "—")

    results = results_all
    if results_all:
        from collections import Counter
        counts = Counter(w["publisher"] for w in results_all)
        breakdown = "  ·  ".join(f"{p}: {c}" for p, c in counts.most_common())
        st.caption(f"Publishers — {breakdown}")
        chosen_pubs = st.multiselect(
            "Filter by publisher (leave empty for all)",
            options=[p for p, _ in counts.most_common()],
            format_func=lambda p: f"{p} ({counts[p]})")
        if chosen_pubs:
            results = [w for w in results_all if w["publisher"] in chosen_pubs]

    if results:
        n_oa = sum(1 for w in results if w["pdf_url"])
        st.markdown(f"**{len(results)} works shown - "
                    f"{n_oa} with a downloadable OA PDF.**")
        st.dataframe(
            [{"OA": "✅" if w["pdf_url"] else ("🔓" if w["is_oa"] else "🔒"),
              "Publisher": w.get("publisher", "—"),
              "Title": w["title"], "Year": w["year"],
              "Journal": w["journal"], "Citations": w["citations"],
              "DOI": w["doi"]} for w in results],
            use_container_width=True, height=340)

        d1, d2, d3 = st.columns(3)
        with d1:
            if st.button("⬇️ Download all OA PDFs",
                         use_container_width=True, disabled=n_oa == 0):
                bar = st.progress(0.0)
                def _cb(i, total, title):
                    bar.progress(i / total, text=f"{i}/{total}: {title[:60]}")
                if _HAVE_WDL:
                    # Enhanced: retries dead OA links via Unpaywall/arXiv/PMC
                    # and verifies real PDFs.
                    n_ok, n_fail, msgs = wdl.download_oa_resolved(
                        results, config.PDF_DIR, dl_email.strip(), _cb)
                    where = f"{config.PDF_DIR}/{wdl.OA_SUBDIR}"
                else:
                    n_ok, n_fail, msgs = gp.download_oa_pdfs(
                        results, config.PDF_DIR, _cb)
                    where = f"{config.PDF_DIR}/{gp.DOWNLOAD_SUBDIR}"
                bar.empty()
                st.success(f"Downloaded {n_ok} PDF(s) into '{where}'. "
                           f"{n_fail} had no reachable OA copy.")
                with st.expander("Download log"):
                    st.text("\n".join(msgs))
        with d2:
            st.download_button(
                "📋 DOI list of paywalled items (.md)",
                data=gp.doi_list_markdown(results, only_non_oa=True),
                file_name="doi_list_paywalled.md", mime="text/markdown",
                use_container_width=True)
        with d3:
            if st.button("🔄 Re-index new papers", use_container_width=True):
                with st.spinner("Running ingest.py (only new PDFs are "
                                "processed)..."):
                    proc = subprocess.run(
                        [_sys.executable, "ingest.py"],
                        capture_output=True, text=True, cwd=".")
                tail = "\n".join((proc.stdout or "").splitlines()[-12:])
                if proc.returncode == 0:
                    st.success("Indexing finished.")
                    st.cache_data.clear()      # refresh Library catalogue
                else:
                    st.error("Indexing reported an error.")
                    tail += "\n" + (proc.stderr or "")[-800:]
                st.text(tail)

        # --- Paywalled items via institutional browser session ---------------
        if _HAVE_WDL and any(not w["pdf_url"] for w in results):
            n_locked = sum(1 for w in results if not w["pdf_url"])
            with st.expander(
                    f"🔒 Fetch {n_locked} paywalled item(s) via institutional access"):
                st.caption(
                    "Uses a browser session you log in to yourself (your "
                    "university SSO + MFA). The tool never sees your password. "
                    "For papers you have institutional access to; conservatively "
                    "rate-limited and meant for small batches, not bulk collection.")
                p1, p2 = st.columns(2)
                with p1:
                    if st.button("① Set up / refresh login",
                                 use_container_width=True):
                        try:
                            wdl.setup_login(dl_email.strip())
                            st.success("Session saved. You can download below.")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Could not open the login browser.\n\n{e}")
                with p2:
                    if st.button("② Download paywalled PDFs",
                                 use_container_width=True):
                        bar = st.progress(0.0)
                        def _cbp(i, total, title):
                            bar.progress(i / total,
                                         text=f"{i}/{total}: {title[:60]}")
                        try:
                            n_ok, n_fail, msgs = wdl.download_paywalled_via_session(
                                results, config.PDF_DIR, dl_email.strip(), _cbp)
                            bar.empty()
                            st.success(f"Fetched {n_ok} PDF(s) into "
                                       f"'{config.PDF_DIR}/{wdl.AUTH_SUBDIR}'. "
                                       f"{n_fail} not reachable (likely no access). "
                                       "Click 'Re-index new papers' above to add them.")
                            with st.expander("Fetch log"):
                                st.text("\n".join(msgs))
                        except Exception as e:  # noqa: BLE001
                            bar.empty()
                            st.error(f"Download stopped.\n\n{e}")
        elif not _HAVE_WDL:
            st.caption("💡 Tip: add the My-Literature downloader files "
                       "(literature/ folder + workbench_downloader.py) to enable "
                       "resilient OA downloads and institutional paywall fetching.")

    st.markdown("---")
    st.markdown("#### Zotero library")
    import zotero_link as zl
    zdir = st.text_input(
        "Zotero data folder (leave empty to auto-detect)",
        value=config.ZOTERO_DIR,
        placeholder=r"auto-detect, e.g. C:\\Users\\krishn28\\Zotero")
    storage = zl.find_zotero_storage(zdir.strip() or None)
    if storage:
        pdfs = zl.list_zotero_pdfs(storage)
        st.success(f"Zotero storage found: {storage} - {len(pdfs)} PDF(s)")
        z1, z2 = st.columns(2)
        with z1:
            if st.button("🔗 Sync Zotero PDFs into corpus",
                         use_container_width=True):
                n_new, n_skip, n_fail = zl.mirror_into_corpus(
                    storage, config.PDF_DIR)
                st.success(f"{n_new} new PDF(s) linked into "
                           f"'{config.PDF_DIR}/zotero', {n_skip} already "
                           f"present, {n_fail} failed.")
                if n_new:
                    st.info("Now click 'Re-index new papers' above.")
        with z2:
            st.caption("Save papers with the Zotero Connector, click sync, "
                       "then re-index. Hard links are used where possible, "
                       "so no disk space is duplicated.")
    else:
        st.info("Zotero storage not found. Open Zotero → Edit → Settings → "
                "Advanced → Files and Folders to see the data directory, "
                "and paste it above.")

        st.caption("Legality note: OA downloads above are from locations "
                   "OpenAlex marks as legally open. For the 🔒 items, use "
                   "the DOI list with your library access one-by-one, or "
                   "ask the UHasselt library for a publisher TDM API key "
                   "for systematic collection.")

# ------------------------------ WATCH -------------------------------------
with tab_watch:
    st.markdown("**Literature watch** - standing topic searches against "
                "OpenAlex. Each check reports only papers you haven't seen "
                "before: open-access PDFs download and join the corpus in "
                "one click, paywalled ones export as a DOI list for your "
                "library access.")

    W_TOPICS = Path("watch_topics.txt")
    W_SEEN = Path("watch_seen.txt")

    def _watch_lines(p):
        if not p.exists():
            return []
        return [ln.strip() for ln in
                p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    w_topics = _watch_lines(W_TOPICS)
    wt1, wt2 = st.columns([3, 1])
    with wt1:
        w_new = st.text_input("Add a topic", key="watch_new",
                              label_visibility="collapsed",
                              placeholder="e.g. perovskite tandem "
                                          "stability encapsulation")
    with wt2:
        if (st.button("Add topic", key="watch_add",
                      use_container_width=True) and w_new.strip()
                and w_new.strip() not in w_topics):
            w_topics.append(w_new.strip())
            W_TOPICS.write_text("\n".join(w_topics) + "\n",
                                encoding="utf-8")
            st.rerun()
    if w_topics:
        w_del = st.multiselect("Topics (select to remove)", w_topics,
                               key="watch_del")
        if w_del and st.button("Remove selected topics",
                               key="watch_del_btn"):
            w_topics = [t for t in w_topics if t not in w_del]
            W_TOPICS.write_text("\n".join(w_topics) + "\n",
                                encoding="utf-8")
            st.rerun()
    else:
        st.info("No topics yet - add your standing searches above. "
                "One line = one OpenAlex query.")

    if w_topics and st.button("📬 Check for new papers", type="primary",
                              key="watch_go"):
        w_seen = set(_watch_lines(W_SEEN))
        w_year = datetime.date.today().year
        w_found = []
        bar = st.progress(0.0)
        for ti, t in enumerate(w_topics, 1):
            bar.progress(ti / len(w_topics), text=t[:60])
            try:
                res = gp.search_openalex(t, w_year - 1, w_year, 30)
            except Exception as e:
                st.warning(f"'{t}' search failed: {e}")
                continue
            for w in res:
                wkey = (w.get("doi") or w.get("title") or "").strip().lower()
                if wkey and wkey not in w_seen:
                    w_seen.add(wkey)
                    w_found.append((t, w))
        bar.empty()
        st.session_state["watch_found"] = w_found
        st.session_state["watch_seen_pending"] = sorted(w_seen)

    w_found = st.session_state.get("watch_found")
    if w_found is not None:
        if not w_found:
            st.success("Nothing new since the last check. ✅")
        else:
            st.markdown(f"**{len(w_found)} new paper(s):**")
            w_cur = None
            for t, w in w_found:
                if t != w_cur:
                    st.markdown(f"#### {t}")
                    w_cur = t
                oa = ("🟢 OA PDF" if w.get("pdf_url")
                      else ("🟡 OA page" if w.get("is_oa")
                            else "🔒 paywalled"))
                doi = (w.get("doi") or "").replace("https://doi.org/", "")
                link = (f" · [doi](https://doi.org/{doi})" if doi else "")
                st.markdown(f"- {oa} **{w.get('title', '?')}** - "
                            f"{w.get('journal', '?')}, "
                            f"{w.get('year', '?')}{link}")
            render_watch_impact(w_found)
            w_oa = [w for _, w in w_found if w.get("pdf_url")]
            w_pay = [w for _, w in w_found if not w.get("pdf_url")]
            wa1, wa2, wa3 = st.columns(3)
            with wa1:
                if w_oa and st.button(f"⬇️ Download {len(w_oa)} OA PDF(s)",
                                      type="primary", key="watch_dl"):
                    bar = st.progress(0.0, text="Downloading...")

                    def _wcb(i, total, title):
                        bar.progress(i / max(total, 1), text=title[:60])
                    try:
                        if _HAVE_WDL:
                            n_ok, _, _ = wdl.download_oa_resolved(
                                w_oa, str(config.PDF_DIR),
                                dl_email.strip(), _wcb)
                        else:
                            n_ok, _, _ = gp.download_oa_pdfs(
                                w_oa, config.PDF_DIR, _wcb)
                    except Exception as e:
                        n_ok = 0
                        st.error(f"Download failed: {e}")
                    bar.empty()
                    if n_ok:
                        st.success(f"Downloaded {n_ok} PDF(s) into "
                                   f"{config.PDF_DIR}. Index them with the "
                                   "button in Get Papers (or `python "
                                   "ingest.py`) so they join the corpus.")
            with wa2:
                if w_pay:
                    st.download_button(
                        f"⬇️ DOI list ({len(w_pay)} paywalled)",
                        data="\n".join(
                            (w.get("doi") or w.get("title", ""))
                            for w in w_pay).encode("utf-8"),
                        file_name="watch_paywalled_dois.txt",
                        mime="text/plain", key="watch_pay",
                        use_container_width=True)
            with wa3:
                if st.button("✅ Mark all as seen", key="watch_seen_btn",
                             use_container_width=True,
                             help="The next check reports only papers newer "
                                  "than this point. Also writes a digest "
                                  "to the answers folder."):
                    W_SEEN.write_text(
                        "\n".join(st.session_state.get(
                            "watch_seen_pending", [])), encoding="utf-8")
                    stamp = datetime.date.today().isoformat()
                    lines = [f"# Literature watch - {stamp}", ""]
                    for t, w in w_found:
                        lines.append(
                            f"- [{t}] {w.get('title', '?')} - "
                            f"{w.get('journal', '?')}, "
                            f"{w.get('year', '?')}. "
                            f"DOI: {w.get('doi', 'n/a')}")
                    (ANSWERS_DIR / f"watch_{stamp}.md").write_text(
                        "\n".join(lines), encoding="utf-8")
                    st.session_state.pop("watch_found", None)
                    st.rerun()

# ------------------------------ PROJECTS ----------------------------------
with tab_proj:
    st.markdown("**Project workspaces** - one persistent folder per "
                "manuscript or proposal: its files, its notes, its saved "
                "outputs. The active project's files appear as pickers in "
                "the Manuscript and Proposal tabs, so nothing needs "
                "re-uploading after a restart. Stored in "
                "answers/projects/ (backed up nightly).")
    projs = list_projects()
    ap = active_project()
    pc1, pc2, pc3 = st.columns([2, 2, 1])
    with pc1:
        new_proj = st.text_input("New project", key="proj_new",
                                 placeholder="e.g. EIC-Transition-2026")
        if st.button("➕ Create", key="proj_create",
                     disabled=not new_proj.strip()):
            safe = re.sub(r"[^A-Za-z0-9 _-]", "", new_proj.strip())[:60]
            (PROJECTS_DIR / safe).mkdir(parents=True, exist_ok=True)
            set_active_project(safe)
            st.rerun()
    with pc2:
        if projs:
            sel = st.selectbox("Active project", projs,
                               index=projs.index(ap) if ap in projs
                               else 0, key="proj_sel")
            if sel != ap and st.button("Set active", key="proj_setact"):
                set_active_project(sel)
                st.rerun()
    with pc3:
        if ap and st.button("🗑️ Delete active", key="proj_del",
                            help="Deletes the project folder and "
                                 "everything in it."):
            import shutil as _sh
            _sh.rmtree(PROJECTS_DIR / ap, ignore_errors=True)
            set_active_project("")
            st.rerun()

    if not ap:
        st.info("Create or select a project - it then follows you "
                "across restarts.")
    else:
        st.markdown(f"### 🗂️ {ap}")
        pdir = PROJECTS_DIR / ap

        pu = st.file_uploader(
            "Add files to this project (docx/pdf/txt/md)",
            type=["docx", "pdf", "txt", "md"],
            accept_multiple_files=True, key="proj_up")
        if pu and st.button(f"💾 Store {len(pu)} file(s)",
                            key="proj_store"):
            for f in pu:
                (pdir / f.name).write_bytes(f.getvalue())
            st.success(f"Stored {len(pu)} file(s).")
            st.rerun()

        pfiles = project_files(ap)
        if pfiles:
            st.markdown("**Files**")
            for p in pfiles:
                fc1, fc2, fc3 = st.columns([5, 1, 1])
                fc1.caption(f"{p.name}  ·  {p.stat().st_size // 1024} KB "
                            f"·  {datetime.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d}")
                fc2.download_button("⬇️", data=p.read_bytes(),
                                    file_name=p.name,
                                    key=f"pf_dl_{p.name}")
                if fc3.button("🗑️", key=f"pf_rm_{p.name}"):
                    p.unlink(missing_ok=True)
                    st.rerun()
        else:
            st.caption("No files yet - add drafts, call texts, referee "
                       "reports, ESRs...")

        st.markdown("**Attach recent outputs** - copy saved answers/"
                    "reports into this project so everything lives "
                    "together.")
        recent = sorted((f for f in ANSWERS_DIR.iterdir()
                         if f.is_file() and f.suffix in
                         (".docx", ".md") and not
                         f.name.startswith("spend")),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True)[:20]
        if recent:
            att = st.multiselect("Recent outputs",
                                 [f.name for f in recent],
                                 key="proj_att")
            if att and st.button("📎 Attach selected", key="proj_att_go"):
                for nm in att:
                    src_f = ANSWERS_DIR / nm
                    (pdir / nm).write_bytes(src_f.read_bytes())
                st.success(f"Attached {len(att)} file(s).")
                st.rerun()

        st.markdown("**Notes**")
        notes_p = pdir / "notes.md"
        notes = st.text_area(
            "Project notes (deadlines, decisions, todo)", height=160,
            value=(notes_p.read_text(encoding="utf-8")
                   if notes_p.exists() else ""),
            key=f"proj_notes_{ap}", label_visibility="collapsed")
        if st.button("💾 Save notes", key="proj_notes_save"):
            notes_p.write_text(notes, encoding="utf-8")
            st.success("Notes saved.")

# ------------------------------ CAREER ------------------------------------
with tab_career:
    render_career_tab()
