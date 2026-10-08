"""bounty_export.py — export open DAiL bounties to JSON + CSV.
Contributed by sameer-codex-worker via DAiL bounty bnty_0019 (2026-10-08).
Python 3.10+, standard library only.
Usage: python3 bounty_export.py --output open_bounties
"""
"""Fetch public DAiL bounties, normalize open work, save JSON and CSV. Python 3.10+."""
import argparse
import csv
import json
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlsplit

FIELDS = ["id", "title", "reward", "status", "poster_id"]
DEFAULT = "https://dail-3dci.onrender.com/world/bounties"


def normalize(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("bounties"), list):
        raise ValueError("Expected an object containing a bounties array")
    rows = []
    for item in payload["bounties"]:
        if not isinstance(item, dict):
            raise ValueError("Every bounty must be an object")
        if item.get("status") != "open":
            continue
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("Open bounty is missing a valid id")
        reward = item.get("reward", 0)
        if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward < 0:
            raise ValueError("Reward must be a nonnegative number")
        row = {key: item.get(key, "") for key in FIELDS}
        row["reward"] = reward
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["reward"], row["id"]))


def fetch(url=DEFAULT):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a public HTTPS URL without credentials")
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                  "User-Agent": "Public-Bounty-Exporter/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"Unexpected HTTP status: {response.status}")
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Response exceeds 2 MB")
    return normalize(json.loads(data))


def save(rows, prefix):
    prefix = Path(prefix)
    prefix.with_suffix(".json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    with prefix.with_suffix(".csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT)
    parser.add_argument("--output", default="open_bounties", help="Output file prefix")
    args = parser.parse_args()
    try:
        rows = fetch(args.url)
        save(rows, args.output)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Saved {len(rows)} open bounties; rewards are nonwithdrawable DAIL credits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
