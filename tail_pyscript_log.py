#!/usr/bin/env python3
"""
tail_pyscript_log.py

Extracts the last N pyscript log *entries* (header + continuation lines) from
Home Assistant's log and writes them to a smaller file.

- Detects entry boundaries by timestamp at line start.
- Keeps a ring buffer of matching entries and their continuation lines.

Defaults:
  input  = /opt/appdata/hass/homeassistant/home-assistant.log
  output = pyscript_working.log
  count  = 50
"""

import argparse
import io
import os
import re
from collections import deque

ENTRY_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+')
DEFAULT_NEEDLES = [
    "pyscript",
    "custom_components.pyscript",
    "[pyscript",
]

def last_pyscript_blocks(in_path: str, count: int, needles: list[str]) -> list[str]:
    needles = [n.lower() for n in needles]
    ring = deque(maxlen=count)

    with io.open(in_path, "r", encoding="utf-8", errors="replace") as f:
        current = []            # lines of the current entry
        keep_current = False    # whether current entry matches pyscript

        for line in f:
            is_header = bool(ENTRY_RE.match(line))
            if is_header:
                # flush previous
                if current and keep_current:
                    ring.append("".join(current))
                # start new
                current = [line]
                keep_current = any(n in line.lower() for n in needles)
            else:
                # continuation line
                if current:
                    current.append(line)

        # flush final
        if current and keep_current:
            ring.append("".join(current))

    return list(ring)

def main():
    ap = argparse.ArgumentParser(description="Write last N pyscript log entries to a small file.")
    ap.add_argument("-i", "--input",
        default="/opt/appdata/hass/homeassistant/home-assistant.log",
        help="Path to home-assistant.log")
    ap.add_argument("-o", "--output",
        default="pyscript_working.log",
        help="Output file path")
    ap.add_argument("-n", "--count", type=int, default=50,
        help="Number of pyscript entries to keep (default: 50)")
    ap.add_argument("-e", "--extra", action="append", default=[],
        help="Additional case-insensitive header substrings to match (repeatable)")
    ap.add_argument("-t", "--tokens", action="append",
        help="Override default tokens entirely (repeatable)")
    args = ap.parse_args()

    needles = args.tokens if args.tokens else (DEFAULT_NEEDLES + args.extra)
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)

    blocks = last_pyscript_blocks(args.input, args.count, needles)
    with io.open(args.output, "w", encoding="utf-8", errors="replace") as out:
        out.write("".join(blocks))

    print(f"Wrote {len(blocks)} pyscript entries to {args.output}")

if __name__ == "__main__":
    main()
