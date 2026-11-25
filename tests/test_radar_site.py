"""Offline tests for the PV Radar static site builder (no network).

The extraction rules are deliberately conservative, so the tests pin both
what they accept and what they must refuse.
"""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "grapheai"))

import radar_site as rs  # noqa: E402

TODAY = datetime.date(2026, 11, 25)


# ------------------------------------------------------------------ efficiency

def test_efficiency_is_read_next_to_an_efficiency_word():
    assert rs.extract_efficiency(
        "JinkoSolar sets new world record of 35.24% for perovskite/TOPCon "
        "tandem cell efficiency") == 35.24
    assert rs.extract_efficiency(
        "NREL certifies 18.1% steady-state efficiency for modules") == 18.1


def test_efficiency_refuses_relative_changes_and_retention():
    assert rs.extract_efficiency("New coating boosts efficiency by 12%") is None
    assert rs.extract_efficiency(
        "Cell retains 95% of its initial efficiency after 1,000 h") is None
    assert rs.extract_efficiency(
        "Module prices fall 8% as efficiency race continues") is None


def test_efficiency_refuses_percentages_without_an_efficiency_word():
    assert rs.extract_efficiency("Company buys 49% stake in solar farm") is None
    assert rs.extract_efficiency("Tariff of 25% on imported modules") is None


def test_cell_type():
    assert rs.extract_cell_type("perovskite-silicon tandem solar cell") == "tandem"
    assert rs.extract_cell_type("2.81 m2 perovskite solar module") == "module"
    assert rs.extract_cell_type("perovskite solar cell") == "single junction"


# --------------------------------------------------------------------- funding

def test_funding_converts_units_and_currencies():
    assert rs.extract_funding("Tandem PV Raises $50M for US factory") == 50.0
    assert rs.extract_funding(
        "Tesla plans to invest about $3.1 billion in polysilicon") == 3100.0
    assert rs.extract_funding("Startup secures EUR 15 million grant") == 16.2


def test_funding_takes_the_amount_nearest_the_funding_word():
    # The raise is 15 million; the 1 billion is the cost of the factory.
    assert rs.extract_funding(
        "HoloSolis raises over €15 million to advance €1 billion "
        "solar gigafactory") == 16.2


def test_funding_needs_funding_language():
    assert rs.extract_funding("Developer wins $200 million module order") is None
    assert rs.extract_funding("Solar farm to save customers $1 billion") is None


def test_funding_does_not_read_other_dollars_as_us_dollars():
    v = rs.extract_funding("Utility opens R$60 million funding call")
    assert v is not None and v < 20          # Brazilian real, not US$60 M


# ------------------------------------------------------ signal, evidence, topics

def test_record_needs_a_stated_efficiency():
    t = "LONGi sets new world record of 34.9% tandem cell efficiency"
    assert rs.classify_signal(t, eff=34.9) == "Record"
    assert rs.classify_signal("Record year for solar installations") != "Record"


def test_simulated_values_are_not_records():
    t = "Simulation shows lead-free perovskite cell reaching record 32.41% efficiency"
    assert rs.evidence_grade(t) == "Simulated"
    assert rs.classify_signal(t, eff=32.41) != "Record"


def test_pv_radar_category_is_trusted():
    assert rs.classify_signal("Some headline", category="Policy") == "Policy"
    assert rs.classify_signal("Some filing", category="Patent") == "Patent"
    assert rs.classify_signal("Firm signs supply deal", category="Market") == "Deal"


def test_evidence_grades():
    assert rs.evidence_grade("certified by Fraunhofer ISE CalLab") == "Certified"
    assert rs.evidence_grade("a new cell", source="Wiley Online Library") \
        == "Peer-reviewed"
    assert rs.evidence_grade("Company claims 30% efficiency") == "Reported"


def test_topics():
    topics = rs.extract_topics("Perovskite-silicon tandem passes damp-heat test")
    assert {"Perovskite", "Tandem", "Silicon", "Stability"} <= set(topics)


# ------------------------------------------------------------------- companies

def test_company_matching_ignores_ordinary_phrases():
    m = rs.CompanyMatcher(rs.build_directory())
    assert m.find("JinkoSolar registers 35.24% tandem PV cell efficiency") \
        == ["jinkosolar"]
    assert m.find("Tandem PV raises $50M") == ["tandem-pv"]
    assert m.find("Longi and Trina sign deal") == ["longi", "trina-solar"]
    assert m.find("ines visits the tno plant") == []       # acronyms: exact case


