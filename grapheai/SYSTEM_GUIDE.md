# GrapheAI Research Platform — Operations Guide

*developed by Dr. Anurag Krishna · last updated 2026-08-31*

Everything lives in **`~/PaperRag`** on the MacBook Neo.
Thirteen instruments, three automatic jobs, one shared data pool.

---

## The instruments

| Launcher (double-click) | App | Port | What it is |
|---|---|---|---|
| `Hub.command` | 🏠 GrapheAI Hub | 8500 | Front door: status, start buttons, deadlines, unified search |
| `Workbench.command` | ✍️ Workbench | 8501 | Literature → manuscripts → proposals |
| `PVRadar.command` | 📡 PV Radar | 8502 | News, patents & market intelligence |
| `MaterialLibrary.command` | 🧱 Material Library | 8503 | Materials extracted from the corpus |
| `PeroDeg.command` | 🔋 PeroDeg | 8504 | Degradation & outdoor-data analytics |
| `Analytics.command` | 📊 Analytics | 8505 | Any-schema data extraction + plots |
| `FundingRadar.command` | 💶 Funding Radar | 8506 | EU calls, deadline board & fit memos |
| `SlideStudio.command` | 🎤 Slide Studio | 8507 | PowerPoint decks from any output |
| `TechnoEcon.command` | 📉 TechnoEcon | 8508 | LCOE / cost modelling per product line |
| `ExpPlanner.command` | 🧪 Experiment Planner | 8509 | DoE + Bayesian next-experiment picks |
| `ImpactTracker.command` | 📈 Impact Tracker | 8510 | Citations, h-index & track record (OpenAlex) |
| `VentureStudio.command` | 🚀 Venture Studio | 8511 | Business case, IP/FTO prep & roadmap (TRANSPIRE) |
| `IndexPapers.command` | 📚 Indexer | — | Ingest new PDFs/docx into the corpus |

All run side by side. Stop any app by closing its Terminal window.
Model defaults: **Frontier (Fable 5.1)** on judgment tasks (thinking
always on; depth set by the sidebar **Reasoning effort**, default xhigh),
Balanced = Sonnet 5, Deep = Opus 5, Fast = Haiku 4.5 for bulk extraction —
every surface has a picker. Long outputs stream; refusal fallbacks are
enabled automatically where the installed SDK supports them.

### ✍️ Workbench tabs
Ask (deep answer + re-rank + reference export) · Draft (passages and
long drafts — and **📚 Review / Perspective writer**: synopsis → corpus
sweep → literature map with gaps → outline (approve it) → critical
synthesis citing only retrieved papers → reference list from your
library → citation/number audit) · Manuscript
(response to reviewers, cover letter, abstract) · Proposal (mock ESR,
call compliance, SOTA & beyond, work plan, weakness-fix loop, revisions)
· Review (referee report, line edits, structure, polish, rebuttal —
and **🧬 Rewrite for a high-impact journal**: manuscript + SI + files →
evidence ledger → editorial plan (approve it) → section rewrite → front
matter → mechanical audit + **PV reporting checklist** + adversarial
referee → Word bundle; never changes a number, marks gaps as
[AUTHOR: ...]; upload CSV/XLSX data tables and the figure engine plots
them column by column) and **📨 Respond to reviewers** (staged: reviews →
numbered points you approve → grounded point-by-point responses → exact
text changes applied only when they match the manuscript and add no
unsourced number → before/after diff → Word bundle with letter, marked
changes and clean manuscript) · Claim checker · Extract
· Figures (corpus figure search — and **🎨 Figure Studio**: sandbox-rendered
schematics/workflows/roadmaps/data charts, 300-dpi PNG + SVG, visual QA)
· Library (comparison matrix, journal scan) · Get Papers (OA +
EZproxy) · Watch (new-paper alerts) · Projects (persistent
per-manuscript/proposal workspaces) · **🎓 Career** (job applications:
advert → requirements, fit matrix, research vision/proposal grounded
in a topic library with citations, cover letter, tailored CV,
interview prep, checklist — every document written only from your
saved CV/profile/facts; unverified numbers flagged)

### 📡 PV Radar tabs
Dashboard (alerts, metrics, charts) · Fetch & Watchlists (searches with
freshness control, RSS feeds, **patent watch** on WIPO Patentscope,
keyword alerts) · Trends · Records · Map · Companies · Reports (digest,
company briefing, detailed report, **investor templates**) · Benchmark
(your startup vs competition per product segment) · Archive

