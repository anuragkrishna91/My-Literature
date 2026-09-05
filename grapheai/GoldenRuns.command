#!/bin/bash
# GrapheAI golden runs - deterministic self-test of the Workbench machinery
# (no model calls). Double-click after every update.
cd "$(dirname "$0")"
/opt/miniconda3/bin/python golden_runs.py
echo
read -n 1 -s -r -p "Press any key to close..."
