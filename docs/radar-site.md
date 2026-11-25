# PV Radar Site

`grapheai/radar_site.py` turns the PV Radar news store into a static public
website: plain HTML, one stylesheet, one script, no framework, no tracker,
no external request. It needs no API key and no package beyond the Python
standard library (tested on Python 3.11 and 3.13).

## Pages

| Page | Content |
|---|---|
| Overview | The week's leading signals, stated efficiency results, funding, most-mentioned organisations, items per week, latest papers, one search box across everything |
| Weekly brief | Top ten stories of the past 7 days, efficiency and funding tables, companies of the week; one archived page per ISO week |
| News | Every item, with search and filters by signal class and topic |
| Papers | Journal articles and preprints from OpenAlex, with search and filters by journal and topic |
| Efficiency | Every stated power-conversion efficiency with device type, evidence grade, date and source; chart of measured values |
| Companies | Directory of companies and institutes, one page each: events, stated results, news |
| Primer | The monolithic perovskite-silicon tandem stack and how to read a reported result |
| About | Who runs the site, and the full method: sources, extraction, evidence grades, ranking formula, limits |

## Build

```bash
cd ~/PaperRag
python radar_site.py build --fetch --serve --open
```

or double-click `RadarSite.command`. The site is written to `site/`.

| Option | Effect |
|---|---|
| `--data-dir DIR` | Folder holding the stores (default `answers`) |
| `--out DIR` | Output folder (default `site`); its previous content is replaced |
| `--fetch` | Fetch headlines and papers before building |
| `--paper-days N` | How far back `--fetch` asks OpenAlex (default 30) |
| `--serve`, `--open`, `--port` | Preview at `http://127.0.0.1:8520/` |
| `--today YYYY-MM-DD` | Build as of a given date |
| `--seed-url URL` | Read the history back from a deployed copy (hosted builds) |

## What it reads and writes

Read from the data folder, all optional:

- `pv_radar.json`: PV Radar's store. Rows analysed there keep their
  model-extracted efficiency, funding, organisation, country and category.
- `pv_watchlists.json`: PV Radar's saved searches and feeds, reused by `--fetch`.
- `pv_companies.json`: your additions to the built-in directory.
- `pv_site.json`: site settings.

Written by `--fetch` only: `pv_site_news.json` and `pv_site_papers.json`.
PV Radar's own store is never modified, so headlines fetched here remain
available for model analysis in PV Radar later; once analysed there, the
analysed copy takes precedence.

Not read and never published: `pv_benchmark.json`, saved reports, the
spend file, and anything else in `answers/`. The public data files contain
headlines, links, sources, dates and the extracted signals only.

### `pv_site.json`

```json
{
  "title": "PV Radar",
  "tagline": "Photovoltaics news, papers and efficiency results, ranked every week.",
  "owner": "Your name",
  "role": "Your role and institution",
  "about": "One paragraph on who runs the site and why.",
  "contact": "name [at] example [dot] org",
  "links": [{"label": "Group website", "url": "https://example.org"}],
  "base_url": "https://example.org/radar",
  "focus_topics": ["Perovskite", "Tandem"]
}
```

`focus_topics` raises the rank of stories on those topics. Available
topics: Perovskite, Tandem, Silicon, Thin film, Organic PV, Stability,
Storage, Utility-scale, Rooftop, Grid, Inverters, Hydrogen, Recycling.

### `pv_companies.json`

```json
[
  {"name": "My Startup", "country": "Germany", "kind": "Company",
   "stage": "Pilot", "tech": ["Perovskite-silicon tandem"],
   "aliases": ["MyStartup GmbH"], "website": "https://example.org",
   "note": "One sentence shown on the company page."},
  {"name": "Oxford PV", "stage": "Commercial"}
]
```

A row with the name of a built-in entry overrides the fields it gives.
Organisations that PV Radar names in two or more items are added
automatically.

## Extraction without a model

Items that PV Radar has not analysed are read by rules written to refuse
rather than guess:

- An **efficiency** is taken only when a percentage between 3 and 50 stands
  within 60 characters of "efficiency", "PCE" or "conversion", and is not
  phrased as a change ("by 12%", "falls 8%") or a retention ("retains 95%").
- A **funding amount** is taken only when the item uses funding language;
  of several amounts, the one nearest that wording is used. Other
  currencies are converted at fixed approximate rates.
- The **evidence grade** is Simulated, Certified, Peer-reviewed or Reported,
  judged from the item's own words. Simulated values are never ranked as
  records and are excluded from the efficiency chart.

Both the model and the rules read only the headline and the feed summary.
`tests/test_radar_site.py` pins the accepted and refused cases.

## Publishing

`site/` is self-contained and uses relative links, so it works from any
folder of any static host.

- **GitHub Pages**: `.github/workflows/pv-radar-site.yml` fetches, builds
  and deploys. It runs only when started by hand until its `schedule` block
  is uncommented. Enable Pages first (Settings > Pages > Source: GitHub
  Actions). Site settings for hosted builds go in
  `grapheai/radar_site_config/` (`pv_site.json`, `pv_companies.json`,
  `pv_watchlists.json`). The hosted build has no access to your local
  `pv_radar.json`; it uses the rule-based extraction throughout. This
  workflow has not yet been run.
- **From your Mac** (keeps the model-analysed store): build locally, then
  upload `site/`, for example with `npx wrangler pages deploy site` for
  Cloudflare Pages, or by copying the folder to a web server. Adding the
  build to the daily `com.grapheai.pvradar` job keeps it current.

The site republishes third-party headlines with links to their publishers
and, in the ranked lists, a short excerpt of the feed summary.
