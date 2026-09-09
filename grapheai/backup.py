"""
PaperRag auto-backup: one dated zip of everything irreplaceable.

What goes in: the whole answers/ folder (saved answers, extractions,
digests, the PV Radar store, watchlists, benchmark, materials library,
analytics datasets, PeroDeg data, projects, spend tracking, journal
cache), watch topics/seen files, config.py, all app and job files, and
.streamlit/.

What stays out (big and rebuildable/re-copyable): papers/, chroma_db/,
figures/, __pycache__.

Destination: your iCloud Drive (so it leaves the machine) if iCloud is
enabled, else ~/PaperRagBackups. Keeps the newest 14 backups, deletes
older ones.

Manual run:    python backup.py
Scheduled:     via launchd - runs daily.
"""

import datetime
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
KEEP = 14

ICLOUD = (Path.home() / "Library" / "Mobile Documents"
          / "com~apple~CloudDocs")
DEST_DIR = (ICLOUD / "PaperRagBackups" if ICLOUD.exists()
            else Path.home() / "PaperRagBackups")

INCLUDE_DIRS = ["answers", ".streamlit"]
INCLUDE_FILES = ["config.py", "workbench.py", "pvradar.py",
                 "materials.py", "perodeg.py", "analytics.py",
                 "fundradar.py", "slides.py", "technoecon.py",
                 "expplan.py", "hub.py", "impact.py", "venture.py",
                 "radar_refresh.py", "analytics_refresh.py",
                 "backup.py", "ask_rules.py",
                 "ingest.py", "get_papers.py", "zotero_link.py",
                 "workbench_downloader.py", "watch.py",
                 "watch_topics.txt", "watch_seen.txt",
                 "requirements.txt", "golden_runs.py", "GoldenRuns.command", "CodexLogin.command",
                 "Workbench.command", "PVRadar.command",
                 "MaterialLibrary.command", "PeroDeg.command",
                 "Analytics.command", "IndexPapers.command",
                 "FundingRadar.command", "SlideStudio.command",
                 "TechnoEcon.command", "ExpPlanner.command",
                 "Hub.command", "ImpactTracker.command",
                 "VentureStudio.command"]


def main():
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    dest = DEST_DIR / f"paperrag_backup_{stamp}.zip"

    n_files = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in INCLUDE_FILES:
            p = HERE / name
            if p.exists():
                zf.write(p, name)
                n_files += 1
        for dname in INCLUDE_DIRS:
            d = HERE / dname
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    zf.write(p, str(p.relative_to(HERE)))
                    n_files += 1

    size_mb = dest.stat().st_size / 1e6
    print(f"Backup written: {dest}  ({n_files} files, {size_mb:.1f} MB)")

    backups = sorted(DEST_DIR.glob("paperrag_backup_*.zip"))
    for old in backups[:-KEEP]:
        try:
            old.unlink()
            print(f"Removed old backup: {old.name}")
        except Exception:
            pass

    if n_files == 0:
        print("WARNING: nothing was backed up - is this script inside "
              "the PaperRag folder?")
        sys.exit(1)


if __name__ == "__main__":
    main()
