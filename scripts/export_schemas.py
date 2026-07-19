"""Generate or verify committed cross-module JSON Schemas."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.domain.schema import render_domain_schema


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the schema is stale")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    target = root / "schemas" / "domain-contracts-v1.json"
    expected = render_domain_schema()

    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            print("Domain schema is stale. Run: python scripts/export_schemas.py")
            return 1
        print("Domain schema is current.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
