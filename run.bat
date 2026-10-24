@echo off
:: One-command launcher for Windows.
:: Requires: Python 3.10+ and FFmpeg on PATH (see README).
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
