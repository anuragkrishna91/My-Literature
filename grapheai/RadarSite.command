#!/bin/bash
# Build the public PV Radar website from the local store and preview it.
# Output goes to ./site (plain HTML; upload that folder to any static host).
cd "$(dirname "$0")"
/opt/miniconda3/bin/python radar_site.py build --fetch --serve --open
