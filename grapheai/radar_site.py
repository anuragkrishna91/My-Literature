"""
PV Radar Site - by GrapheAI. Static public website built from the PV Radar store.

Turns the news store that PV Radar (pvradar.py) accumulates into a static,
dependency-free website: overview, weekly brief with archive, searchable
news and papers, efficiency results, company pages, a primer and a method
page. The output is plain HTML/CSS/JS that any static host can serve
(GitHub Pages, Cloudflare Pages, a university web folder) and that also
opens straight from disk.

Run with:
    python radar_site.py build                 # answers/ -> site/
    python radar_site.py build --fetch         # refresh news + papers first
    python radar_site.py build --fetch --serve # ... and preview locally

Reads (all optional, from --data-dir, default answers/):
    pv_radar.json        PV Radar's news store (model-extracted signals)
    pv_watchlists.json   PV Radar's saved searches and RSS feeds
    pv_companies.json    your additions to the company directory
    pv_site.json         site title, owner, contact, focus topics

Writes (only with --fetch, never touching PV Radar's own store):
    pv_site_news.json    headlines fetched without a model
    pv_site_papers.json  journal papers from OpenAlex

It needs no API key and no third-party package. Signals for items that
PV Radar has not analysed are extracted with the conservative rules in
this file; items already analysed by PV Radar keep its values.
"""

import argparse
import datetime
import email.utils
import html
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS_DIR = HERE / "radar_site_assets"

USER_AGENT = "PVRadarSite/1.0 (static site builder)"
FETCH_PAUSE_S = 1.0          # pause between requests to one run's sources
FETCH_TIMEOUT_S = 30

DEFAULT_FEEDS = ["https://www.pv-magazine.com/feed/",
                 "https://www.pv-tech.org/feed/"]
DEFAULT_QUERIES = ["perovskite solar cell",
                   "perovskite silicon tandem",
                   "solar cell efficiency record",
                   "solar module manufacturing",
                   "photovoltaic funding round"]
PAPER_TERMS = ['"solar cell"', '"solar cells"', "photovoltaic",
               '"solar module"', '"solar modules"', '"perovskite tandem"']

DEFAULT_SITE = {
    "title": "PV Radar",
    "tagline": "Photovoltaics news, papers and efficiency results, "
               "ranked every week.",
    "owner": "",
    "role": "",
    "about": "",
    "contact": "",
    "links": [],            # [{"label": "...", "url": "https://..."}]
    "base_url": "",         # e.g. https://name.github.io/radar (for sitemap)
    "focus_topics": ["Perovskite", "Tandem"],
}

# ------------------------------------------------------------------ text utils


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s or "x"


def safe_url(u):
    """Only http(s) links are ever written into the site."""
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else ""


def parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def fmt_date(d, year=True):
    if not d:
        return ""
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def fmt_range(a, b):
    return f"{fmt_date(a, a.year != b.year)} – {fmt_date(b)}"


def fmt_num(x, digits=1):
    if x is None:
        return ""
    s = f"{x:,.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def fmt_musd(x):
    if x is None:
        return ""
    if x >= 1000:
        return f"US${fmt_num(x / 1000, 2)} bn"
    return f"US${fmt_num(x, 1)} M"


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:. ") + "…"


def host_of(u):
    try:
        h = urllib.parse.urlparse(u).netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


# ------------------------------------------------------- rule-based extraction
# Conservative by design: a figure is only taken when the surrounding words
# say what it is. A missed value is acceptable; an invented one is not.

SIGNALS = ["Record", "Funding", "Deal", "Manufacturing", "Policy",
           "Technology", "Research", "Patent", "Market", "News"]
SIGNAL_WEIGHT = {"Record": 5.0, "Funding": 4.0, "Deal": 3.0,
                 "Manufacturing": 3.0, "Policy": 2.5, "Technology": 2.0,
                 "Research": 2.0, "Patent": 1.5, "Market": 1.5, "News": 1.0}

_PCT = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,3})?)\s?(?:%|per\s?cent|percent)")
_EFF_WORD = re.compile(r"efficien|\bPCE\b|conversion", re.I)
_REL_AFTER = re.compile(
    r"^\W{0,3}(?:relative|increase|improvement|gain|boost|higher|lower|more|"
    r"less|rise|drop|decline|reduction|degradation|loss|cheaper|stake|"
    r"share|of\s+(?:its|the|their)\s+initial|retention|retained)", re.I)
_REL_BEFORE = re.compile(
    r"(?:\bby|\bretain(?:s|ed|ing)?|\bkeep(?:s|ing)?|\bmaintain(?:s|ed|ing)?|"
    r"\blos(?:es|t|ing)|\bup|\bdown|\bcut(?:s|ting)?|\bsav(?:es|ed|ing)|"
    r"\bfall(?:s|ing)?|\bfell|\bris(?:es|ing)|\brose|\bgrow(?:s|n|ing)?|\bgrew|"
    r"\bdrop(?:s|ped|ping)?|\bjump(?:s|ed)?|\bclimb(?:s|ed)?|\bsurg(?:es|ed)|"
    r"\bslump(?:s|ed)?|\bgain(?:s|ed)?|\bimprov(?:es|ed)|\bincreas(?:es|ed)|"
    r"\bdecreas(?:es|ed)|\bdeclin(?:es|ed))"
    r"\s+(?:about\s+|around\s+|nearly\s+|up\s+to\s+)?$", re.I)


def extract_efficiency(text):
    """A stated power-conversion efficiency in percent, or None.

    Accepts a percentage between 3 and 50 that has an efficiency word within
    60 characters and is not phrased as a relative change or a retention.
    """
    text = text or ""
    for m in _PCT.finditer(text):
        v = float(m.group(1))
        if not 3.0 <= v <= 50.0:
            continue
        lo, hi = max(0, m.start() - 60), min(len(text), m.end() + 60)
        if not _EFF_WORD.search(text[lo:hi]):
            continue
        if _REL_AFTER.search(text[m.end():m.end() + 40]):
            continue
        if _REL_BEFORE.search(text[max(0, m.start() - 30):m.start()]):
            continue
        return v
    return None


def extract_cell_type(text):
    t = (text or "").lower()
    if re.search(r"tandem|multi-?junction|triple-?junction|double-?junction", t):
        return "tandem"
    if re.search(r"\bmodules?\b|\bpanels?\b|mini-?module", t):
        return "module"
    if re.search(r"solar cells?\b|\bcells?\b", t):
        return "single junction"
    return "unspecified"


# Approximate conversion to US$; shown as approximate on the site.
_FX = {"$": 1.0, "us$": 1.0, "usd": 1.0, "€": 1.08, "eur": 1.08,
       "£": 1.27, "gbp": 1.27, "cny": 0.14, "rmb": 0.14, "yuan": 0.14,
       "¥": 0.14, "inr": 0.012, "₹": 0.012, "rs": 0.012,
       "aud": 0.66, "a$": 0.66, "au$": 0.66, "cad": 0.73, "c$": 0.73,
       "ca$": 0.73, "jpy": 0.0067, "chf": 1.12, "sek": 0.095,
       "krw": 0.00073, "brl": 0.18, "r$": 0.18, "sgd": 0.74, "s$": 0.74,
       "hk$": 0.128, "nz$": 0.60, "nt$": 0.031}
_SCALE = {"k": 1e-3, "thousand": 1e-3, "m": 1.0, "mn": 1.0, "mm": 1.0,
          "million": 1.0, "b": 1e3, "bn": 1e3, "billion": 1e3,
          "crore": 10.0, "cr": 10.0, "lakh crore": 1e6, "trillion": 1e6}
_CUR = (r"US\$|AU\$|CA\$|HK\$|NZ\$|NT\$|A\$|C\$|R\$|S\$|(?<![A-Za-z])\$|"
        r"€|£|¥|₹|USD|EUR|GBP|CNY|RMB|INR|Rs\.?|AUD|CAD|JPY|CHF|SEK|KRW|"
        r"BRL|SGD")
_AMT = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNIT = (r"(lakh crore|thousand|million|billion|trillion|crore|"
         r"mn|bn|mm|cr|[kKmMbB])\b")
_MONEY_PRE = re.compile(rf"({_CUR})\s?{_AMT}\s?-?\s?(?:{_UNIT})?", re.I)
_MONEY_POST = re.compile(
    rf"{_AMT}\s?(?:{_UNIT})?\s?(USD|EUR|GBP|CNY|RMB|INR|AUD|CAD|JPY|CHF|"
    r"SEK|KRW|BRL|SGD|yuan|euros?|dollars?|pounds?|rupees?)\b", re.I)
_WORD_CUR = {"euro": "eur", "euros": "eur", "dollar": "usd",
             "dollars": "usd", "pound": "gbp", "pounds": "gbp",
             "rupee": "inr", "rupees": "inr"}
_FUND_WORD = re.compile(
    r"rais(?:e|es|ed|ing)|funding|financ(?:e|ing)|invest(?:s|ed|ment|ing)?|"
    r"grant|series [a-f]\b|seed round|\bIPO\b|loan|debt facility|"
    r"venture capital|equity", re.I)


def _to_musd(cur, amount, unit):
    cur = (cur or "").lower().rstrip(".")
    cur = _WORD_CUR.get(cur, cur)
    rate = _FX.get(cur)
    if rate is None:
        return None
    try:
        n = float(amount.replace(",", ""))
    except ValueError:
        return None
    unit = (unit or "").lower()
    if unit:
        scale = _SCALE.get(unit)
        if scale is None:
            return None
        return n * scale * rate
    # A bare number: treat it as units of currency (1,500,000 -> 1.5 M).
    return n * rate / 1e6 if n >= 1e5 else None


def extract_funding(text):
    """An investment / funding amount in US$ millions, or None.

    Only taken when a funding word is present in the same text, so that
    project costs, revenues and savings are not booked as funding. Of
    several amounts, the one nearest to a funding word is used: in "raises
    EUR 15 million for a EUR 1 billion factory" the raise is 15 million.
    """
    text = text or ""
    words = [m.start() for m in _FUND_WORD.finditer(text)]
    if not words:
        return None
    found = []
    for m in _MONEY_PRE.finditer(text):
        found.append((m.start(), _to_musd(m.group(1), m.group(2), m.group(3))))
    for m in _MONEY_POST.finditer(text):
        found.append((m.start(), _to_musd(m.group(3), m.group(1), m.group(2))))
    found = [(pos, v) for pos, v in found if v and 0.05 <= v <= 5e5]
    if not found:
        return None
    pos, best = min(found, key=lambda f: min(abs(f[0] - w) for w in words))
    return round(best, 2)


