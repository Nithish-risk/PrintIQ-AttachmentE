#!/usr/bin/env bash
set -e
python -m streamlit run app.py --server.port ${PORT:-8000} --server.address 0.0.0.0 --server.headless true
