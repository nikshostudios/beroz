#!/usr/bin/env python3
"""
ExcelTech Recruitment Agent — Launcher
Starts the single Flask process. The AI agent layer (formerly a separate
FastAPI service on port 8001) is now merged into this process; see
backend/ai_agents/core.py.

Usage:
  python run.py
"""

import os
import subprocess
import sys


def main():
    backend_dir = os.path.join(os.path.dirname(__file__), "backend")
    os.environ.setdefault("FLASK_PORT", "5001")

    print("[*] Starting Flask web app on port 5001 (AI agent core merged in)...")
    print("[*] Open http://localhost:5001 in your browser")
    print("[*] Login with recruiter credentials (e.g. raju/raju18 for TL access)")
    print()

    try:
        subprocess.run([sys.executable, "app.py"], cwd=backend_dir)
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")


if __name__ == "__main__":
    main()