_RX = {
    "record": re.compile(r"\brecord\b|world.?first|highest (?:ever|certified|"
                         r"efficien)|new high|breaks?|surpass", re.I),
    "funding": re.compile(
        r"\brais(?:es|ed|ing)\b|funding|series [a-f]\b|seed round|\bIPO\b|"
        r"\bgrant\b|financing|secures? .{0,30}(?:loan|investment|capital)|"
        r"\binvests?\b|investment round|venture", re.I),
    "deal": re.compile(
        r"\bsigns?\b|agreement|\bMoU\b|\bLoI\b|partnership|partners? with|"
        r"acquir|acquisition|merger|supply (?:deal|contract|order)|"
        r"\border(?:s)?\b|\bcontract\b|\bPPA\b|joint venture|licens|"
        r"selected|wins? |to supply", re.I),
    "manufacturing": re.compile(
        r"factory|gigafactory|\bfab\b|production line|pilot line|"
        r"mass production|manufactur|capacity|ramp|plant\b|commission", re.I),
    "policy": re.compile(
        r"tariff|subsid|policy|regulat|auction|tender|ministry|anti-?dumping|"
        r"incentive|legislat|\bduty\b|duties|government|\bbill\b|"
        r"tax credit|ban\b|mandate", re.I),
    "technology": re.compile(
        r"launch|unveil|introduc|releases?|presents?|new (?:module|cell|"
        r"panel|product)|prototype|demonstrat|certif", re.I),
    "research": re.compile(
        r"researchers?|scientists?|study|university|institute|\bpaper\b|"
        r"journal|simulat|laborator|\blab\b|published", re.I),
}


_SIM = re.compile(r"simulat|\bSCAPS|numerical(?:ly)? model|modell?ing|"
                  r"theoretical|\bDFT\b|first-principles|could reach|"
                  r"predict(?:s|ed)? ", re.I)


def classify_signal(text, category=None, eff=None, funding=None):
    """One display class per item. `category` is PV Radar's own category
    when the item was analysed there; it is trusted over the keyword rules
    except that a stated record or funding amount is promoted."""
    t = text or ""
    if category == "Patent":
        return "Patent"
    if eff is not None and _RX["record"].search(t) and not _SIM.search(t):
        return "Record"
    if funding is not None or category == "Funding":
        return "Funding"
    if category in ("Policy", "Manufacturing", "Research", "Technology"):
        return category
    if category == "Market":
        return "Deal" if _RX["deal"].search(t) else "Market"
    for key, name in (("funding", "Funding"), ("deal", "Deal"),
                      ("manufacturing", "Manufacturing"),
                      ("policy", "Policy"), ("technology", "Technology"),
                      ("research", "Research")):
        if _RX[key].search(t):
            return name
    # A stated device efficiency is a technology result even when no
    # keyword says so.
    return "Technology" if eff is not None else "News"


TOPIC_RULES = [
    ("Perovskite", r"perovskite"),
    ("Tandem", r"tandem|multi-?junction|triple-?junction"),
    ("Silicon", r"silicon|\bTOPCon\b|heterojunction|\bHJT\b|\bSHJ\b|\bPERC\b|"
                r"back.?contact|\bIBC\b|\bc-Si\b|polysilicon|wafer|ingot"),
    ("Thin film", r"\bCdTe\b|cadmium telluride|\bCIGS\b|thin.?film|kesterite|"
                  r"\bCZTS"),
    ("Organic PV", r"organic (?:solar|photovolt)|\bOPV\b|non-?fullerene|"
                   r"polymer solar"),
    ("Stability", r"stabilit|degradat|durab|lifetime|\bT80\b|\bISOS\b|"
                  r"damp.?heat|encapsul|reliab"),
    ("Storage", r"storage|batter|\bBESS\b"),
    ("Utility-scale", r"utility.?scale|solar (?:farm|park|plant)|\b\d+(?:\.\d+)?"
                      r"\s?(?:MW|GW)p?\b.{0,40}(?:project|plant|farm|park)"),
    ("Rooftop", r"rooftop|residential|\bBIPV\b|building.?integrated|balcony"),
    ("Grid", r"\bgrid\b|curtail|interconnect|transmission"),
    ("Inverters", r"inverter"),
    ("Hydrogen", r"hydrogen|electroly[sz]"),
    ("Recycling", r"recycl|end.?of.?life|circular"),
]
_TOPIC_RX = [(name, re.compile(rx, re.I)) for name, rx in TOPIC_RULES]


def extract_topics(text):
    return [name for name, rx in _TOPIC_RX if rx.search(text or "")]


_CERT = re.compile(r"certif|independently (?:confirmed|verified|measured)|"
                   r"(?:confirmed|verified|measured) by", re.I)
_JOURNAL_HOSTS = ("nature.com", "science.org", "cell.com", "wiley.com",
                  "acs.org", "rsc.org", "sciencedirect.com", "springer.com",
                  "iop.org", "aip.org", "ieee.org", "mdpi.com", "pnas.org")
_JOURNAL_SRC = re.compile(
    r"wiley|^nature|sciencedirect|acs publications|royal society of chem|"
    r"springer|cell press|iopscience|aip publishing|\bmdpi\b|\bpnas\b|"
    r"^science$|science advances|^joule$", re.I)
_JOURNAL_TXT = re.compile(
    r"published in|in the journal|\b(?:Nature|Science|Joule|Solar RRL|"
    r"ACS Energy Letters|Advanced (?:Energy )?Materials|Progress in "
    r"Photovoltaics|Energy & Environmental Science)\b")


def evidence_grade(text, link="", source=""):
    """Simulated / Certified / Peer-reviewed / Reported - how a result is
    backed, judged only from the words of the item itself. A modelled
    value is flagged first: it is not a measurement, whoever published it."""
    if _SIM.search(text or ""):
        return "Simulated"
    if _CERT.search(text or ""):
        return "Certified"
    h = host_of(link)
    if any(j in h for j in _JOURNAL_HOSTS) or _JOURNAL_SRC.search(source or "") \
            or _JOURNAL_TXT.search(text or ""):
        return "Peer-reviewed"
    return "Reported"


EVIDENCE_RANK = {"Simulated": 3, "Certified": 2, "Peer-reviewed": 1,
                 "Reported": 0}


_PV_WORD = re.compile(
    r"solar|photovolt|\bPV\b|perovskite|\bTOPCon\b|heterojunction|\bHJT\b|"
    r"polysilicon|inverter|module", re.I)


def is_pv_relevant(text):
    return bool(_PV_WORD.search(text or ""))


# ------------------------------------------------------------ company directory
# Name, headquarters country and technology focus only: stable facts. Stage,
# funding and capacity change too fast to hard-code; add them in
# pv_companies.json if you track them.

SEED_COMPANIES = [
    ("Oxford PV", "United Kingdom", ["Perovskite-silicon tandem"], ["OxfordPV"], "https://www.oxfordpv.com", "Company"),
    ("LONGi", "China", ["Silicon", "Perovskite-silicon tandem"], ["Longi Green Energy", "LONGi Solar"], "https://www.longi.com", "Company"),
    ("JinkoSolar", "China", ["TOPCon", "Perovskite-silicon tandem"], ["Jinko Solar", "Jinko"], "https://www.jinkosolar.com", "Company"),
    ("Trina Solar", "China", ["TOPCon", "HJT", "Perovskite-silicon tandem"], ["Trinasolar", "Trina"], "https://www.trinasolar.com", "Company"),
    ("JA Solar", "China", ["Silicon"], ["JASolar"], "https://www.jasolar.com", "Company"),
    ("Tongwei", "China", ["Silicon", "Polysilicon"], ["Tongwei Solar"], "", "Company"),
    ("Canadian Solar", "Canada", ["TOPCon", "HJT"], [], "https://www.canadiansolar.com", "Company"),
    ("Aiko Solar", "China", ["Back-contact silicon"], ["AIKO", "Aiko"], "https://www.aikosolar.com", "Company"),
    ("Huasun", "China", ["HJT"], ["Huasun Energy"], "https://www.huasunsolar.com", "Company"),
    ("Risen Energy", "China", ["HJT"], [], "", "Company"),
    ("Astronergy", "China", ["TOPCon"], ["Chint New Energy"], "", "Company"),
    ("DAS Solar", "China", ["TOPCon"], [], "", "Company"),
    ("First Solar", "United States", ["CdTe thin film"], [], "https://www.firstsolar.com", "Company"),
    ("Hanwha Qcells", "South Korea", ["Silicon", "Perovskite-silicon tandem"], ["Qcells", "Q Cells", "Q-Cells"], "https://www.qcells.com", "Company"),
    ("Meyer Burger", "Switzerland", ["HJT"], [], "https://www.meyerburger.com", "Company"),
    ("Maxeon", "Singapore", ["Back-contact silicon"], ["Maxeon Solar"], "https://www.maxeon.com", "Company"),
    ("3Sun", "Italy", ["HJT"], ["Enel 3Sun", "3SUN"], "", "Company"),
    ("GCL Perovskite", "China", ["Perovskite", "Perovskite-silicon tandem"], ["GCL Optoelectronic"], "", "Company"),
    ("Microquanta", "China", ["Perovskite"], ["MicroQuanta", "Microquanta Semiconductor"], "https://www.microquanta.com", "Company"),
    ("UtmoLight", "China", ["Perovskite"], ["Utmo Light", "Utmost Light"], "https://www.utmolight.com", "Company"),
    ("Renshine Solar", "China", ["All-perovskite tandem"], ["Renshine"], "", "Company"),
    ("Wonder Solar", "China", ["Printable perovskite"], ["WonderSolar"], "", "Company"),
    ("Swift Solar", "United States", ["Perovskite tandem"], [], "https://www.swiftsolar.com", "Company"),
    ("Tandem PV", "United States", ["Perovskite-silicon tandem"], [], "https://www.tandempv.com", "Company"),
    ("Caelux", "United States", ["Perovskite-on-glass"], [], "https://www.caelux.com", "Company"),
    ("CubicPV", "United States", ["Perovskite-silicon tandem"], ["Cubic PV"], "https://www.cubicpv.com", "Company"),
    ("Active Surfaces", "United States", ["Flexible perovskite"], [], "", "Company"),
    ("Saule Technologies", "Poland", ["Flexible perovskite"], ["Saule"], "https://www.sauletech.com", "Company"),
    ("Sekisui Chemical", "Japan", ["Flexible perovskite"], ["Sekisui"], "", "Company"),
    ("Kaneka", "Japan", ["HJT", "Perovskite-silicon tandem"], [], "", "Company"),
    ("Panasonic", "Japan", ["Perovskite"], [], "", "Company"),
    ("Toshiba", "Japan", ["Flexible perovskite"], [], "", "Company"),
    ("EneCoat Technologies", "Japan", ["Perovskite"], ["EneCoat"], "", "Company"),
    ("Halocell Energy", "Australia", ["Flexible perovskite"], ["Halocell"], "", "Company"),
    ("Power Roll", "United Kingdom", ["Flexible perovskite"], ["PowerRoll"], "", "Company"),
    ("Solaronix", "Switzerland", ["Perovskite"], [], "https://www.solaronix.com", "Company"),
    ("Heliatek", "Germany", ["Organic PV"], [], "https://www.heliatek.com", "Company"),
    ("Fraunhofer ISE", "Germany", ["Research institute"], ["Fraunhofer Institute for Solar Energy Systems"], "https://www.ise.fraunhofer.de", "Institute"),
    ("NREL", "United States", ["Research institute"], ["National Renewable Energy Laboratory", "National Laboratory of the Rockies"], "", "Institute"),
    ("Helmholtz-Zentrum Berlin", "Germany", ["Research institute"], ["HZB"], "https://www.helmholtz-berlin.de", "Institute"),
    ("KAUST", "Saudi Arabia", ["Research institute"], ["King Abdullah University of Science and Technology"], "", "Institute"),
    ("EPFL", "Switzerland", ["Research institute"], [], "", "Institute"),
    ("CSEM", "Switzerland", ["Research institute"], [], "", "Institute"),
    ("imec", "Belgium", ["Research institute"], [], "", "Institute"),
    ("TNO", "Netherlands", ["Research institute"], [], "", "Institute"),
    ("UNSW", "Australia", ["Research institute"], ["University of New South Wales"], "", "Institute"),
    ("ISFH", "Germany", ["Research institute"], ["Institute for Solar Energy Research Hamelin"], "", "Institute"),
    ("CEA-INES", "France", ["Research institute"], ["CEA INES", "INES"], "", "Institute"),
]


