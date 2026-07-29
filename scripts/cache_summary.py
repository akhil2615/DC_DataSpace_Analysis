"""Print what landed in the live metadata cache: status, list key and record count."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / ".data-space-analysis-cache"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    prov = json.loads((CACHE / "_provenance.json").read_text(encoding="utf-8"))
    print(f"org={prov['orgId']} fetched={prov['fetchedAtUtc']} api={prov['apiVersion']}")
    for f in sorted(CACHE.glob("*.json")):
        if f.stem == "_provenance":
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        recs = d.get("records")
        n = len(recs) if isinstance(recs, list) else "-"
        extra = ""
        if "byType" in d:
            extra = f" byType={ {k: len(v) for k, v in d['byType'].items()} }"
        if "byDmo" in d:
            extra = f" byDmo={len(d['byDmo'])}"
        if "bySpace" in d:
            extra = f" bySpace={list(d['bySpace'])}"
        print(f"{f.stem:28s} status={d.get('status')} key={d.get('key')} n={n}{extra}")


if __name__ == "__main__":
    main()
