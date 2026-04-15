"""WSGI entry point for Railway deployment.
Adds the backend directory to Python's path so gunicorn can import app.
"""
import sys
import os

# Add backend directory to Python path
backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend')
sys.path.insert(0, backend_dir)

# Change working directory to backend so relative imports/paths work
os.chdir(backend_dir)

from app import app  # noqa: E402