def build_directory(user_rows=None):
    """Seed directory merged with the user's pv_companies.json rows.
    A user row with the same name replaces the seed row."""
    out = {}
    for name, country, tech, aliases, site, kind in SEED_COMPANIES:
        out[name.lower()] = {"name": name, "country": country,
                             "tech": list(tech), "aliases": list(aliases),
                             "website": site, "kind": kind, "stage": "",
                             "note": "", "tracked": True}
    for r in user_rows or []:
        if not isinstance(r, dict) or not str(r.get("name", "")).strip():
            continue
        name = str(r["name"]).strip()
        base = out.get(name.lower(), {"name": name, "country": "", "tech": [],
                                      "aliases": [], "website": "",
                                      "kind": "Company", "stage": "",
                                      "note": "", "tracked": True})
        for k in ("country", "website", "kind", "stage", "note"):
            if r.get(k):
                base[k] = str(r[k])
        for k in ("tech", "aliases"):
            if isinstance(r.get(k), list):
                base[k] = [str(x) for x in r[k] if str(x).strip()]
        out[name.lower()] = base
    rows = list(out.values())
    used = set()
    for c in rows:
        s = slugify(c["name"])
        while s in used:
            s += "-2"
        used.add(s)
        c["slug"] = s
    return rows


# Names that are also ordinary phrases: not a company when these words follow.
_NOT_A_NAME_BEFORE = {
    "tandem pv": re.compile(r"\s+(?:cells?|modules?|technolog\w*|efficien\w*|"
                            r"devices?|panels?|products?|research|records?|"
                            r"market|manufactur\w*)\b", re.I),
}


class CompanyMatcher:
    """Finds directory entries in a headline by whole-word name or alias."""

    def __init__(self, directory):
        self.directory = directory
        self.by_slug = {c["slug"]: c for c in directory}
        self._alias = {}
        self._form = {}
        pats = []
        for c in directory:
            for n in [c["name"]] + c["aliases"]:
                n = n.strip()
                if len(n) < 3:
                    continue
                self._alias[n.lower()] = c["slug"]
                self._form[n.lower()] = n
                pats.append(re.escape(n))
        pats.sort(key=len, reverse=True)
        self._rx = (re.compile(r"(?<![A-Za-z0-9])(" + "|".join(pats)
                               + r")(?![A-Za-z0-9])", re.I)
                    if pats else None)

    def find(self, text):
        if not self._rx:
            return []
        seen = []
        for m in self._rx.finditer(text or ""):
            hit = m.group(1)
            form = self._form.get(hit.lower(), "")
            if form.isupper() and len(form) <= 6:
                if hit != form:            # acronyms: exact case only
                    continue
            elif form[:1].isupper() and not hit[:1].isupper():
                continue                   # "tandem PV cell" is not Tandem PV
            follow = _NOT_A_NAME_BEFORE.get(hit.lower())
            if follow and follow.match(text[m.end():]):
                continue
            s = self._alias.get(hit.lower())
            if s and s not in seen:
                seen.append(s)
        return seen

    def resolve(self, name):
        """Slug for a company name as PV Radar stored it, or None."""
        if not name:
            return None
        n = str(name).strip().lower()
        if n in self._alias:
            return self._alias[n]
        hits = self.find(str(name))
        return hits[0] if hits else None


# ---------------------------------------------------------------------- fetchers


def http_get(url, timeout=FETCH_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                                      html.unescape(s or ""))).strip()


def parse_rss(xml_bytes, default_source="", n=60):
    """Items of an RSS 2.0 document as store rows (title, summary, link,
    source, date). Google News puts the outlet in <source> and repeats it
    at the end of the title; both are handled."""
    root = ET.fromstring(xml_bytes)
    feed_title = (root.findtext("channel/title") or "").strip()
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        summary = _strip_tags(it.findtext("description") or "")[:600]
        if source and summary.endswith(source):
            summary = summary[: -len(source)].strip()
        if summary.lower().startswith(title.lower()[:60]):
            summary = ""            # the feed only repeated the headline
        try:
            date = email.utils.parsedate_to_datetime(
                it.findtext("pubDate") or "").date().isoformat()
        except Exception:
            date = ""
        items.append({"title": title, "summary": summary,
                      "link": (it.findtext("link") or "").strip(),
                      "source": source or feed_title or default_source,
                      "date": date})
        if len(items) >= n:
            break
    return items


def google_news_url(query, freshness="when:14d"):
    q = f"{query} {freshness}".strip()
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
            + "&hl=en-US&gl=US&ceid=US:en")


def fetch_news(queries, feeds, log=print, getter=http_get, pause=FETCH_PAUSE_S):
    """Headlines from Google News searches and RSS feeds, PV-relevant only."""
    rows = []
    jobs = ([(google_news_url(q), q, f"search: {q}") for q in queries]
            + [(u, f"feed:{u}", f"feed: {host_of(u)}") for u in feeds])
    for i, (url, tag, label) in enumerate(jobs):
        if i and pause:
            time.sleep(pause)
        try:
            got = parse_rss(getter(url), default_source=host_of(url))
        except Exception as e:           # one dead feed must not stop a build
            log(f"  ! {label}: {type(e).__name__}: {e}")
            continue
        kept = [dict(r, query=tag) for r in got
                if is_pv_relevant(r["title"] + " " + r["summary"])]
        log(f"  {label}: {len(kept)} of {len(got)} items kept")
        rows.extend(kept)
    return rows


def openalex_url(terms, since, until, page, mailto=""):
    flt = ("title.search:(" + " OR ".join(terms) + "),"
           f"from_publication_date:{since},to_publication_date:{until},"
           "type:article|preprint")
    q = {"filter": flt, "sort": "publication_date:desc", "per-page": "200",
         "page": str(page),
         "select": "id,doi,title,publication_date,primary_location,"
                   "authorships,cited_by_count,open_access,type"}
    if mailto:
        q["mailto"] = mailto
    return "https://api.openalex.org/works?" + urllib.parse.urlencode(q)


def parse_openalex(payload):
    rows = []
    for w in payload.get("results", []):
        title = _strip_tags(w.get("title") or "")
        if not title:
            continue
        loc = w.get("primary_location") or {}
        src = (loc.get("source") or {}).get("display_name") or ""
        authors = [((a.get("author") or {}).get("display_name") or "")
                   for a in (w.get("authorships") or [])]
        authors = [a for a in authors if a]
        if not authors:
            byline = ""
        elif len(authors) == 1:
            byline = authors[0]
        else:
            byline = f"{authors[0]} et al."
        link = w.get("doi") or loc.get("landing_page_url") or w.get("id") or ""
        rows.append({"title": title, "link": link, "journal": src,
                     "date": (w.get("publication_date") or "")[:10],
                     "authors": byline,
                     "oa": bool((w.get("open_access") or {}).get("is_oa")),
                     "cited_by": int(w.get("cited_by_count") or 0),
                     "kind": w.get("type") or "article",
                     "id": w.get("id") or link})
    return rows


def fetch_papers(since, until, terms=None, max_pages=5, mailto="", log=print,
                 getter=http_get, pause=FETCH_PAUSE_S):
    """Journal articles and preprints with PV terms in the title (OpenAlex)."""
    rows = []
    for page in range(1, max_pages + 1):
        if page > 1 and pause:
            time.sleep(pause)
        try:
            payload = json.loads(getter(openalex_url(
                terms or PAPER_TERMS, since, until, page, mailto)))
        except Exception as e:
            log(f"  ! OpenAlex page {page}: {type(e).__name__}: {e}")
            break
        got = parse_openalex(payload)
        rows.extend(got)
        if len(got) < 200:
            break
    log(f"  OpenAlex: {len(rows)} papers, {since} to {until}")
    return rows


# ------------------------------------------------------------------------- store


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def merge_rows(*lists, key=lambda r: r.get("link") or r.get("title")):
    """Concatenate row lists; the first occurrence of a key wins."""
    seen, out = set(), []
    for rows in lists:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            k = key(r)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(r)
    return out


def load_site_config(data_dir):
    cfg = dict(DEFAULT_SITE)
    user = load_json(Path(data_dir) / "pv_site.json", {})
    if isinstance(user, dict):
        for k in cfg:
            if k in user:
                cfg[k] = user[k]
    return cfg


def watch_sources(data_dir):
    """Queries and feeds: PV Radar's watchlists when present, else defaults."""
    w = load_json(Path(data_dir) / "pv_watchlists.json", {})
    queries = [q for q in (w.get("queries") or []) if isinstance(q, str)]
    feeds = [f for f in (w.get("feeds") or []) if safe_url(f)] \
        if "feeds" in w else list(DEFAULT_FEEDS)
    return (queries or list(DEFAULT_QUERIES)), feeds


def refresh(data_dir, today, seed_url="", paper_days=30, log=print,
            getter=http_get, pause=FETCH_PAUSE_S):
    """Fetch headlines and papers into the site's own two files."""
    data_dir = Path(data_dir)
    news_file = data_dir / "pv_site_news.json"
    papers_file = data_dir / "pv_site_papers.json"
    news = load_json(news_file, [])
    papers = load_json(papers_file, [])
    if seed_url:
        # A hosted build has no disk between runs: the previous deployment
        # carries the history, and is read back here before adding to it.
        base = seed_url.rstrip("/") + "/data/"
        for name, target in (("store_news.json", news),
                             ("store_papers.json", papers)):
            try:
                prev = json.loads(getter(base + name))
                if isinstance(prev, list):
                    target.extend(prev)
                    log(f"  seed {name}: {len(prev)} rows")
            except Exception as e:
                log(f"  seed {name}: not available ({type(e).__name__})")
    log("Fetching headlines")
    queries, feeds = watch_sources(data_dir)
    news = merge_rows(news, fetch_news(queries, feeds, log, getter, pause))
    log("Fetching papers")
    mailto = os.environ.get("LITERATURE_EMAIL", "")
    since = (today - datetime.timedelta(days=paper_days)).isoformat()
    papers = merge_rows(papers,
                        fetch_papers(since, today.isoformat(), mailto=mailto,
                                     log=log, getter=getter, pause=pause),
                        key=lambda r: r.get("id") or r.get("link"))
    save_json(news_file, news)
    save_json(papers_file, papers)
    return len(news), len(papers)


