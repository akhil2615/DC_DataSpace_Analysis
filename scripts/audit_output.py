"""Audit a filled analysis record: per-column fill stats plus sample rows."""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document

TABLES = {
    4: "0.1 data space object",
    5: "0.2 data space scope",
    10: "2.1 stream inventory",
    11: "2.2 source connections",
    13: "3.1 dlo inventory",
    15: "4.1 dmo inventory",
    16: "4.2 dmo relationships",
    20: "5.1a ruleset header",
    21: "5.1a match rules",
    22: "5.1b reconciliation",
    25: "6.1 calculated insights",
    27: "7.1 segment inventory",
    28: "7.2 segment logic",
    32: "8.1a activations",
    33: "8.1b activation payload",
    42: "11.3 search indexes",
}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(sys.argv[1])
    d = Document(str(path))
    for ti, label in TABLES.items():
        t = d.tables[ti]
        hdr = [c.text.strip() for c in t.rows[0].cells]
        data = t.rows[1:]
        print(f"\n=== T{ti} {label} rows={len(data)}")
        for ci, h in enumerate(hdr):
            f = b = p = 0
            for r in data:
                if ci >= len(r.cells):
                    continue
                v = r.cells[ci].text.strip()
                if not v:
                    b += 1
                elif v.startswith("\u00ab") or "NOT AVAILABLE" in v:
                    p += 1
                else:
                    f += 1
            print(f"   {ci} {h[:36]:38s} filled={f:4d} blank={b:4d} na/placeholder={p:4d}")
        for r in data[:2]:
            cells = [c.text.strip()[:40] for c in r.cells]
            print("   sample:", " | ".join(cells))


if __name__ == "__main__":
    main()