### 🧱 Material Library tabs
Build (incremental corpus extraction) · Browse & export (dossiers per
material) · Insights (role deep-dive, adoption timeline, stack pairing)
· Design assistant (cited candidates + white space)

### 🔋 PeroDeg tabs
Data (universal CSV importer with saved profiles) · I-V curves ·
Long-term (bilinear PLR with bootstrap 95% CIs, T80, PR) · Diurnal (DPD/DPR per the ACS Energy
Lett. 2024 methodology) · Reversibility (irreversible vs reversible +
dark-storage recovery) · Compare · Arrhenius (Ea, acceleration factors)
· Energy yield (measured + projected) · Imaging (PL/EL/DLIT) · Forecast
(XGBoost) · Report (corpus-grounded analysis + ISOS summary table)

### 📝 Track Changes, references, submission packet, watch impact
- **Word Track Changes**: the rewrite results offer "Tracked changes vs the
  original" and the response mode offers "Revised manuscript with Word Track
  Changes" - real revisions (insertions/deletions) co-authors accept or reject
  in Word's Review pane. Reordered material appears as deletion + insertion.
- **Resolved references**: in the rewrite's 📚 Library tab and the review
  writer's References tab, export the cited corpus papers as BibTeX / RIS, or
  fetch formal entries via DOI (Crossref) for Zotero or the reference list.
- **Submission packet** (rewrite results): a limits table against the
  journal (main-text words, abstract words, display items, references,
  title length - approximate, verify) and a one-call packet: cover letter,
  editor summary, why this journal, reviewer profiles (names as [AUTHOR]),
  exclusions, CRediT contributions, statements, pre-submission checklist.
- **Watch impact**: after "Check for new papers" in the Watch tab, "Check the
  new papers against my open documents" tells you which manuscript, review,
  proposal or response in progress a new paper supports, competes with or
  contradicts, and what to do about it.
- **PeroDeg / Analytics bridge**: the rewrite panel lists CSV/XLSX exports
  saved under `answers/perodeg_data`, `answers/analytics` and
  `answers/figures_out` as data tables for file-backed figures.
- **Venture Studio → Roadmap tab**: "Grant-to-startup roadmap" turns TRL,
  IP, team and cash facts into a six-lane roadmap with decision gates and
  adds dated milestones to the Gantt.
- **Golden runs**: double-click `GoldenRuns.command` (or `python
  golden_runs.py`) after every update or model change - nine deterministic
  checks of the Workbench machinery with no model call; exit code 1 on
  failure.

### 📚 Library grounding of the rewrite
"Use my library" in the rewrite panel has four levels: Off · Positioning
only (the plan sees passages and suggests references as [AUTHOR: consider
citing]) · **Cite my library in Introduction & Discussion** (default) ·
Cite and compare values (Results sections and data figures may also use
library values). Passages are retrieved per section from your index
(optionally restricted by folder, journal, topic, year or specific papers)
and numbered [L1], [L2], ...; the writer may cite them only for
positioning, precedent and comparison, never to support the manuscript's
own results. Every [Ln] is checked against what was actually retrieved
(unknown markers are CRITICAL), literature values quoted from passages are
flagged MINOR for verification, the referee sees the same passages and
must quote any library paper that contradicts a novelty claim, and the
manuscript ends with "Library references (to merge into the reference
list)". The results view has a 📚 Library tab with the passages each
section received.

### 💬 Follow-up conversation on every outcome
Under each result (rewritten manuscript, review article, career document,
response letter, mock ESR, consistency audit) a chat box lets you continue:
ask why, say what is wrong, add new facts or results, or ask for a
revision of a sentence, a section or the whole approach. The editor
answers and returns exact edits, which are applied only when the text
matches once and every new number exists in the sources or in your own
messages; the document, its audit and the Word bundle are rebuilt without
extra model calls. For the mock ESR the edits go to a working copy of the
proposal (download as Word). Conversations persist per job under
`answers/followups/`.

### 🏆 Proposal tab (grant proposals)
Mock evaluation is now **ESR-calibrated**: it reads your past Evaluation
Summary Reports from `papers/evaluations/` into a calibration digest
(recurring weaknesses, scoring habits, threshold language; cached under
`answers/proposal_eval/`), scores each criterion against its threshold in
a table, flags repeat offences with quotes from the old ESRs, and lists
weakness → fix. **Consistency audit** extracts objectives, KPIs, work
packages, deliverables, milestones, risks, effort and budget into a model
and checks it mechanically (orphan objectives, WPs without deliverables or
milestones, dates outside the project, effort mismatch, risks without
mitigation) with an objective × WP coverage matrix. **Gantt & WP
figures** draws the Gantt chart, the WP structure diagram and the effort
chart deterministically in the style card (PNG/SVG/PDF) from that model or
from tables you fill in, plus the WP/deliverable/milestone/effort tables
as Word.