# -------------------------------------------------------------------- enrichment

_STOP = set("a an the of for in on at to and or with by from as is are be its "
            "it this that new says said will after into over up than".split())


def title_tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (title or "").lower())
            if w not in _STOP and len(w) > 1}


def enrich_news(rows, matcher):
    """Store rows -> display items. Values already present in a row (from
    PV Radar's model analysis) are kept; missing ones come from the rules."""
    items, seen_titles = [], set()
    for r in rows:
        title = re.sub(r"\s+", " ", str(r.get("title") or "")).strip()
        if not title:
            continue
        norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        if norm in seen_titles:
            continue
        seen_titles.add(norm)
        summary = re.sub(r"\s+", " ", str(r.get("summary") or "")).strip()
        text = f"{title}. {summary}"
        analysed = "category" in r           # row went through PV Radar
        eff = r.get("efficiency_pct") if analysed else extract_efficiency(text)
        try:
            eff = float(eff) if eff is not None else None
        except (TypeError, ValueError):
            eff = None
        if eff is not None and not 0 < eff <= 60:
            eff = None
        fund = r.get("funding_musd") if analysed else extract_funding(text)
        try:
            fund = float(fund) if fund is not None else None
        except (TypeError, ValueError):
            fund = None
        if fund is not None and fund <= 0:
            fund = None
        cell = r.get("cell_type") if analysed else None
        if not cell or cell == "unspecified":
            cell = extract_cell_type(text) if eff is not None else "unspecified"
        link = safe_url(r.get("link"))
        source = str(r.get("source") or "").strip()
        if re.match(r"^https?://", source, re.I):
            source = host_of(source)
        source = source or host_of(link)
        cos = matcher.find(title)
        named = matcher.resolve(r.get("company")) if r.get("company") else None
        if named and named not in cos:
            cos.insert(0, named)
        for s in matcher.find(summary):
            if s not in cos and len(cos) < 4:
                cos.append(s)
        items.append({
            "title": title, "summary": summary, "link": link,
            "source": source, "date": parse_date(r.get("date")),
            "eff": eff, "cell": cell, "fund": fund,
            "signal": classify_signal(text, r.get("category"), eff, fund),
            "topics": extract_topics(text),
            "evidence": evidence_grade(text, link, source),
            "companies": cos,
            "company_raw": (str(r.get("company")).strip()
                            if r.get("company") and not named else ""),
            "country": str(r.get("country") or "").strip(),
            "analysed": analysed, "outlets": [source] if source else [],
        })
    items.sort(key=lambda x: (x["date"] or datetime.date.min, x["title"]),
               reverse=True)
    return items


def enrich_papers(rows, today):
    out, seen = [], set()
    for r in rows:
        title = re.sub(r"\s+", " ", str(r.get("title") or "")).strip()
        d = parse_date(r.get("date"))
        if not title or not d or d > today:
            continue
        k = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": title, "link": safe_url(r.get("link")),
                    "journal": str(r.get("journal") or "").strip()
                    or ("Preprint" if r.get("kind") == "preprint" else ""),
                    "date": d, "authors": str(r.get("authors") or ""),
                    "oa": bool(r.get("oa")),
                    "cited_by": int(r.get("cited_by") or 0),
                    "topics": extract_topics(title)})
    out.sort(key=lambda x: (x["date"], x["title"]), reverse=True)
    return out


def add_untracked_companies(items, directory, min_items=2):
    """Organisations PV Radar named that the directory does not list become
    entries of their own once they appear in `min_items` items."""
    counts, country = {}, {}
    for it in items:
        n = it["company_raw"]
        if n:
            counts[n] = counts.get(n, 0) + 1
            if it["country"]:
                country.setdefault(n, it["country"])
    used = {c["slug"] for c in directory}
    added = {}
    for n, k in counts.items():
        if k < min_items or len(n) > 80:
            continue
        s = slugify(n)
        while s in used:
            s += "-2"
        used.add(s)
        added[n] = s
        directory.append({"name": n, "slug": s, "country": country.get(n, ""),
                          "tech": [], "aliases": [], "website": "",
                          "kind": "Company", "stage": "", "note": "",
                          "tracked": False})
    for it in items:
        s = added.get(it["company_raw"])
        if s and s not in it["companies"]:
            it["companies"].insert(0, s)
    return directory


# ------------------------------------------------------------ ranking and weeks


def cluster_stories(items, threshold=0.6, window_days=4):
    """Group reports of the same story: similar headlines within a few
    days, or the same stated efficiency (or funding amount) with a shared
    organisation or partly shared wording. Returns one representative per
    group, carrying every outlet and the strongest evidence grade."""
    reps = []
    for it in sorted(items, key=lambda x: (x["date"] or datetime.date.min)):
        tok = title_tokens(it["title"])
        home = None
        for rep in reversed(reps):
            if it["date"] and rep["date"] and \
                    (it["date"] - rep["date"]).days > window_days:
                break
            rt = rep["_tok"]
            sim = len(tok & rt) / len(tok | rt) if tok and rt else 0.0
            shared = bool(set(it["companies"]) & set(rep["companies"]))
            same_eff = (it["eff"] is not None and rep["eff"] is not None
                        and abs(it["eff"] - rep["eff"]) < 0.005
                        and (shared or sim >= 0.2))
            same_fund = (it["fund"] and rep["fund"] and sim >= 0.15
                         and abs(it["fund"] - rep["fund"])
                         <= 0.1 * max(it["fund"], rep["fund"]))
            if sim >= threshold or same_eff or same_fund:
                home = rep
                break
        if home is None:
            rep = dict(it)
            rep["_tok"] = tok
            rep["outlets"] = list(it["outlets"])
            rep["companies"] = list(it["companies"])
            reps.append(rep)
        else:
            for o in it["outlets"]:
                if o and o not in home["outlets"]:
                    home["outlets"].append(o)
            for s in it["companies"]:
                if s not in home["companies"]:
                    home["companies"].append(s)
            grade = max(it["evidence"], home["evidence"],
                        key=EVIDENCE_RANK.get)
            # Keep the richer report as the face of the story.
            if (it["eff"] is not None and home["eff"] is None) or \
                    (it["fund"] is not None and home["fund"] is None):
                keep = (home["outlets"], home["companies"], home["_tok"])
                home.update({k: v for k, v in it.items()
                             if k not in ("outlets", "companies")})
                home["outlets"], home["companies"], home["_tok"] = keep
            home["evidence"] = grade
            if SIGNAL_WEIGHT.get(it["signal"], 0) > \
                    SIGNAL_WEIGHT.get(home["signal"], 0) \
                    and grade != "Simulated":
                home["signal"] = it["signal"]
            home["_tok"] = tok | home["_tok"]
    for rep in reps:
        rep.pop("_tok", None)
    return reps


def score_item(it, focus_topics=(), tracked=frozenset()):
    """The published ranking formula (see the method page)."""
    s = SIGNAL_WEIGHT.get(it["signal"], 1.0)
    if it["evidence"] == "Certified":
        s += 2.0
    elif it["evidence"] == "Peer-reviewed":
        s += 1.0
    elif it["evidence"] == "Simulated":
        s -= 1.0
    if it["fund"]:
        s += min(2.0, math.log10(1 + it["fund"]) / 1.5)
    s += min(2.0, 0.75 * (len(it["outlets"]) - 1))
    if any(t in focus_topics for t in it["topics"]):
        s += 1.5
    if any(c in tracked for c in it["companies"]):
        s += 0.5
    return round(s, 2)


def rank_window(items, start, end, focus_topics, tracked):
    win = [it for it in items if it["date"] and start <= it["date"] <= end]
    stories = cluster_stories(win)
    for st in stories:
        st["score"] = score_item(st, focus_topics, tracked)
    stories.sort(key=lambda x: (x["score"], x["date"], x["title"]),
                 reverse=True)
    return win, stories


def week_start(d):
    return d - datetime.timedelta(days=d.weekday())


def week_id(monday):
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


# ----------------------------------------------------------------------- render

NAV = [("index.html", "Overview"), ("brief.html", "Weekly brief"),
       ("news.html", "News"), ("papers.html", "Papers"),
       ("records.html", "Efficiency"), ("companies.html", "Companies"),
       ("primer.html", "Primer"), ("about.html", "About")]

EVIDENCE_HELP = {
    "Simulated": "A modelled or simulated value, not a measured device.",
    "Certified": "The item says an independent test laboratory measured or "
                 "certified the result.",
    "Peer-reviewed": "Published in, or attributed to, a scientific journal.",
    "Reported": "Stated by the company or the press; no independent "
                "measurement is mentioned.",
}
CELL_LABEL = {"single junction": "Single junction", "tandem": "Tandem",
              "module": "Module", "unspecified": "Unspecified"}
CELL_ORDER = ["single junction", "tandem", "module", "unspecified"]


