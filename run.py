#!/usr/bin/env python3
"""
ExcelTech Recruitment Agent — Launcher
Starts both Flask (main web app) and FastAPI (AI agent layer).

Usage:
  python run.py              # Start Flask only (port 5001)
  python run.py --with-agents  # Start Flask + FastAPI agents (ports 5001 + 8001)
"""

import os
import sys
import subprocess

def main():
    backend_dir = os.path.join(os.path.dirname(__file__), "backend")

    with_agents = "--with-agents" in sys.argv

    if with_agents:
        # Start FastAPI agent layer in background
        print("[*] Starting FastAPI AI Agent layer on port 8001...")
        agent_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ai-agents.main:app", "--host", "127.0.0.1", "--port", "8001", "--reload"],
            cwd=backend_dir,
            env={**os.environ, "PYTHONPATH": backend_dir},
        )
        print("[*] FastAPI agent layer started (PID: {})".format(agent_proc.pid))

    # Start Flask app
    print("[*] Starting Flask web app on port 5001...")
    print("[*] Open http://localhost:5001 in your browser")
    print("[*] Login with recruiter credentials (e.g. raju/raju18 for TL access)")
    print()

    os.environ.setdefault("FLASK_PORT", "5001")
    os.environ.setdefault("AI_AGENT_URL", "http://localhost:8001")

    try:
        subprocess.run(
            [sys.executable, "app.py"],
            cwd=backend_dir,
        )
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")
    finally:
        if with_agents:
            agent_proc.terminate()


if __name__ == "__main__":
    main()
