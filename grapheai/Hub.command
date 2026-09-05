#!/bin/bash
cd "$(dirname "$0")"
/opt/miniconda3/bin/python -m streamlit run hub.py --server.port 8500 \
  --theme.base dark --theme.primaryColor "#FF6B3D" \
  --theme.backgroundColor "#12161C" \
  --theme.secondaryBackgroundColor "#1B222B" \
  --theme.textColor "#E6EAF0"
