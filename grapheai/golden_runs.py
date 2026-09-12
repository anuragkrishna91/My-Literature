#!/usr/bin/env python3
"""GrapheAI golden runs - deterministic checks of the Workbench machinery
without any model call. Run after every update (or model change) from the
PaperRag folder:   python golden_runs.py

Loads the functions straight out of workbench.py, feeds them fixed inputs
and checks fixed expectations: sandbox gate, canvas/data audits, figure
stage with checkpoint and placeholder, reporting checklist, proposal
consistency checks, work-plan figures, follow-up edits, revision apply and
diff, library grounding, tracked-changes export, ChatGPT-backend event
parsing. Exit code 1 on failure."""
import ast, io, json, hashlib, re, sys, time, types, datetime, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WB = HERE / "workbench.py"
RESULTS = []


def check(name, fn):
    t0 = time.time()
    try:
        fn()
        RESULTS.append((name, "PASS", f"{time.time() - t0:.1f}s"))
    except Exception as e:
        RESULTS.append((name, "FAIL", f"{type(e).__name__}: {str(e)[:160]}"))


def build_namespace():
    src = WB.read_text(encoding="utf-8")
    tree = ast.parse(src)
    tmp = Path(tempfile.mkdtemp(prefix="golden_"))
    st = types.SimpleNamespace(session_state={"backend": "api", "effort": "xhigh"})
    G = {"__name__": "golden", "re": re, "io": io, "ast": ast, "json": json, "datetime": datetime,
         "Path": Path, "st": st, "ANSWERS_DIR": tmp, "_rwjson": json, "_rwhash": hashlib,
         "MODELS": {"Frontier (Fable 5.1)": "claude-fable-5-1"}, "api_key": "sk-test",
         "index_ok": False, "top_k": 8, "do_autosave": False, "model": "m", "model_label": "m",
         "_RW_UI": {}, "config": types.SimpleNamespace(MAX_ANSWER_TOKENS=4000, PDF_DIR=str(tmp)),
         "record_qa": lambda q, a, h, s: {"question": q, "answer": a, "hits": h, "time": "t"},
         "retrieve": lambda *a, **k: [], "build_context": lambda hits: "",
         "load_journals": lambda: {}, "rv_save_state": lambda s: None,
         "rw_save_state": lambda s: None, "_api_kwargs": lambda *a, **k: {},
         "_api_create": lambda *a, **k: None, "_track_usage": lambda *a: None,
         "_response_text": lambda r: "", "RW_TRANSIENT": ("529",), "RW_RETRY_WAITS": (1,),
         "claude_code_status": lambda refresh=False: {}}
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    G.update({"Document": Document, "Pt": Pt, "RGBColor": RGBColor, "Inches": Inches})
    # 1) module-level constants we need (regexes, prompts, tables)
    want_consts = {"RW_NUM_RE", "RW_LABEL_RE", "RW_CIT_RE", "RW_AY_RE", "RW_PM_RE", "RW_INTENS",
                   "RW_AUTHOR_RE", "RW_CHECKLIST", "RW_RECORD_WORDS", "RW_CERT_RE", "RW_LIB_MODES",
                   "JOURNAL_LIMITS", "RW_PROFILES_VERIFIED", "RX_POINTS_SYSTEM", "RX_RESPOND_SYSTEM",
                   "RX_LETTER_SYSTEM", "FU_SYSTEM", "PROP_MODEL_SYSTEM", "FIG_IMG_RE",
                   "WATCH_IMPACT_SYSTEM", "SUBMISSION_SYSTEM"}
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in want_consts
                                             for t in n.targets):
            exec(ast.get_source_segment(src, n), G)
    # 2) the figure engine region verbatim (sandbox, vision, core, pipeline)
    a = src.index("# ==========================================================================\n"
                  "# Figure engine - sandboxed renderer")
    b = src.index("\nFIG_IMG_RE = ")
    exec(compile(src[a:b], "figure_engine", "exec"), G)
    # 3) selected functions and classes by name
    want = {"_rw_norm_num", "rw_numbers_in", "_rw_labels", "_rw_citations", "_rw_between",
            "_rw_json_block", "_rw_strip_json", "rw_mechanical_audit", "rw_audit_table",
            "_rw_snippet", "rw_reporting_checklist", "rw_checklist_table", "rx_save_state",
            "rx_number_ok", "rx_numbers_strict", "RX_UNIT_RE", "rx_apply_changes", "rx_diff_paragraphs", "rx_word_diff", "rx_diff_docx",
            "rx_run", "rx_bundle", "rx_mark_sections", "fu_parse", "_fu_heading_key",
            "fu_apply_parts", "fu_pool", "fu_apply_rx", "prop_consistency_checks", "_pm_ids",
            "prop_gantt_source", "prop_wp_diagram_source", "prop_effort_source",
            "prop_workplan_docx", "prop_render_workplan_figures", "_prop_fig_paths",
            "rw_lib_cited", "rw_lib_register", "rw_lib_context", "rw_lib_reference_list",
            "rw_lib_queries_plan", "rw_lib_queries_section", "rv_register", "_rv_paper_key",
            "_rv_year_of", "rv_hits_for_numbers", "_tc_plain", "_tc_run", "_tc_mark_paragraph",
            "tracked_changes_docx", "tracked_changes_bytes", "extract_uploaded_table",
            "table_to_text", "_md_runs", "md_to_docx", "_png_width_in", "sub_limits_check",
            "_FuBox", "RX_DIR", "PROP_FIG_DIR", "_codex_parse_events", "_codex_effort",
            "_codex_error_message", "CODEX_EFFORT_ORDER", "rx_docs", "_rx_doc_for_change",
            "rc_deterministic", "_rc_figrefs", "RC_FIGREF_RE", "RC_DEFENSIVE", "RC_OBSEQUIOUS"}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in want:
            exec(ast.get_source_segment(src, n), G)
        elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in want
                                               for t in n.targets):
            exec(ast.get_source_segment(src, n), G)
    G["rw_log"] = lambda state, msg: state.setdefault("log", []).append(msg)
    return G


