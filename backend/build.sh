#!/bin/bash
# Build the React dashboard before starting Flask
set -e

echo "==> Installing dashboard dependencies..."
cd dashboard
npm install --production=false
echo "==> Building dashboard..."
npx vite build
cd ..
echo "==> Dashboard built successfully to dashboard/dist/"
