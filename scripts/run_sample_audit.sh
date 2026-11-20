#!/usr/bin/env bash
# Serve both bundled test sites and audit them. Fully offline except the
# two example.com links in the flawed site (checked live; add --no-external
# to the audit commands below to skip those). Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m http.server 8123 --directory sample_data/site >/tmp/site8123.log 2>&1 &
SRV1=$!
python3 -m http.server 8124 --directory sample_data/site-fixed >/tmp/site8124.log 2>&1 &
SRV2=$!
trap 'kill $SRV1 $SRV2 2>/dev/null || true' EXIT
sleep 1

.venv/bin/python -m seo_audit --url http://127.0.0.1:8123/ --max-pages 20 --out sample_output/
.venv/bin/python -m seo_audit --url http://127.0.0.1:8124/ --max-pages 20 --out /tmp/audit-fixed/
cp /tmp/audit-fixed/report.md sample_output/report-fixed.md
cp /tmp/audit-fixed/report.html sample_output/report-fixed.html
echo "Done. Compare sample_output/report.md with sample_output/report-fixed.md."