### 🎨 Automatic figures (rewrite, review/perspective, career documents)
Each pipeline pauses after **planning** the figures (untick, instruct or
re-plan before any drawing cost) and, once written, lets you **redraw any
figure with feedback** from the results view; the caption and the
document update in place. Rewrite jobs may include CSV/XLSX tables: the
engine plots measured sweeps, time series and device statistics straight
from the file (source `FILE:<name>`), with every plotted number checked
against the table. Outputs are PNG + SVG + **PDF**; the same entity keeps
the same colour across all figures of a document (colour ledger). On the
Max backend the visual critic works through Claude Code's Read tool once a
one-time probe has confirmed that images reach the model.

Each writing pipeline plans the figures its text calls for (schematic,
mechanism, workflow, architecture, roadmap, taxonomy, comparison,
structure–property, process, graphical summary) and draws them in three
steps modelled on a journal art department: an **art editor** briefs each
figure (one claim, panels, spine/satellites, semantic colour roles, arrow
meanings, text budget, what it must not imply, epistemic status of every
mechanism); an **illustrator** composes it on a grid and writes matplotlib
code for a locked-down sandbox (vetted numpy/matplotlib namespaces only,
no files or network, timeout, exact journal column width, 7–8 pt type,
Okabe-Ito colours: perovskite brick, ETL sky, HTL orange, defect vermilion,
accent blue for the one new thing); an **art-editor critic** reviews the
rendered image (API backend). Panel-label form, caption lead and column
widths follow the target journal (Nature a/b, Science/Cell A/B, RSC/ACS
(a)/(b), Wiley a)/b)).

Integrity is mechanical, not a promise: every data figure declares its
values in a `DATA` block that is checked against the sources (exact match,
integers included, source ID per series, one condition per value); after
rendering, the harness harvests every number that reached the canvas
(points, bars, labels) and rejects anything not in DATA (no fits, trend
lines, means or guide curves), and rejects quantitative axes or unsourced
numbers on conceptual figures. A layout lint (overlapping or clipped text,
tiny type, labels on data marks) triggers repairs before any visual review.
INDIRECT/SPECULATIVE mechanisms get dashed "proposed" arrows and a hedged
caption; stacks and ladders get "not to scale". A figure that cannot be
rendered honestly becomes a **loud placeholder** image with an [AUTHOR: ...]
request instead of a silent gap. Outputs: 300-dpi PNG + SVG (with source
metadata) + code + a provenance sidecar JSON in `answers/figures_gen/<job>/`,
embedded at true print width in the Word export and listed with sources
and QA status in the bundle. Untick "Draw the figures" in a panel to skip.

### 📊 Analytics tabs
Schemas (define any fields; presets: PV device metrics, stability,
proposal metadata) · Extract (papers and/or proposals) · Plot studio
(scatter/box/bar/histogram, publication figures, trends narration,
group stats with Mann-Whitney) · QC (physics sanity rules + Claude
verification sample = a measured error bar on every dataset)

### 💶 Funding Radar tabs
Board (deadline countdown, statuses) · Find calls (EU Funding &
Tenders portal watch queries + manual add for FWO/NWO/national) · Fit
memo (score, angle, role, gaps, dated actions - judged against your
profile) · Profile

### 🎤 Slide Studio flow
Source (any answers/ output, upload, or paste) → Figures (pick from
figures_out/) → Build (audience, Investor/Conference template, slide
count) → .pptx with speaker notes in `answers/decks/`. Needs
`pip install python-pptx` once.

### 📉 TechnoEcon tabs
Scenarios (editable product lines vs c-Si baseline: EUR/Wp, LCOE) ·
Sensitivity (tornado) · Parity map (efficiency x degradation vs c-Si)
· Report (Word + optional narrative). PeroDeg degradation rates plug
straight into the degradation input.

### 🏠 Hub
Start it first: shows every instrument's running state with ▶ Start
buttons, corpus/spend/backup/nightly-job health (flags launchd
failures), the next funding deadlines, latest radar signals, and
full-text search across everything in `answers/`. Makes no AI calls.

### 📈 Impact Tracker tabs
Overview (citations/works per year, h-index) · Papers (sortable, CSV)
· Who cites you (institutions + authors over your top papers) · Track
record (application-ready paragraph from live numbers) · Setup (link
your OpenAlex author once, then Refresh). Free OpenAlex API, no key.

