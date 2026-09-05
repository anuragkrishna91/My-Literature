#!/bin/bash
cd "$(dirname "$0")"
/opt/miniconda3/bin/python -m streamlit run technoecon.py --server.port 8508 \
  --theme.base dark --theme.primaryColor "#FF6B3D" \
  --theme.backgroundColor "#12161C" \
  --theme.secondaryBackgroundColor "#1B222B" \
  --theme.textColor "#E6EAF0"