class Site:
    def __init__(self, cfg, items, papers, directory, today, built):
        self.cfg = cfg
        self.items = items
        self.papers = papers
        self.directory = directory
        self.by_slug = {c["slug"]: c for c in directory}
        self.today = today
        self.built = built
        self.focus = tuple(cfg.get("focus_topics") or ())
        self.tracked = frozenset(c["slug"] for c in directory if c["tracked"])
        self.co_items = {}
        for it in items:
            for s in it["companies"]:
                self.co_items.setdefault(s, []).append(it)
        self.asset_v = str(int(built.timestamp()))

    # ---- shared pieces

    def page(self, path, title, body, description="", page_class=""):
        depth = path.count("/")
        root = "../" * depth
        nav = "".join(
            f'<a href="{root}{href}"'
            + (' aria-current="page"' if href == path or
               (href == "brief.html" and path.startswith("brief/")) or
               (href == "companies.html" and path.startswith("company/"))
               else "")
            + f">{esc(label)}</a>" for href, label in NAV)
        site_title = esc(self.cfg["title"])
        full = site_title if not title else f"{esc(title)} · {site_title}"
        desc = esc(description or self.cfg["tagline"])
        owner = (f' · {esc(self.cfg["owner"])}'
                 if self.cfg.get("owner") else "")
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{full}</title>
<meta name="description" content="{desc}">
<meta name="generator" content="GrapheAI PV Radar Site">
<link rel="stylesheet" href="{root}assets/style.css?v={self.asset_v}">
<script src="{root}assets/app.js?v={self.asset_v}" defer></script>
</head>
<body class="{esc(page_class)}" data-root="{root}">
<a class="skip" href="#main">Skip to content</a>
<header class="bar"><div class="wrap bar-in">
<a class="brand" href="{root}index.html"><svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><rect x="2" y="3" width="20" height="5" rx="1.5" class="b1"/><rect x="2" y="9.5" width="20" height="5" rx="1.5" class="b2"/><rect x="2" y="16" width="20" height="5" rx="1.5" class="b3"/></svg><span>{site_title}</span></a>
<nav class="nav" aria-label="Pages">{nav}</nav>
</div></header>
<main id="main">
{body}
</main>
<footer class="foot"><div class="wrap foot-in">
<p><strong>{site_title}</strong>{owner}</p>
<p>Updated {esc(fmt_date(self.built.date()))}, {self.built.strftime('%H:%M')} UTC · <a href="{root}about.html#method">How the ranking works</a></p>
<p class="fine">Headlines link to their publishers. Figures are as stated in the linked item and are not investment advice.</p>
</div></footer>
</body>
</html>
"""

    def chip_signal(self, sig):
        return f'<span class="sig sig-{slugify(sig)}">{esc(sig)}</span>'

    def chip_evidence(self, ev):
        return (f'<span class="ev ev-{slugify(ev)}" '
                f'title="{esc(EVIDENCE_HELP.get(ev, ""))}">{esc(ev)}</span>')

    def link(self, it, cls=""):
        t = esc(it["title"])
        if not it["link"]:
            return f'<span class="{cls}">{t}</span>'
        return (f'<a class="{cls} out" href="{esc(it["link"])}" '
                f'rel="noopener noreferrer">{t}</a>')

    def co_links(self, slugs, root=""):
        return ", ".join(
            f'<a href="{root}company/{esc(s)}.html">'
            f'{esc(self.by_slug[s]["name"])}</a>'
            for s in slugs if s in self.by_slug)

    def facts(self, it):
        """The measured values an item states, as compact data chips."""
        out = []
        if it["eff"] is not None:
            out.append(f'<span class="fact"><b>{fmt_num(it["eff"], 2)}%</b> '
                       f'{esc(CELL_LABEL.get(it["cell"], "").lower())}</span>')
        if it["fund"]:
            out.append(f'<span class="fact"><b>{esc(fmt_musd(it["fund"]))}'
                       f'</b> funding</span>')
        return "".join(out)

    def story(self, st, rank=None, root="", lead=False):
        outlets = st["outlets"] or ([st["source"]] if st["source"] else [])
        src = esc(outlets[0]) if outlets else ""
        if len(outlets) > 1:
            src += (f' <span class="also">+ {len(outlets) - 1} more: '
                    f'{esc(", ".join(outlets[1:4]))}</span>')
        cos = self.co_links(st["companies"], root)
        meta = " · ".join(x for x in (
            src, esc(fmt_date(st["date"], False)), cos) if x)
        summ = (f'<p class="sum">{esc(clip(st["summary"], 260 if lead else 180))}</p>'
                if st["summary"] else "")
        ev = self.chip_evidence(st["evidence"]) if st["eff"] is not None else ""
        rk = f'<span class="rank">{rank}</span>' if rank else ""
        score = (f'<span class="score" title="Ranking score">'
                 f'{fmt_num(st.get("score", 0), 1)}</span>'
                 if "score" in st else "")
        return (f'<li class="story{" lead" if lead else ""}">{rk}'
                f'<div class="story-main"><div class="tags">'
                f'{self.chip_signal(st["signal"])}{ev}{self.facts(st)}</div>'
                f'<h3>{self.link(st)}</h3>{summ}'
                f'<p class="meta">{meta}</p></div>{score}</li>')

    # ---- charts (inline SVG, drawn to one scale, hover handled in app.js)

    def chart_weeks(self, n=12):
        end = week_start(self.today)
        weeks = [end - datetime.timedelta(weeks=i) for i in range(n - 1, -1, -1)]
        counts = []
        for w in weeks:
            w_end = w + datetime.timedelta(days=6)
            counts.append(sum(1 for it in self.items
                              if it["date"] and w <= it["date"] <= w_end))
        if not any(counts):
            return ""
        first = next(i for i, c in enumerate(counts) if c)
        cut = min(first, n - 4)        # keep at least four weeks on the axis
        weeks, counts, n = weeks[cut:], counts[cut:], n - cut
        top = _nice_ceiling(max(counts))
        W, H, L, R, T, B = 640, 200, 40, 8, 12, 28
        pw, ph = W - L - R, H - T - B
        step = pw / n
        bw = min(34, step - 8)
        g = []
        for frac in (0, 0.5, 1):
            y = T + ph * (1 - frac)
            g.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="tick" x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{fmt_num(top * frac, 0)}</text>')
        rows = []
        for i, (w, c) in enumerate(zip(weeks, counts)):
            x = L + step * i + (step - bw) / 2
            h = ph * c / top
            y = T + ph - h
            tip = f"Week of {fmt_date(w)}: {c} items"
            if c:
                r = min(4, h)
                g.append(f'<path class="bar-m" d="M{x:.1f} {T + ph:.1f}V{y + r:.1f}Q{x:.1f} {y:.1f} {x + r:.1f} {y:.1f}H{x + bw - r:.1f}Q{x + bw:.1f} {y:.1f} {x + bw:.1f} {y + r:.1f}V{T + ph:.1f}Z"/>')
            g.append(f'<rect class="hit" x="{L + step * i:.1f}" y="{T}" width="{step:.1f}" height="{ph}" data-tip="{esc(tip)}" tabindex="0" role="img" aria-label="{esc(tip)}"/>')
            if i % 2 == (n - 1) % 2:
                g.append(f'<text class="tick" x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle">{esc(fmt_date(w, False))}</text>')
            rows.append(f"<tr><td>{esc(fmt_date(w))}</td><td>{c}</td></tr>")
        return (f'<figure class="chart"><figcaption>News items per week, '
                f'last {n} weeks</figcaption>'
                f'<div class="chart-box"><svg viewBox="0 0 {W} {H}" role="group" '
                f'aria-label="News items per week">{"".join(g)}</svg></div>'
                f'<details class="tableview"><summary>Show as table</summary>'
                f'<table><thead><tr><th>Week starting</th><th>Items</th></tr>'
                f'</thead><tbody>{"".join(rows)}</tbody></table></details>'
                f'</figure>')

    def chart_records(self, recs):
        pts = [r for r in recs if r["date"]]
        if len(pts) < 2:
            return ""
        d0 = min(p["date"] for p in pts)
        d1 = max(p["date"] for p in pts)
        span = max(1, (d1 - d0).days)
        lo = math.floor(min(p["eff"] for p in pts) / 5) * 5
        hi = math.ceil(max(p["eff"] for p in pts) / 5) * 5
        if hi == lo:
            hi += 5
        W, H, L, R, T, B = 720, 320, 44, 16, 14, 30
        pw, ph = W - L - R, H - T - B

        def X(d):
            return L + pw * (d - d0).days / span

        def Y(v):
            return T + ph * (1 - (v - lo) / (hi - lo))
        g = []
        v = lo
        while v <= hi:
            g.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                     f'<text class="tick" x="{L - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{v}%</text>')
            v += 5
        for k in range(5):
            d = d0 + datetime.timedelta(days=round(span * k / 4))
            anchor = "start" if k == 0 else ("end" if k == 4 else "middle")
            g.append(f'<text class="tick" x="{X(d):.1f}" y="{H - 8}" text-anchor="{anchor}">{esc(fmt_date(d, span > 300))}</text>')
        for p in sorted(pts, key=lambda p: CELL_ORDER.index(p["cell"])
                        if p["cell"] in CELL_ORDER else 9, reverse=True):
            cls = "c-" + slugify(p["cell"])
            x, y = X(p["date"]), Y(p["eff"])
            tip = (f'{fmt_num(p["eff"], 2)}% {CELL_LABEL.get(p["cell"], "")} '
                   f'· {p["evidence"]} · {fmt_date(p["date"])} '
                   f'· {clip(p["title"], 90)}')
            if p["cell"] == "tandem":
                mark = f'<rect class="pt {cls}" x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="1.5"/>'
            elif p["cell"] == "module":
                mark = f'<path class="pt {cls}" d="M{x:.1f} {y - 6.5:.1f}L{x + 6:.1f} {y + 4.5:.1f}H{x - 6:.1f}Z"/>'
            else:
                mark = f'<circle class="pt {cls}" cx="{x:.1f}" cy="{y:.1f}" r="5"/>'
            g.append(f'<g data-tip="{esc(tip)}" tabindex="0" role="img" aria-label="{esc(tip)}">'
                     f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12"/>{mark}</g>')
        present = [c for c in CELL_ORDER if any(p["cell"] == c for p in pts)]
        legend = "".join(
            f'<span class="key"><svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">'
            + ('<rect class="pt c-tandem" x="2" y="2" width="10" height="10" rx="1.5"/>' if c == "tandem"
               else '<path class="pt c-module" d="M7 1L13 12H1Z"/>' if c == "module"
               else f'<circle class="pt c-{slugify(c)}" cx="7" cy="7" r="5"/>')
            + f'</svg>{esc(CELL_LABEL[c])}</span>' for c in present)
        return (f'<figure class="chart"><figcaption>Efficiency values stated '
                f'in news items, by date of the item</figcaption>'
                f'<div class="legend">{legend}</div>'
                f'<div class="chart-box"><svg viewBox="0 0 {W} {H}" role="group" '
                f'aria-label="Reported efficiency by date">{"".join(g)}</svg></div>'
                f'</figure>')

    # ---- pages

    def records(self):
        recs = cluster_stories([it for it in self.items
                                if it["eff"] is not None])
        recs.sort(key=lambda x: (x["date"] or datetime.date.min), reverse=True)
        return recs

    def page_overview(self):
        end = self.today
        start = end - datetime.timedelta(days=6)
        win, stories = rank_window(self.items, start, end, self.focus,
                                   self.tracked)
        n_co = len({s for it in win for s in it["companies"]})
        recs = [s for s in stories if s["eff"] is not None]
        recs.sort(key=lambda x: x["eff"], reverse=True)
        funds = [s for s in stories if s["fund"]]
        funds.sort(key=lambda x: x["fund"], reverse=True)
        top = stories[:6]
        lead_html = "".join(
            self.story(st, i + 1, lead=(i == 0)) for i, st in enumerate(top))
        if not top:
            lead_html = ('<li class="empty">No dated items in the past seven '
                         'days. Run a fetch in PV Radar, or build with '
                         '<code>--fetch</code>.</li>')

        def side_list(rows, kind):
            if not rows:
                return '<p class="quiet">None stated this week.</p>'
            out = []
            for r in rows[:6]:
                val = (f'{fmt_num(r["eff"], 2)}%' if kind == "eff"
                       else fmt_musd(r["fund"]))
                sub = (self.chip_evidence(r["evidence"]) if kind == "eff"
                       else "")
                who = (esc(self.by_slug[r["companies"][0]]["name"])
                       if r["companies"] and r["companies"][0] in self.by_slug
                       else esc(r["source"]))
                out.append(f'<li><span class="val">{esc(val)}</span>'
                           f'<span class="what">{self.link(r)}'
                           f'<span class="meta">{who} {sub}</span></span></li>')
            return f'<ul class="vals">{"".join(out)}</ul>'

        counts = {}
        for it in win:
            for s in it["companies"]:
                counts[s] = counts.get(s, 0) + 1
        movers = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        movers_html = "".join(
            f'<li><a href="company/{esc(s)}.html">{esc(self.by_slug[s]["name"])}</a>'
            f'<span class="n">{n}</span></li>' for s, n in movers
            if s in self.by_slug) or '<li class="quiet">No directory names in this week’s headlines.</li>'
        papers_html = "".join(
            f'<li><a class="out" href="{esc(p["link"])}" rel="noopener noreferrer">{esc(p["title"])}</a>'
            f'<span class="meta">{esc(" · ".join(x for x in (p["journal"], p["authors"], fmt_date(p["date"], False)) if x))}</span></li>'
            for p in self.papers[:6])
        papers_block = (f'<section class="block"><div class="block-head"><h2>Latest papers</h2>'
                        f'<a class="more" href="papers.html">All {len(self.papers):,} papers</a></div>'
                        f'<ul class="plain">{papers_html}</ul></section>'
                        if self.papers else "")
        body = f"""<section class="hero"><div class="wrap">
