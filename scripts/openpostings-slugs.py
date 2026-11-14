#!/usr/bin/env python3
"""Turn a list of careers-page URLs into jobmcp board lines ("vendor:board"),
ready to paste into the admin portal's bulk import.

Built for the company list of OpenPostings (github.com/Masterjx9/OpenPostings):
export the `url_string` column of the `companies` table of its jobs.db, one
URL per line, with any database tool, then

    python scripts/openpostings-slugs.py urls.txt > boards.txt
    python scripts/openpostings-slugs.py urls.txt --vendor ukg --vendor ultipro
    some-export-command | python scripts/openpostings-slugs.py - > boards.txt

Every URL goes through the same classifier as Common Crawl discovery, so the
lines are exactly what jobmcp would have discovered itself. URLs for a
platform jobmcp does not support are counted and skipped.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from jobmcp.vendors import classify


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", help="file with one URL per line, or - for stdin")
    ap.add_argument(
        "--vendor", action="append", help="only this vendor (repeatable)"
    )
    args = ap.parse_args()

    src = sys.stdin if args.urls == "-" else open(args.urls, encoding="utf-8")
    lines: set[str] = set()
    skipped: Counter[str] = Counter()
    with src:
        for raw in src:
            url = raw.strip()
            if not url:
                continue
            found = classify(url)
            if not found:
                skipped["no board in the URL"] += 1
            elif args.vendor and found[0] not in args.vendor:
                skipped["other vendor"] += 1
            else:
                lines.add(f"{found[0]}:{found[1]}")
    print("\n".join(sorted(lines)))
    per = Counter(line.split(":", 1)[0] for line in lines)
    for vendor, n in per.most_common():
        print(f"{vendor}: {n}", file=sys.stderr)
    print(f"{len(lines)} boards; skipped {dict(skipped)}", file=sys.stderr)


if __name__ == "__main__":
    main()