def main():
    G = build_namespace()
    V, R = G["fig_validate_source"], G["fig_render"]
    out = Path(G["ANSWERS_DIR"])

    def t_sandbox():
        bad = ["import os\ndef draw(fig, plt, np, mpl):\n    pass\n",
               "from matplotlib import font_manager\ndef draw(fig, plt, np, mpl):\n    pass\n",
               "def draw(fig, plt, np, mpl):\n    fig.canvas.print_figure('/tmp/x')\n",
               "def draw(fig, plt, np, mpl):\n    '{0.__class__}'.format(fig)\n",
               "def draw(fig, plt, np, mpl):\n    while True:\n        pass\n"]
        assert all(V(c) for c in bad), "static gate let something through"
        good = ("DATA = {}\ndef draw(fig, plt, np, mpl):\n    ax = fig.add_subplot(111)\n"
                "    ax.set_axis_off(); ax.set_xlim(0, 100); ax.set_ylim(0, 60)\n"
                "    ax.add_patch(mpl.patches.FancyBboxPatch((10, 20), 30, 12, boxstyle='round,pad=0.4'))\n"
                "    ax.text(25, 26, 'FAPbI$_3$', ha='center', va='center')\n")
        res = R(good, out / "g.png", out / "g.svg", width="single", height_mm=50, pdf_path=out / "g.pdf")
        assert res["ok"], res.get("error")
        w = int.from_bytes((out / "g.png").read_bytes()[16:20], "big")
        assert abs(w - round(89 / 25.4 * 300)) <= 2 and (out / "g.pdf").read_bytes()[:5] == b"%PDF-"
    check("sandbox gate + exact-width render + PDF", t_sandbox)

    def t_audits():
        pool = G["fig_numbers_in"]("T80 of 1,000 h and 500 h at 65 C; PCE 25 %")
        assert G["fig_data_audit"]({"T80": {"values": [1000, 500], "conditions": ["a", "b"],
                                            "source": "[4]"}}, pool, {"[4]", "4"}) == []
        bad = G["fig_data_audit"]({"x": {"values": [1234.5], "source": "[9]"}}, pool, {"[4]"})
        assert len(bad) == 2
        h = {"vals_y": [500.0, 777.0], "vals_x": [0.0, 1.0], "text_nums": ["750"], "quant_axes": 1,
             "n_errorbars": 0, "long_series": 0}
        p = G["fig_canvas_audit"](h, {"T80": {"values": [1000, 500], "source": "[4]"}}, True)
        assert any("777" in x for x in p) and any("750" in x for x in p)
        assert G["fig_canvas_audit"](h, {}, False) and "quantitative axis" in " ".join(G["fig_canvas_audit"](h, {}, False))
    check("DATA provenance + canvas audits", t_audits)

    def t_stage():
        plan = json.dumps({"figures": [
            {"number": 1, "kind": "mechanism", "class": "conceptual", "section_id": "R1", "title": "M",
             "claim": "c", "purpose": "p", "width": "single", "height_mm": 50, "panels": [], "brief": "b",
             "data": [], "epistemic": {"status": "INDIRECT", "evidence_ids": ["[4]"]}},
            {"number": 2, "kind": "workflow", "class": "conceptual", "section_id": "R1", "title": "Broken",
             "claim": "c", "purpose": "p", "width": "single", "panels": [], "brief": "b", "data": [],
             "epistemic": {"status": "DIRECT", "evidence_ids": []}}]})
        good = ("DATA = {}\ndef draw(fig, plt, np, mpl):\n    ax = fig.add_subplot(111)\n"
                "    ax.set_axis_off(); ax.set_xlim(0, 100); ax.set_ylim(0, 60)\n"
                "    ax.add_patch(mpl.patches.FancyBboxPatch((10, 20), 30, 12, boxstyle='round,pad=0.4', fc='#8C4A2F'))\n"
                "    ax.text(50, 6, 'Schematic, not to scale', ha='center', fontsize=7)\n")
        crash = "DATA = {}\ndef draw(fig, plt, np, mpl):\n    fig.add_subplot(111).plot([1, 2], [1])\n"
        wrap = lambda c, cap: f"```python\n{c}\n```\n<<<CAPTION>>>\n{cap}\n<<<END CAPTION>>>"
        script = iter([plan, wrap(good, "**Figure 1 | M.** a, x."), wrap(crash, "x"), wrap(crash, "x"), wrap(crash, "x")])
        G["rw_call"] = lambda s, u, m: next(script)
        state = {"sig": "golden", "stages": {"passages": {"[4]": "T80 1,000 h"}},
                 "registry": {"a": {"n": 4, "title": "A"}}, "opts": {"venue": "Nature Reviews", "pause_figs": True},
                 "inputs": {}}
        secs = [("R1", "Res", "## Res\n\nText [4].\n\nMore.\n")]
        figs, _ = G["fig_run_stage"](state, "rv", secs, "plan", "k", "m", lambda m: None, max_figs=2, vision=False)
        assert figs is None and state["status"] == "awaiting_figures"
        state["figs_approved"] = True
        figs, out_secs = G["fig_run_stage"](state, "rv", secs, "plan", "k", "m", lambda m: None, max_figs=2, vision=False)
        assert figs[0]["ok"] and "inferred from [4]" in figs[0]["caption"]
        assert not figs[1]["ok"] and figs[1].get("placeholder") and Path(figs[1]["placeholder"]).exists()
        assert out_secs[0][2].count("![") == 2
    check("figure stage: checkpoint, hedge, placeholder", t_stage)

    def t_checklist():
        chk = G["rw_reporting_checklist"]("n = 32 devices; reverse scan; AM 1.5G; 0.09 cm2 mask; MPP; "
                                          "standard deviation; ISOS-L-2; record efficiency", ["N2 glovebox"])
        by = {r["item"]: r for r in chk["rows"]}
        assert by["Device statistics (number of devices, n)"]["status"] == "present"
        assert by["Certification for record / highest / champion claims"]["severity"] == "MAJOR"
        assert by["Atmosphere / encapsulation during testing"]["status"].startswith("in sources only")
    check("PV reporting checklist", t_checklist)

    def t_consistency():
        model = {"duration_months": 36,
                 "objectives": [{"id": "O1", "text": "x", "kpis": ["k"]}, {"id": "O2", "text": "y", "kpis": []}],
                 "work_packages": [{"id": "WP1", "title": "A", "lead": "imec", "start_month": 1, "end_month": 18,
                                    "person_months": 24, "objectives": ["O1"], "depends_on": [], "tasks": []},
                                   {"id": "WP2", "title": "B", "lead": "", "start_month": 7, "end_month": 40,
                                    "person_months": 30, "objectives": [], "depends_on": ["WP9"], "tasks": []}],
                 "deliverables": [{"id": "D1.1", "title": "r", "wp": "WP1", "month": 12, "type": "report"}],
                 "milestones": [{"id": "MS1", "title": "m", "month": 12, "wps": ["WP1"], "means_of_verification": ""}],
                 "risks": [], "partners": [{"name": "imec", "role": "c", "person_months": 10}]}
        rows, matrix = G["prop_consistency_checks"](model)
        checks = {r["check"] for r in rows}
        for w in ("Objective addressed by no work package", "Work package without a deliverable",
                  "Work package runs past the project end", "Milestone without means of verification",
                  "Effort mismatch: WP person-months vs partner person-months"):
            assert w in checks, w
        figs, errors = G["prop_render_workplan_figures"](model, "golden_wp", 160.0)
        assert not errors and set(figs) == {"gantt", "wp_diagram", "effort"}
    check("proposal consistency + work-plan figures", t_consistency)

    def t_followup():
        _, edits = G["fu_parse"]("ok\n<<<EDITS>>>\n[{\"doc\": \"m\", \"find\": \"Stability was tested for 500 h.\", "
                                 "\"replace_with\": \"Stability was tested for 1,120 h.\", \"why\": \"w\"}, "
                                 "{\"doc\": \"m\", \"find\": \"Cells reach 26 % [1].\", \"replace_with\": \"Cells reach 27.5 % [1].\", \"why\": \"w\"}]\n<<<END EDITS>>>")
        parts = {"S1": "## Results\n\nCells reach 26 % [1]. Stability was tested for 500 h.\n"}
        new, applied, skipped = G["fu_apply_parts"](parts, edits, G["fu_pool"](["T80 = 1,120 h"]))
        assert len(applied) == 1 and len(skipped) == 1 and "1,120 h" in new["S1"] and "27.5" not in new["S1"]
    check("follow-up edits with number guard", t_followup)

    def t_revision():
        paras = ["Results", "The LiF interlayer improves FF from 78 % to 82 %. Stability was tested for 500 h.",
                 "Conclusions", "We conclude that LiF passivates the interface."]
        changes = [{"find": "Stability was tested for 500 h.", "replace_with": "Stability was tested for 1,120 h.", "where": "R"},
                   {"find": "We conclude that LiF passivates the interface.", "replace_with": "The data are consistent with passivation.", "where": "C"},
                   {"find": "FF from 78 % to 82 %", "replace_with": "FF from 78 % to 85 %", "where": "R"}]
        pool = G["rx_numbers_strict"]("notes: T80 = 1,120 h") | G["rx_numbers_strict"](" ".join(paras))
        new, applied, skipped = G["rx_apply_changes"](list(paras), changes, pool)
        assert len(applied) == 2 and len(skipped) == 1 and "85" in skipped[0]["reason"]
        diff = G["rx_diff_paragraphs"](paras, new)
        assert len(diff) == 2 and "~~" in diff[0]["marked"]
        docs = G["rx_docs"]({"inputs": {"ms_paragraphs": paras, "si_docs": [
            {"name": "si.docx", "text": "Figure S3\n\nJ-V curves of 12 devices.", "paragraphs": ["Figure S3", "J-V curves of 12 devices."]}]}})
        R = G["_rx_doc_for_change"]
        assert R({"doc": "si.docx", "find": "J-V curves of 12 devices."}, docs) == "si.docx"
        assert R({"doc": "manuscript", "find": "J-V curves of 12 devices."}, docs) == "si.docx"
        assert R({"doc": "SI", "find": "We conclude that LiF passivates the interface."}, docs) == "manuscript"
    check("revision apply + diff", t_revision)

    def t_library():
        assert G["rw_lib_cited"]("[L3] and [L1, L7]") == {3, 1, 7}
        state = {"registry": {}}
        hits = [{"meta": {"title": "A", "file": "1_stab_2024_A.pdf", "page_start": 1, "page_end": 1},
                 "text": "T80 of 1,200 h.", "score": 0.9}]
        nums = G["rw_lib_register"](state, hits)
        assert nums == [1] and G["rw_lib_context"](state, hits, nums).startswith("[L1] A")
        audit = G["rw_mechanical_audit"]("Prior work reached 1,200 h [L1]; also 999 h [L2].", ["x"], [], "x",
                                         library_texts=["T80 of 1,200 h."], library_ids={1})
        kinds = {(r["kind"][:20], r["item"]) for r in audit["rows"]}
        assert ("Literature value fro", "1200") in kinds and ("Library citation [Ln", "[L2]") in kinds
    check("library grounding: [Ln], registry, audit", t_library)

    def t_tracked():
        b = G["tracked_changes_bytes"]("## Intro\n\nCells reach 26 %.\n\nOld paragraph.",
                                       "## Intro\n\nCells reach 26 % under reverse scan.\n\nNew paragraph.", title="T")
        from docx import Document
        d = Document(io.BytesIO(b))
        xml = d.element.xml
        assert "<w:ins " in xml and "<w:del " in xml and "<w:delText" in xml
    check("Word Track Changes export", t_tracked)

    def t_codex():
        P = G["_codex_parse_events"]
        t, u, e = P('{"type":"thread.started","thread_id":"x"}\n{"type":"turn.started"}\n'
                    '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"working"}}\n'
                    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"FINAL"}}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":123,"cached_input_tokens":23,'
                    '"output_tokens":45,"reasoning_output_tokens":5}}\n')
        assert (t, u["input_tokens"], u["output_tokens"], e) == ("FINAL", 123, 45, "")
        t, u, e = P('{"type":"turn.failed","error":{"message":"usage limit reached"}}')
        assert e == "usage limit reached" and not t
        assert "Codex window" in G["_codex_error_message"](e, "", 1)
        levels = {"levels": ["low", "medium", "high", "xhigh"]}
        G["st"].session_state["effort"] = "max"
        assert G["_codex_effort"](levels) == "xhigh" and G["_codex_effort"]({}) == "max"
        G["st"].session_state["effort"] = "xhigh"
    check("ChatGPT backend: event parsing + effort clamp", t_codex)

    def t_revcheck():
        orig = "Results\n\nStability was tested for 500 h.\n\nConclusions\n\nLiF passivates the interface."
        rev = ("Results\n\nStability was tested for 1,120 h (Fig. S12) on 8 devices.\n\nConclusions\n\n"
               "The data are consistent with passivation. The champion reached 25.6 %.")
        letter = "We thank the reviewers. We extended the test to 1,120 h (Fig. S12, Table S9). Clearly the reviewer is wrong. EQE 91 %."
        st_ = {"inputs": {"orig_text": orig, "rev_text": rev, "orig_paragraphs": G["rx_mark_sections"](orig)[0],
                          "rev_paragraphs": G["rx_mark_sections"](rev)[0], "letter": letter,
                          "reviews": "R1: longer stability test.", "si_text": "Figure S12\n\nMPP for 1,120 h on 8 devices.", "notes": ""},
               "stages": {}}
        d = G["rc_deterministic"](st_)
        assert d["n_diffs"] == 2 and d["unsourced_numbers"] == ["25.6"], (d["n_diffs"], d["unsourced_numbers"])
        assert "91" in d["letter_numbers_missing"] and d["refs_missing"] == ["S9"], (d["letter_numbers_missing"], d["refs_missing"])
        assert "clearly" in d["defensive"] and "the reviewer is wrong" in d["defensive"] and d["thanks"] == 1
    check("revision check: diff, numbers, references, tone", t_revcheck)

    width = max(len(r[0]) for r in RESULTS)
    fails = 0
    print(f"\nGrapheAI golden runs - {datetime.datetime.now():%Y-%m-%d %H:%M} - workbench.py "
          f"{WB.stat().st_size // 1024} KB\n")
    for name, status, info in RESULTS:
        fails += status == "FAIL"
        print(f"  {status:4}  {name.ljust(width)}  {info}")
    print(f"\n{len(RESULTS) - fails}/{len(RESULTS)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
