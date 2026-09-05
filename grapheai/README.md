# GrapheAI - personal research platform (Streamlit apps)

Thirteen instruments for a perovskite-PV research group leader: literature
workbench (ask, draft, manuscript rewrite with library grounding, response to
reviewers, claim checker, figure engine, career documents), PV Radar,
Material Library, PeroDeg analytics, Analytics, Funding Radar, Slide Studio,
TechnoEcon, Experiment Planner, Impact Tracker, Venture Studio, Hub.

See `SYSTEM_GUIDE.md` for the map of instruments, data folders and
troubleshooting. `golden_runs.py` runs the deterministic self-checks of the
Workbench machinery without any model call.

The apps expect the `PaperRag` layout (config.py, ingest.py, ask_rules.py,
papers/, chroma_db/, answers/) next to them; those local files and the
personal library are not part of this repository.