def test_user_directory_rows_override_the_seed():
    d = rs.build_directory([{"name": "Oxford PV", "stage": "Commercial"},
                            {"name": "My Startup", "country": "Germany",
                             "tech": ["Perovskite"]}])
    by = {c["name"]: c for c in d}
    assert by["Oxford PV"]["stage"] == "Commercial"
    assert by["Oxford PV"]["country"] == "United Kingdom"
    assert by["My Startup"]["slug"] == "my-startup"


# --------------------------------------------------------------------- parsing

RSS = b"""<?xml version="1.0"?><rss><channel><title>Feed</title>
<item><title>Oxford PV ships tandem modules - pv magazine</title>
<link>https://example.org/a</link>
<pubDate>Mon, 23 Nov 2026 14:09:08 GMT</pubDate>
<description>&lt;a href="x"&gt;Oxford PV ships tandem modules&lt;/a&gt; pv magazine</description>
<source url="https://www.pv-magazine.com">pv magazine</source></item>
<item><title>Second story</title><link>https://example.org/b</link>
<pubDate>bad date</pubDate><description>Body &lt;b&gt;text&lt;/b&gt;</description></item>
</channel></rss>"""


def test_parse_rss_strips_outlet_suffix_and_markup():
    rows = rs.parse_rss(RSS)
    assert rows[0]["title"] == "Oxford PV ships tandem modules"
    assert rows[0]["source"] == "pv magazine"
    assert rows[0]["date"] == "2026-11-23"
    assert rows[0]["summary"] == ""           # it only repeated the headline
    assert rows[1]["summary"] == "Body text" and rows[1]["date"] == ""
    assert rows[1]["source"] == "Feed"


def test_parse_openalex():
    rows = rs.parse_openalex({"results": [{
        "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x",
        "title": "A <i>tandem</i> solar cell", "publication_date": "2026-11-20",
        "primary_location": {"source": {"display_name": "Joule"}},
        "authorships": [{"author": {"display_name": "A. One"}},
                        {"author": {"display_name": "B. Two"}}],
        "cited_by_count": 3, "open_access": {"is_oa": True}, "type": "article"}]})
    assert rows == [{"title": "A tandem solar cell",
                     "link": "https://doi.org/10.1/x", "journal": "Joule",
                     "date": "2026-11-20", "authors": "A. One et al.",
                     "oa": True, "cited_by": 3, "kind": "article",
                     "id": "https://openalex.org/W1"}]


def test_fetch_news_survives_a_dead_feed_and_filters_off_topic():
    def getter(url):
        if "dead" in url:
            raise OSError("down")
        return (b"<rss><channel><title>T</title>"
                b"<item><title>Perovskite solar cell hits 27% efficiency</title>"
                b"<link>https://example.org/1</link></item>"
                b"<item><title>Football results</title>"
                b"<link>https://example.org/2</link></item></channel></rss>")
    rows = rs.fetch_news([], ["https://dead.example/feed", "https://ok.example/feed"],
                         log=lambda *_: None, getter=getter, pause=0)
    assert [r["title"] for r in rows] == \
        ["Perovskite solar cell hits 27% efficiency"]


# ------------------------------------------------------------ ranking and build

def _row(title, date, **kw):
    return dict({"title": title, "summary": "", "link": "https://example.org/"
                 + rs.slugify(title), "source": "Outlet", "date": date}, **kw)


STORE = [
    _row("JinkoSolar sets record 35.24% efficiency for perovskite-silicon "
         "tandem solar cell", "2026-11-20", source="pv magazine"),
    _row("JinkoSolar certifies 35.24% tandem solar cell efficiency",
         "2026-11-23", source="Wire"),
    _row("Tandem PV Raises $50M for perovskite manufacturing", "2026-11-22"),
    _row("Module prices stable this week", "2026-11-24"),
    _row("Old item outside the week", "2026-10-01"),
    # A row PV Radar analysed: its values are kept as they are.
    _row("Analysed <script>alert(1)</script> item", "2026-11-21",
         efficiency_pct=26.1, cell_type="single junction", funding_musd=None,
         company="Acme Solar", country="Germany", category="Research"),
    _row("Second Acme Solar item", "2026-11-19", efficiency_pct=None,
         cell_type="unspecified", funding_musd=12, company="Acme Solar",
         country="Germany", category="Funding"),
]


def _site_items():
    directory = rs.build_directory()
    items = rs.enrich_news(STORE, rs.CompanyMatcher(directory))
    rs.add_untracked_companies(items, directory)
    return items, directory


