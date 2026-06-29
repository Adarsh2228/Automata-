#!/usr/bin/env bash
set -o errexit
export PLAYWRIGHT_BROWSERS_PATH=$(pwd)/.ms-playwright
pip install -r requirements.txt
playwright install chromium