<p class="eyebrow">{esc(fmt_range(start, end))}</p>
<h1>{esc(self.cfg["title"])}</h1>
<p class="lede">{esc(self.cfg["tagline"])}</p>
<form class="search" data-search role="search">
<label for="q" class="sr">Search news, papers and companies</label>
<input id="q" type="search" placeholder="Search news, papers and companies" autocomplete="off">
</form>
<div class="results" data-results hidden></div>
<dl class="counts">
<div><dt>News items this week</dt><dd>{len(win):,}</dd></div>
<div><dt>Distinct stories</dt><dd>{len(stories):,}</dd></div>
<div><dt>Efficiency results</dt><dd>{len(recs):,}</dd></div>
<div><dt>Companies in the news</dt><dd>{n_co:,}</dd></div>
<div><dt>In the store</dt><dd>{len(self.items):,} <span>news</span> {len(self.papers):,} <span>papers</span></dd></div>
</dl>
</div></section>
<div class="wrap cols">
<section class="block main-col">
<div class="block-head"><h2>Leading signals, past 7 days</h2><a class="more" href="brief.html">Full weekly brief</a></div>
<ol class="stories">{lead_html}</ol>
</section>
<aside class="side-col">
<section class="block"><div class="block-head"><h2>Efficiency results</h2><a class="more" href="records.html">All</a></div>{side_list(recs, "eff")}</section>
<section class="block"><div class="block-head"><h2>Funding</h2></div>{side_list(funds, "fund")}</section>
<section class="block"><div class="block-head"><h2>Most mentioned</h2><a class="more" href="companies.html">Directory</a></div><ul class="movers">{movers_html}</ul></section>
</aside>
</div>
<div class="wrap">
<section class="block">{self.chart_weeks()}</section>
{papers_block}
</div>"""
        return self.page("index.html", "", body, page_class="p-overview")

    def brief_body(self, start, end, root=""):
        win, stories = rank_window(self.items, start, end, self.focus,
                                   self.tracked)
        if not win:
            return '<p class="quiet">No dated items in this period.</p>', 0
        recs = sorted((s for s in stories if s["eff"] is not None),
                      key=lambda x: x["eff"], reverse=True)
        funds = sorted((s for s in stories if s["fund"]),
                       key=lambda x: x["fund"], reverse=True)
        total = sum(f["fund"] for f in funds)
        top = stories[:10]
        top_html = "".join(self.story(st, i + 1, root, lead=(i == 0))
                           for i, st in enumerate(top))
        by_sig = {}
        for it in win:
            by_sig[it["signal"]] = by_sig.get(it["signal"], 0) + 1
        mix = "".join(f'<li>{self.chip_signal(s)}<span class="n">{by_sig[s]}</span></li>'
                      for s in SIGNALS if s in by_sig)
        rec_rows = "".join(
            f'<tr><td class="num">{fmt_num(r["eff"], 2)}%</td>'
            f'<td>{esc(CELL_LABEL.get(r["cell"], ""))}</td>'
            f'<td>{self.chip_evidence(r["evidence"])}</td>'
            f'<td>{self.link(r)}<span class="meta">{esc(r["source"])} · {esc(fmt_date(r["date"], False))}</span></td></tr>'
            for r in recs[:15])
        rec_block = (f'<section class="block"><h2>Efficiency results</h2>'
                     f'<div class="scroll"><table class="data"><thead><tr><th>PCE</th><th>Device</th><th>Evidence</th><th>Item</th></tr></thead>'
                     f'<tbody>{rec_rows}</tbody></table></div></section>'
                     if recs else "")
        fund_rows = "".join(
            f'<tr><td class="num">{esc(fmt_musd(f["fund"]))}</td>'
            f'<td>{self.link(f)}<span class="meta">{esc(f["source"])} · {esc(fmt_date(f["date"], False))}</span></td></tr>'
            for f in funds[:15])
        fund_block = (f'<section class="block"><h2>Funding and investment</h2>'
                      f'<p class="quiet">Sum of the amounts stated: about {esc(fmt_musd(total))}. '
                      f'Other currencies are converted at fixed approximate rates.</p>'
                      f'<div class="scroll"><table class="data"><thead><tr><th>Amount</th><th>Item</th></tr></thead>'
                      f'<tbody>{fund_rows}</tbody></table></div></section>'
                      if funds else "")
        co = {}
        for it in win:
            for s in it["companies"]:
                co.setdefault(s, []).append(it)
        co_html = "".join(
            f'<details class="co-week"><summary><a href="{root}company/{esc(s)}.html">{esc(self.by_slug[s]["name"])}</a>'
            f'<span class="n">{len(rows)}</span></summary><ul class="plain">'
            + "".join(f'<li>{self.link(r)}<span class="meta">{esc(r["signal"])} · {esc(fmt_date(r["date"], False))}</span></li>'
                      for r in rows[:5])
            + "</ul></details>"
            for s, rows in sorted(co.items(), key=lambda kv: -len(kv[1]))[:12]
            if s in self.by_slug)
        co_block = (f'<section class="block"><h2>Companies this week</h2>{co_html}</section>'
                    if co_html else "")
        body = f"""<dl class="counts">
<div><dt>News items</dt><dd>{len(win):,}</dd></div>
<div><dt>Distinct stories</dt><dd>{len(stories):,}</dd></div>
<div><dt>Efficiency results</dt><dd>{len(recs):,}</dd></div>
<div><dt>Funding stated</dt><dd>{esc(fmt_musd(total)) if funds else "none"}</dd></div>
</dl>
<ul class="mix">{mix}</ul>
<section class="block"><h2>Top ten</h2><ol class="stories">{top_html}</ol></section>
{rec_block}
{fund_block}
{co_block}"""
        return body, len(win)

    def weeks_available(self, limit=26):
        dated = [it["date"] for it in self.items if it["date"]]
        if not dated:
            return []
        first = week_start(min(dated))
        w = week_start(self.today) - datetime.timedelta(weeks=1)
        out = []
        while w >= first and len(out) < limit:
            w_end = w + datetime.timedelta(days=6)
            if any(w <= d <= w_end for d in dated):
                out.append(w)
            w -= datetime.timedelta(weeks=1)
        return out

    def pages_brief(self):
        pages = {}
        weeks = self.weeks_available()
        end = self.today
        start = end - datetime.timedelta(days=6)

        def archive_list(root):
            if not weeks:
                return ""
            return ('<nav class="weeks" aria-label="Past weeks"><h2>Past weeks</h2><ul>'
                    + "".join(f'<li><a href="{root}brief/{week_id(w)}.html">'
                              f'{esc(fmt_range(w, w + datetime.timedelta(days=6)))}</a></li>'
                              for w in weeks) + "</ul></nav>")
        body, _ = self.brief_body(start, end)
        pages["brief.html"] = self.page(
            "brief.html", "Weekly brief",
            f'<div class="wrap narrow"><p class="eyebrow">Weekly brief · past 7 days</p>'
            f'<h1>{esc(fmt_range(start, end))}</h1>{body}{archive_list("")}</div>',
            f"The week in photovoltaics, {fmt_range(start, end)}.")
        for w in weeks:
            w_end = w + datetime.timedelta(days=6)
            body, _ = self.brief_body(w, w_end, "../")
            path = f"brief/{week_id(w)}.html"
            pages[path] = self.page(
                path, f"Week {week_id(w)}",
                f'<div class="wrap narrow"><p class="eyebrow">Weekly brief · {esc(week_id(w))}</p>'
                f'<h1>{esc(fmt_range(w, w_end))}</h1>{body}{archive_list("../")}</div>',
                f"The week in photovoltaics, {fmt_range(w, w_end)}.")
        return pages

    def finder_page(self, path, title, lede, data_file, kind, placeholder,
                    facets, rows_html):
        facet_html = "".join(
            f'<div class="facet" data-facet="{esc(key)}"><span class="facet-name">{esc(label)}</span></div>'
            for key, label in facets)
        body = f"""<div class="wrap narrow">
<h1>{esc(title)}</h1>
<p class="lede">{lede}</p>
<div class="finder" data-finder data-src="data/{esc(data_file)}" data-kind="{esc(kind)}">
<label class="sr" for="find">{esc(placeholder)}</label>
<input id="find" type="search" placeholder="{esc(placeholder)}" autocomplete="off">
{facet_html}
<p class="count" data-count aria-live="polite"></p>
<ul class="entries" data-list>{rows_html}</ul>
<button class="btn" type="button" data-more hidden>Show more</button>
</div>
</div>"""
        return self.page(path, title, body)

    def page_news(self):
        rows = "".join(
            f'<li class="entry"><div class="tags">{self.chip_signal(it["signal"])}{self.facts(it)}</div>'
            f'{self.link(it)}<span class="meta">{esc(" · ".join(x for x in (it["source"], fmt_date(it["date"])) if x))}</span></li>'
            for it in self.items[:40])
        return self.finder_page(
            "news.html", "News",
            f"{len(self.items):,} photovoltaics news items, newest first.",
            "news.json", "news", "Search headlines, sources and companies",
            [("g", "Signal"), ("tp", "Topic")], rows)

    def page_papers(self):
        rows = "".join(
            f'<li class="entry"><a class="out" href="{esc(p["link"])}" rel="noopener noreferrer">{esc(p["title"])}</a>'
            f'<span class="meta">{esc(" · ".join(x for x in (p["journal"], p["authors"], fmt_date(p["date"])) if x))}</span></li>'
            for p in self.papers[:40])
        if not self.papers:
            rows = ('<li class="empty">No papers in the store yet. Build with '
                    '<code>--fetch</code> to pull them from OpenAlex.</li>')
        return self.finder_page(
            "papers.html", "Papers",
            f"{len(self.papers):,} journal articles and preprints with "
            f"photovoltaic terms in the title, newest first. "
            f"Source: <a class=\"out\" href=\"https://openalex.org\" rel=\"noopener noreferrer\">OpenAlex</a>.",
            "papers.json", "papers", "Search titles, journals and authors",
            [("j", "Journal"), ("tp", "Topic")], rows)

    def page_records(self):
        recs = self.records()
        best = []
        measured = [r for r in recs if r["evidence"] != "Simulated"]
        for c in CELL_ORDER[:3]:
            rows = [r for r in measured if r["cell"] == c]
            if not rows:
                continue
            top = max(rows, key=lambda r: (r["eff"],
                                           EVIDENCE_RANK[r["evidence"]]))
            cert = [r for r in rows if r["evidence"] == "Certified"]
            cbest = max(cert, key=lambda r: r["eff"]) if cert else None
            best.append(
                f'<div class="best"><p class="k">{esc(CELL_LABEL[c])}</p>'
                f'<p class="v">{fmt_num(top["eff"], 2)}%</p>'
                f'<p class="meta">Highest value in this store · {esc(top["evidence"].lower())} · {esc(fmt_date(top["date"]))}</p>'
                + (f'<p class="meta">Highest certified: {fmt_num(cbest["eff"], 2)}% · {esc(fmt_date(cbest["date"]))}</p>'
                   if cbest and cbest["eff"] < top["eff"] else "")
                + "</div>")
        rows_html = "".join(
            f'<tr data-cell="{esc(r["cell"])}"><td class="num">{fmt_num(r["eff"], 2)}%</td>'
            f'<td>{esc(CELL_LABEL.get(r["cell"], ""))}</td>'
            f'<td>{self.chip_evidence(r["evidence"])}</td>'
            f'<td class="nowrap">{esc(fmt_date(r["date"]))}</td>'
            f'<td>{self.link(r)}<span class="meta">{esc(" · ".join(x for x in (_outlets(r), ", ".join(self.by_slug[s]["name"] for s in r["companies"] if s in self.by_slug)) if x))}</span></td></tr>'
            for r in recs[:400])
        table = (f'<div class="scroll"><table class="data"><thead><tr><th>PCE</th><th>Device</th><th>Evidence</th><th>Date</th><th>Item</th></tr></thead>'
                 f'<tbody>{rows_html}</tbody></table></div>'
                 if recs else '<p class="quiet">No item in the store states an efficiency yet.</p>')
        body = f"""<div class="wrap narrow">