### 🚀 Venture Studio tabs
Brief (project facts, single source of truth) · Business case
(EIC-style, grounded in TechnoEcon numbers + PV Radar signals; gaps
become [TO CONFIRM] placeholders) · IP & FTO prep (feature list,
candidate patents with pasted claims, IP-strategy draft for the TTO
and a preliminary screening pack **for the patent attorney** - not
legal advice) · Roadmap (milestones + Gantt).

### 🧪 Experiment Planner tabs
Campaign (parameter space + objective) · Log (runs, CSV import) ·
Suggest (Latin hypercube → Gaussian-process Bayesian at ≥5 results;
noise pinned by replicate runs when you have them) ·
Progress · Interpret. Bayesian mode needs `pip install scikit-learn`.

---

## Automatic jobs (launchd)

| Job | When | Does |
|---|---|---|
| `com.grapheai.pvradar` | daily 08:00 (or next wake) | All watchlists + PV feeds headlessly (past-week freshness), Haiku via Max, macOS notification on alert matches. Log: `answers/radar_refresh.log` |
| `com.grapheai.backup` | daily 20:00 | Zips `answers/` + configs + app files to iCloud/PaperRagBackups, keeps 14 |

`launchctl list | grep grapheai` — check · `launchctl start <label>` — run now.

---

## Claude access
**Max mode** (default): via Claude Code login; if "Not logged in" →
`claude` → `/login` → subscription option. **API key**: overflow +
batch. Spend + budget shared across all apps (`answers/spend.json`).
Fable 5.1 = deepest reasoning (use effort 'max' for the hardest judgments); drop to Sonnet 5 when the Max window runs low.

**Keeping Max mode current.** The Python package `claude-agent-sdk` ships
its own copy of Claude Code and uses it by default, so a new model can be
refused ("Claude Code 2.1.233 does not support this model; version 2.1.251
or newer is required") even after `claude update`. Fix:
`/opt/miniconda3/bin/pip install -U claude-agent-sdk`, then restart the
app. Every app now runs the newest Claude Code it can find (bundled or
installed) and shows its version under the backend switch in the sidebar,
with a warning when it is older than Fable 5.1 needs (2.1.251).

---

## Data map (what to protect)

| Path | Contents | Rebuildable? |
|---|---|---|
| `papers/` (+`proposals/`, `evaluations/`) | source documents | copy exists on Windows laptop |
| `chroma_db/` | bge-m3 index | overnight `ingest.py --reset` |
| `answers/` | ALL outputs + every store: rewrite_jobs/, review_jobs/, career/, pv_radar, watchlists, benchmark, materials, analytics/, perodeg_data/, projects/, figures_out/, figures_gen/, funding/, decks/, technoecon/, experiments/, impact/, venture/, spend, journals | **No — nightly backup protects this** |
| `.streamlit/`, `*.command` | theme + launchers | trivial |

**Add papers:** drop into `papers/` → double-click `IndexPapers.command`.
Proposals → `papers/proposals/`; ESRs → `papers/evaluations/`.
Filename pattern `1001_topic_2025_Title.pdf` enables Topic/Year filters.

---

## Troubleshooting quick hits

| Symptom | Fix |
|---|---|
| "Not logged in" in Max mode | `claude` → `/login` → subscription → `/exit` |
| "Claude Code x.y.z does not support this model" | `/opt/miniconda3/bin/pip install -U claude-agent-sdk`, restart the app (the SDK bundles its own Claude Code) |
| Max usage-limit message | Sidebar → API key, or wait for the window |
| Import button greyed out (PeroDeg/Analytics) | A required name/mapping field is empty |
| Patent check fails | Patentscope RSS hiccup — tell Claude, provider can switch |
| Port already in use | An old Terminal window still runs that app |
| Extraction stopped early | Max window hit — progress is saved, just rerun |
| Anything else | Paste the exact error to Claude |

## File inventory

`workbench.py` `pvradar.py` `materials.py` `perodeg.py` `analytics.py`
`fundradar.py` `slides.py` `technoecon.py` `expplan.py` `hub.py`
`impact.py` `venture.py` — the apps ·
`radar_refresh.py` `analytics_refresh.py` `backup.py` — the jobs · `ingest.py`
`get_papers.py` `workbench_downloader.py` + `literature/` — the corpus
pipeline · `ask_rules.py` `config.py` `zotero_link.py` — support