def test_same_result_from_two_outlets_is_one_story_with_best_evidence():
    items, directory = _site_items()
    tracked = frozenset(c["slug"] for c in directory if c["tracked"])
    win, stories = rs.rank_window(items, TODAY - datetime.timedelta(days=6),
                                  TODAY, ("Perovskite", "Tandem"), tracked)
    assert len(win) == 6
    jinko = [s for s in stories if s["eff"] == 35.24]
    assert len(jinko) == 1
    assert jinko[0]["evidence"] == "Certified"
    assert set(jinko[0]["outlets"]) == {"pv magazine", "Wire"}
    assert stories[0] is jinko[0]                 # and it leads the week
    assert stories[-1]["title"] == "Module prices stable this week"


def test_pv_radar_values_are_kept_and_unknown_companies_are_added():
    items, directory = _site_items()
    acme = [c for c in directory if c["name"] == "Acme Solar"]
    assert acme and acme[0]["tracked"] is False and acme[0]["country"] == "Germany"
    row = next(i for i in items if i["title"].startswith("Analysed"))
    assert row["eff"] == 26.1 and row["analysed"] and row["signal"] == "Research"
    assert acme[0]["slug"] in row["companies"]


def test_build_writes_a_complete_site(tmp_path):
    data, out = tmp_path / "answers", tmp_path / "site"
    data.mkdir()
    (data / "pv_radar.json").write_text(json.dumps(STORE), encoding="utf-8")
    (data / "pv_site_papers.json").write_text(json.dumps([
        {"title": "Stable perovskite solar cells", "link": "https://doi.org/10.1/y",
         "journal": "Joule", "date": "2026-11-18", "authors": "A. One et al.",
         "id": "W2"},
        {"title": "A paper from the future", "link": "https://doi.org/10.1/z",
         "journal": "Joule", "date": "2027-01-01", "id": "W3"}]), encoding="utf-8")
    (data / "pv_site.json").write_text(json.dumps(
        {"title": "Test Radar", "owner": "A. Researcher",
         "base_url": "https://example.org/radar"}), encoding="utf-8")
    stats = rs.build(data, out, today=TODAY, log=lambda *_: None)
    assert stats["news"] == 7 and stats["papers"] == 1
    for name in ("index.html", "brief.html", "news.html", "papers.html",
                 "records.html", "companies.html", "primer.html", "about.html",
                 "company/jinkosolar.html", "company/acme-solar.html",
                 "brief/2026-W47.html", "assets/style.css", "assets/app.js",
                 "data/news.json", "data/papers.json", "sitemap.xml"):
        assert (out / name).is_file(), name
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Test Radar" in index and "35.24%" in index
    # Store text never reaches a page as markup.
    for page in out.rglob("*.html"):
        assert "<script>alert" not in page.read_text(encoding="utf-8"), page
    news = json.loads((out / "data" / "news.json").read_text(encoding="utf-8"))
    assert len(news) == 7 and news[0]["d"] >= news[-1]["d"]
    # PV Radar's own store is never republished; only site-fetched rows are.
    assert json.loads((out / "data" / "store_news.json").read_text()) == []
    # A relative link from a sub-page climbs back to the root.
    sub = (out / "company" / "jinkosolar.html").read_text(encoding="utf-8")
    assert 'href="../assets/style.css' in sub and 'href="../index.html"' in sub


def test_build_with_an_empty_store_still_produces_pages(tmp_path):
    stats = rs.build(tmp_path / "none", tmp_path / "site", today=TODAY,
                     log=lambda *_: None)
    assert stats["news"] == 0
    assert "No dated items" in (tmp_path / "site" / "index.html").read_text(
        encoding="utf-8")


def test_only_http_links_are_written():
    assert rs.safe_url("javascript:alert(1)") == ""
    assert rs.safe_url("https://example.org/x") == "https://example.org/x"


def test_refresh_merges_without_touching_pv_radar_store(tmp_path):
    (tmp_path / "pv_radar.json").write_text(json.dumps(STORE), encoding="utf-8")
    before = (tmp_path / "pv_radar.json").read_bytes()

    def getter(url):
        if "openalex" in url:
            return json.dumps({"results": []}).encode()
        return (b"<rss><channel><title>T</title><item><title>Solar module "
                b"factory opens</title><link>https://example.org/n</link>"
                b"</item></channel></rss>")
    n1, _ = rs.refresh(tmp_path, TODAY, log=lambda *_: None, getter=getter, pause=0)
    n2, _ = rs.refresh(tmp_path, TODAY, log=lambda *_: None, getter=getter, pause=0)
    assert n1 == n2 == 1                         # the second run adds nothing
    assert (tmp_path / "pv_radar.json").read_bytes() == before