<h1>Efficiency results</h1>
<p class="lede">{len(recs):,} power-conversion efficiencies stated in news items, each with its source and how it is backed. Simulated values are listed but left out of the summary and the chart.</p>
<div class="bests">{"".join(best)}</div>
{self.chart_records(measured)}
<aside class="note"><p><strong>Read these as reported values, not as a record chart.</strong> Device area, measurement protocol (stabilised or scan) and architecture differ between items, and the date is that of the news item. For confirmed records use the <a class="out" href="https://onlinelibrary.wiley.com/journal/1099159x" rel="noopener noreferrer">Solar cell efficiency tables</a> in <cite>Progress in Photovoltaics</cite> and the <a class="out" href="https://www.nrel.gov/pv/cell-efficiency.html" rel="noopener noreferrer">research-cell efficiency chart</a>.</p></aside>
{table}
</div>"""
        return self.page("records.html", "Efficiency results", body)

    def page_companies(self):
        listed = [c for c in self.directory
                  if c["tracked"] or self.co_items.get(c["slug"])]
        listed.sort(key=lambda c: c["name"].lower())
        cards = "".join(
            f'<li class="co" data-text="{esc((c["name"] + " " + c["country"] + " " + " ".join(c["tech"]) + " " + c["kind"]).lower())}">'
            f'<a href="company/{esc(c["slug"])}.html">{esc(c["name"])}</a>'
            f'<span class="meta">{esc(" · ".join(x for x in (c["country"], c["stage"], "" if c["kind"] == "Company" else c["kind"]) if x))}</span>'
            f'<span class="tech">{"".join(f"<span>{esc(t)}</span>" for t in c["tech"] if t != "Research institute")}</span>'
            f'<span class="n">{len(self.co_items.get(c["slug"], []))} news</span></li>'
            for c in listed)
        body = f"""<div class="wrap">
<h1>Companies and institutes</h1>
<p class="lede">{len(listed):,} organisations, each with its news, events and stated efficiency results.</p>
<div class="finder" data-filter>
<label class="sr" for="cofind">Filter by name, country or technology</label>
<input id="cofind" type="search" placeholder="Filter by name, country or technology" autocomplete="off">
<p class="count" data-count aria-live="polite"></p>
<ul class="cos" data-list>{cards}</ul>
</div></div>"""
        return self.page("companies.html", "Companies", body)

    def page_company(self, c):
        rows = self.co_items.get(c["slug"], [])
        events = [r for r in rows if r["signal"] in
                  ("Record", "Funding", "Deal", "Manufacturing", "Policy",
                   "Patent")][:12]
        recs = sorted((r for r in rows if r["eff"] is not None),
                      key=lambda r: (r["date"] or datetime.date.min),
                      reverse=True)[:20]
        facts = " · ".join(x for x in (c["country"], c["stage"],
                                            c["kind"] if c["kind"] != "Company" else "") if x)
        site = safe_url(c["website"])
        site_html = (f'<p><a class="out" href="{esc(site)}" rel="noopener noreferrer">{esc(host_of(site))}</a></p>'
                     if site else "")
        tech = "".join(f"<span>{esc(t)}</span>" for t in c["tech"])
        news_html = "".join(
            f'<li>{self.link(r)}<span class="meta">{esc(" · ".join(x for x in (r["source"], fmt_date(r["date"])) if x))}</span></li>'
            for r in rows[:15]) or '<li class="quiet">No news items in the store name this organisation yet.</li>'
        ev_html = "".join(
            f'<li><div class="tags">{self.chip_signal(r["signal"])}{self.facts(r)}</div>{self.link(r)}'
            f'<span class="meta">{esc(" · ".join(x for x in (r["source"], fmt_date(r["date"])) if x))}</span></li>'
            for r in events)
        rec_html = "".join(
            f'<tr><td class="num">{fmt_num(r["eff"], 2)}%</td><td>{esc(CELL_LABEL.get(r["cell"], ""))}</td>'
            f'<td>{self.chip_evidence(r["evidence"])}</td><td class="nowrap">{esc(fmt_date(r["date"]))}</td>'
            f'<td>{self.link(r)}</td></tr>' for r in recs)
        untracked = ("" if c["tracked"] else
                     '<p class="quiet">Added automatically because PV Radar named this organisation in several items.</p>')
        body = f"""<div class="wrap narrow">
<p class="eyebrow"><a href="../companies.html">Companies and institutes</a></p>
<h1>{esc(c["name"])}</h1>
<p class="lede">{esc(facts)}</p>
<p class="tech">{tech}</p>
{site_html}
{f'<p>{esc(c["note"])}</p>' if c["note"] else ""}
{untracked}
{f'<section class="block"><h2>Events</h2><ul class="plain">{ev_html}</ul></section>' if events else ""}
{f'<section class="block"><h2>Stated efficiency results</h2><div class="scroll"><table class="data"><thead><tr><th>PCE</th><th>Device</th><th>Evidence</th><th>Date</th><th>Item</th></tr></thead><tbody>{rec_html}</tbody></table></div></section>' if recs else ""}
<section class="block"><h2>Latest news <span class="n">{len(rows)}</span></h2><ul class="plain">{news_html}</ul></section>
</div>"""
        return self.page(f"company/{c['slug']}.html", c["name"], body,
                         f"{c['name']}: news and stated efficiency results.")

    def page_primer(self):
        layers = [
            ("Front grid", "Ag", "metal", 14),
            ("Transparent electrode", "IZO", "tco", 16),
            ("Buffer", "SnOₓ (ALD)", "buffer", 12),
            ("Electron transport", "C₆₀", "etl", 14),
            ("Passivation", "LiF or PDAI₂", "pass", 9),
            ("Perovskite absorber", "wide gap, ~1.68 eV", "pero", 62),
            ("Hole contact", "carbazole SAM", "htl", 9),
            ("Recombination junction", "ITO or nc-Si:H", "rec", 14),
            ("Silicon bottom cell", "heterojunction or TOPCon, 1.12 eV", "si", 104),
            ("Rear electrode", "TCO / Ag", "metal", 16),
        ]
        y, rects = 16, []
        for name, mat, cls, h in layers:
            rects.append(
                f'<rect class="ly ly-{cls}" x="20" y="{y}" width="250" height="{h}" rx="2"/>'
                f'<line class="lead" x1="270" x2="292" y1="{y + h / 2}" y2="{y + h / 2}"/>'
                f'<text class="ltxt" x="298" y="{y + h / 2 + 4}">{esc(name)}'
                f'<tspan class="lmat" dx="8">{esc(mat)}</tspan></text>')
            y += h + 3
        stack = (f'<figure class="chart"><div class="chart-box"><svg viewBox="0 0 640 {y + 12}" role="img" '
                 f'aria-label="Layer stack of a monolithic perovskite-silicon tandem cell, light entering from the top">'
                 f'<text class="ltxt" x="20" y="10">Light enters here ↓</text>{"".join(rects)}</svg></div>'
                 f'<figcaption>Typical p-i-n perovskite top cell on a silicon bottom cell (two-terminal). Schematic; layer thicknesses are not to scale.</figcaption></figure>')
        body = f"""<div class="wrap narrow prose">
