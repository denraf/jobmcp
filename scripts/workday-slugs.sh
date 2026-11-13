#!/usr/bin/env bash
# Turn a JSON list of "tenant|wdN|site" strings into jobmcp board lines ("workday:tenant.wdN/site"), ready to paste
# into the admin portal's bulk import. Reads the URL given as $1 (default: the job-board-aggregator list), or a local
# file if $1 is a path.
set -euo pipefail

src=${1:-https://raw.githubusercontent.com/Riley-Dorrington/job-board-aggregator/main/data/workday_companies.json}

if [ -f "$src" ]; then cat "$src"; else curl -fsSL "$src"; fi |
  jq -r '.[] | split("|") | select(length == 3) | "workday:\(.[0]).\(.[1])/\(.[2])"' |
  sort -u
