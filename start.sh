#!/bin/bash
# Railway start script - ensures gunicorn can find the app module
cd /app/backend
export PYTHONPATH=/app/backend:$PYTHONPATH
exec gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 120