<p class="eyebrow">Primer</p>
<h1>How to read a tandem result</h1>
<p class="lede">Most headlines on this site report one number, the power-conversion efficiency. This page explains the device behind that number and what makes a reported value trustworthy.</p>
<section class="block"><h2>The device</h2>
<p>A silicon cell extracts at most its band-gap energy of 1.12&nbsp;eV from each absorbed photon; the excess energy of blue and green photons is lost as heat. A tandem adds a wide-gap perovskite cell on top, which absorbs the high-energy part of the spectrum at a higher voltage and passes the near-infrared to the silicon below.</p>
{stack}
<p>In the two-terminal (monolithic) design the two cells are connected in series through a recombination junction. Their voltages add and the same current flows through both, so the cell with the lower photocurrent limits the device. This is why the perovskite band gap and thickness are tuned for current matching. In a four-terminal tandem the two cells are contacted separately and need no current matching, at the cost of extra electrodes and optics.</p></section>
<section class="block"><h2>The numbers</h2>
<dl class="defs">
<div><dt>PCE</dt><dd>Power-conversion efficiency: electrical power out divided by light power in, under the AM1.5G spectrum at 1000&nbsp;W&nbsp;m⁻² and 25&nbsp;°C. PCE = V<sub>oc</sub> × J<sub>sc</sub> × FF / P<sub>in</sub>.</dd></div>
<div><dt>V<sub>oc</sub>, J<sub>sc</sub>, FF</dt><dd>Open-circuit voltage, short-circuit current density and fill factor. A result that states all three with the area can be checked; a bare percentage cannot.</dd></div>
<div><dt>Area</dt><dd>Records on 1&nbsp;cm² cells, on full wafers and on modules are different achievements. Compare like with like, and note whether the area is the aperture or the designated illumination area.</dd></div>
<div><dt>Stabilised</dt><dd>Perovskite cells show hysteresis, so a single voltage scan can overstate performance. A stabilised value comes from maximum-power-point tracking over minutes.</dd></div>
<div><dt>T80</dt><dd>Operating time until the efficiency falls to 80% of its initial value, reported with the stress protocol, for example the ISOS light, heat and humidity conditions or the IEC&nbsp;61215 module tests.</dd></div>
</dl></section>
<section class="block"><h2>How results are graded here</h2>
<ul class="grades">
<li>{self.chip_evidence("Certified")} {esc(EVIDENCE_HELP["Certified"])}</li>
<li>{self.chip_evidence("Peer-reviewed")} {esc(EVIDENCE_HELP["Peer-reviewed"])}</li>
<li>{self.chip_evidence("Reported")} {esc(EVIDENCE_HELP["Reported"])}</li>
<li>{self.chip_evidence("Simulated")} {esc(EVIDENCE_HELP["Simulated"])}</li>
</ul>
<p>Further reading: M.&nbsp;A.&nbsp;Green et&nbsp;al., <a class="out" href="https://onlinelibrary.wiley.com/journal/1099159x" rel="noopener noreferrer">Solar cell efficiency tables</a>, <cite>Progress in Photovoltaics</cite>; M.&nbsp;V.&nbsp;Khenkin et&nbsp;al., <a class="out" href="https://doi.org/10.1038/s41560-019-0529-5" rel="noopener noreferrer">Consensus statement for stability assessment and reporting for perovskite photovoltaics based on ISOS procedures</a>, <cite>Nature Energy</cite> 5, 35 (2020).</p></section>
</div>"""
        return self.page("primer.html", "Primer", body,
                         "How to read a perovskite-silicon tandem result.")

    def page_about(self):
        cfg = self.cfg
        who = ""
        if cfg.get("owner") or cfg.get("about"):
            links = "".join(
                f'<li><a class="out" href="{esc(safe_url(l.get("url")))}" rel="noopener noreferrer">{esc(l.get("label") or host_of(l.get("url", "")))}</a></li>'
                for l in (cfg.get("links") or [])
                if isinstance(l, dict) and safe_url(l.get("url")))
            who = (f'<section class="block"><h2>{esc(cfg.get("owner") or "Who runs this")}</h2>'
                   + (f'<p class="quiet">{esc(cfg["role"])}</p>' if cfg.get("role") else "")
                   + (f'<p>{esc(cfg["about"])}</p>' if cfg.get("about") else "")
                   + (f'<p>Contact: <span class="select">{esc(cfg["contact"])}</span></p>' if cfg.get("contact") else "")
                   + (f'<ul class="plain">{links}</ul>' if links else "")
                   + "</section>")
        n_an = sum(1 for it in self.items if it["analysed"])
        weights = "".join(f"<tr><td>{self.chip_signal(s)}</td><td class=\"num\">{SIGNAL_WEIGHT[s]:g}</td></tr>"
                          for s in SIGNALS)
        focus = ", ".join(self.focus) or "none"
        body = f"""<div class="wrap narrow prose">
<h1>About this site</h1>
<p class="lede">{esc(cfg["title"])} collects photovoltaics news and journal papers, extracts the figures they state, and ranks each week by a fixed, published formula.</p>
{who}
<section class="block" id="method"><h2>Method</h2>
<h3>Sources</h3>
<p>News comes from saved Google News searches and the RSS feeds of PV trade publications. Papers come from <a class="out" href="https://openalex.org" rel="noopener noreferrer">OpenAlex</a>, selected by photovoltaic terms in the title. Every headline links to its publisher and every paper to its DOI.</p>
<h3>Extraction</h3>
<p>{n_an:,} of the {len(self.items):,} news items were analysed by a language model in PV Radar, which reads the headline and summary and returns the stated efficiency, funding amount, organisation, country and category. The remaining items were processed by keyword rules that accept an efficiency only when an efficiency term stands next to the percentage, and a funding amount only when the item uses funding language. Both methods read the headline and summary only, so a figure given further down an article is missed.</p>
<h3>Evidence grades</h3>
<ul class="grades">
<li>{self.chip_evidence("Certified")} {esc(EVIDENCE_HELP["Certified"])}</li>
<li>{self.chip_evidence("Peer-reviewed")} {esc(EVIDENCE_HELP["Peer-reviewed"])}</li>
<li>{self.chip_evidence("Reported")} {esc(EVIDENCE_HELP["Reported"])}</li>
<li>{self.chip_evidence("Simulated")} {esc(EVIDENCE_HELP["Simulated"])}</li>
</ul>
<h3>Ranking</h3>
<p>Reports of the same story are grouped when, within four days, their headlines share at least 60% of their words or they state the same efficiency for the same organisation. A group carries the strongest evidence grade among its reports. Each story then receives a score:</p>
<div class="scroll"><table class="data compact"><thead><tr><th>Signal</th><th>Base</th></tr></thead><tbody>{weights}</tbody></table></div>
<ul>
<li>+2 if certified, +1 if peer-reviewed, −1 if simulated.</li>
<li>+ log<sub>10</sub>(1 + funding in US$&nbsp;M) / 1.5, capped at 2.</li>
<li>+0.75 for each additional outlet reporting the story, capped at 2.</li>
<li>+1.5 if the story is on a focus topic ({esc(focus)}).</li>
<li>+0.5 if it names an organisation in the directory.</li>
</ul>
<h3>Limits</h3>
<ul>
<li>Extraction errors occur. Check the linked source before citing a figure.</li>
<li>Funding in other currencies is converted at fixed approximate rates and is indicative only.</li>
<li>The efficiency list mixes device areas and measurement protocols; it is a log of claims, not a record table.</li>
<li>Coverage follows the saved searches and feeds, which are English-language and weighted towards perovskite and tandem technology.</li>
</ul></section>
<p class="quiet">Built {esc(fmt_date(self.built.date()))}, {self.built.strftime('%H:%M')} UTC with GrapheAI PV Radar Site.</p>
</div>"""
        return self.page("about.html", "About", body)

    # ---- data files for the browser

    def data_news(self):
        return [{"t": it["title"], "u": it["link"], "s": it["source"],
                 "d": it["date"].isoformat() if it["date"] else "",
                 "g": it["signal"], "tp": it["topics"],
                 "c": [self.by_slug[s]["name"] for s in it["companies"]
                       if s in self.by_slug],
                 **({"e": it["eff"], "ct": CELL_LABEL.get(it["cell"], "")}
                    if it["eff"] is not None else {}),
                 **({"f": fmt_musd(it["fund"])} if it["fund"] else {})}
                for it in self.items]

    def data_papers(self):
        return [{"t": p["title"], "u": p["link"], "j": p["journal"],
                 "d": p["date"].isoformat(), "a": p["authors"],
                 "tp": p["topics"]} for p in self.papers]

    def data_companies(self):
        return [{"t": c["name"], "u": f"company/{c['slug']}.html",
                 "s": " · ".join(x for x in (c["country"],
                                                  ", ".join(c["tech"])) if x)}
                for c in self.directory
                if c["tracked"] or self.co_items.get(c["slug"])]


def _outlets(st, shown=3):
    o = st["outlets"] or ([st["source"]] if st["source"] else [])
    more = f" + {len(o) - shown} more" if len(o) > shown else ""
    return ", ".join(o[:shown]) + more


def _nice_ceiling(v):
    if v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 4, 5, 10):
        if v <= m * mag:
            return int(m * mag) if m * mag >= 2 else 2
    return int(10 * mag)


# ------------------------------------------------------------------------- build


def build(data_dir, out_dir, today=None, built=None, log=print):
    """Read the stores under data_dir and write the whole site to out_dir."""
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    today = today or datetime.date.today()
    built = built or datetime.datetime.now(datetime.timezone.utc)
    cfg = load_site_config(data_dir)
    radar_rows = load_json(data_dir / "pv_radar.json", [])
    site_rows = load_json(data_dir / "pv_site_news.json", [])
    paper_rows = load_json(data_dir / "pv_site_papers.json", [])
    # PV Radar's analysed rows come first so they win over rule-read copies.
    news_rows = merge_rows(radar_rows, site_rows)
    directory = build_directory(load_json(data_dir / "pv_companies.json", []))
    matcher = CompanyMatcher(directory)
    items = enrich_news(news_rows, matcher)
    items = [it for it in items if not it["date"] or it["date"] <= today]
    directory = add_untracked_companies(items, directory)
    papers = enrich_papers(paper_rows, today)
    site = Site(cfg, items, papers, directory, today, built)

    pages = {"index.html": site.page_overview(), "news.html": site.page_news(),
             "papers.html": site.page_papers(),
             "records.html": site.page_records(),
             "companies.html": site.page_companies(),
             "primer.html": site.page_primer(),
             "about.html": site.page_about()}
    pages.update(site.pages_brief())
    for c in directory:
        if c["tracked"] or site.co_items.get(c["slug"]):
            pages[f"company/{c['slug']}.html"] = site.page_company(c)

    if out_dir.exists():
        for child in out_dir.iterdir():        # keep the folder, drop its content
            if child.name.startswith(".git"):
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    for path, text in pages.items():
        p = out_dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    for name in ("style.css", "app.js"):
        shutil.copyfile(ASSETS_DIR / name, out_dir / "assets" / name)
    save_json(out_dir / "data" / "news.json", site.data_news())
    save_json(out_dir / "data" / "papers.json", site.data_papers())
    save_json(out_dir / "data" / "companies.json", site.data_companies())
    # The site's own fetched rows, republished so that a hosted build can
    # read its history back (--seed-url). PV Radar's store is not copied.
    save_json(out_dir / "data" / "store_news.json", site_rows)
    save_json(out_dir / "data" / "store_papers.json", paper_rows)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    base = (cfg.get("base_url") or "").rstrip("/")
    if safe_url(base):
        urls = "".join(f"<url><loc>{esc(base)}/{esc(p)}</loc></url>"
                       for p in sorted(pages))
        (out_dir / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.'
            f'sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
            encoding="utf-8")
    log(f"Built {len(pages)} pages from {len(items)} news items, "
        f"{len(papers)} papers, {len(directory)} organisations -> {out_dir}")
    return {"pages": len(pages), "news": len(items), "papers": len(papers),
            "organisations": len(directory)}


def serve(out_dir, port, open_browser=False):
    import functools
    import http.server
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(out_dir))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        print(f"Serving {out_dir} at http://127.0.0.1:{port}/  (Ctrl+C stops)")
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the PV Radar static website.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the site")
    b.add_argument("--data-dir", default="answers",
                   help="folder with pv_radar.json etc. (default: answers)")
    b.add_argument("--out", default="site", help="output folder (default: site)")
    b.add_argument("--fetch", action="store_true",
                   help="fetch headlines and papers before building")
    b.add_argument("--seed-url", default="",
                   help="URL of the deployed site, to carry its history "
                        "into a hosted build")
    b.add_argument("--paper-days", type=int, default=30,
                   help="how far back --fetch asks OpenAlex (default: 30)")
    b.add_argument("--today", default="",
                   help="build as of this date, YYYY-MM-DD (default: today)")
    b.add_argument("--serve", action="store_true",
                   help="serve the result locally after building")
    b.add_argument("--open", action="store_true",
                   help="with --serve, open the site in the browser")
    b.add_argument("--port", type=int, default=8520)
    a = ap.parse_args(argv)
    today = parse_date(a.today) or datetime.date.today()
    if a.fetch:
        n, p = refresh(a.data_dir, today, a.seed_url, a.paper_days)
        print(f"Site store: {n} headlines, {p} papers")
    build(a.data_dir, a.out, today)
    if a.serve:
        serve(a.out, a.port, a.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
