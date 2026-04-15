#!/bin/bash
# Build script for Railway deployment
set -e

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Copying frontend files..."
# The frontend HTML files live one level up from backend/
# On Railway, the root dir is backend/, so we copy them in
mkdir -p ../frontend-saas ../frontend-exceltech
echo "==> Build complete."
