#!/usr/bin/env bash
set -e
echo "Starting RC BEYOND TOGETHER: GALACTIC FLEET..."
echo "Open http://localhost:8000 on Host computer & scan QR from phones!"
uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"
