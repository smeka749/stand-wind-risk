#!/usr/bin/env python3
"""Reference-score gate: every reference address must score within +/-2.

Against a running API (local Docker or the deployed ALB) -- submits the
reference addresses plus any --address extras, polls until each quote
is Quoted or Failed, then applies the gate:

    python scripts/check_reference_scores.py --api http://localhost:8000
    python scripts/check_reference_scores.py --api $API_URL \
        --address "1600 Amphitheatre Pkwy, Mountain View, CA 94043"

Against score files copied from a run (scores/<reference file name>):

    python scripts/check_reference_scores.py --scores-dir scores

Exit 0 when every reference address passes and every extra address got
a score; 1 otherwise. Reference values are read from the SME's
wind-risk-take-home/reference/*.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.reference_gate import compare_scores

# Reference file -> the address it was produced for.
ADDRESSES = {
    "169-tuscaloosa-ave-atherton-ca-94027.json":
        "169 Tuscaloosa Ave, Atherton, CA 94027",
    "1265-lands-end-rd-lantana-fl-33462.json":
        "1265 Lands End Rd, Lantana, FL 33462",
}


def _call(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def run_quotes(api: str, addresses: list[str], timeout: float,
               poll: float) -> dict[str, dict]:
    """Submit all addresses at once (a small burst), poll until done."""
    api = api.rstrip("/")
    ids = {}
    for address in addresses:
        q = _call(f"{api}/quotes", {"address": address})
        ids[address] = q["quote_id"]
        print(f"submitted {address} -> {q['quote_id']} ({q['phase']}/{q['status']})")
    done: dict[str, dict] = {}
    start = time.monotonic()
    while len(done) < len(ids) and time.monotonic() - start < timeout:
        time.sleep(poll)
        for address, quote_id in ids.items():
            if address in done:
                continue
            q = _call(f"{api}/quotes/{quote_id}")
            if q["phase"] == "Quoted" or q["status"] == "Fail":
                q["elapsed_seconds"] = round(time.monotonic() - start)
                done[address] = q
                print(f"finished {address}: {q['phase']}/{q['status']} "
                      f"score={q['score']} after {q['elapsed_seconds']}s")
    for address, quote_id in ids.items():
        done.setdefault(address, {"quote_id": quote_id, "phase": None,
                                  "status": "Timeout", "score": None})
    return done


def report(rows: list[dict]) -> bool:
    failed = False
    for row in rows:
        if row["reason"] == "missing":
            print(f"FAIL {row['name']}: reference={row['reference']}, no score")
            failed = True
        elif row["reason"]:
            print(f"FAIL {row['name']}: {row['reason']}")
            failed = True
        else:
            verdict = "PASS" if row["ok"] else "FAIL"
            print(f"{verdict} {row['name']}: reference={row['reference']}, "
                  f"actual={row['actual']}, diff={row['diff']:.2f}")
            failed = failed or not row["ok"]
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference-dir", type=Path,
                    default=Path(__file__).resolve().parents[2]
                    / "wind-risk-take-home" / "reference")
    ap.add_argument("--scores-dir", type=Path, default=Path("scores"))
    ap.add_argument("--api", help="base URL of a running API")
    ap.add_argument("--address", action="append", default=[],
                    help="extra address to run (scored, not gated)")
    ap.add_argument("--timeout", type=float, default=1800,
                    help="seconds to wait for all quotes (default: the 30 min SLA)")
    ap.add_argument("--poll", type=float, default=15)
    args = ap.parse_args(argv)

    failed = False
    scores_dir = args.scores_dir
    if args.api:
        refs = sorted(p.name for p in args.reference_dir.glob("*.json"))
        unknown = [r for r in refs if r not in ADDRESSES]
        if unknown:
            print(f"no address known for reference files: {unknown}")
            return 1
        results = run_quotes(args.api, [ADDRESSES[r] for r in refs] + args.address,
                             args.timeout, args.poll)
        scores_dir = Path(tempfile.mkdtemp(prefix="scores-"))
        for ref in refs:
            q = results[ADDRESSES[ref]]
            if q.get("score") is not None:
                (scores_dir / ref).write_text(json.dumps({"score": q["score"]}))
        for address in args.address:
            q = results[address]
            ok = q.get("score") is not None
            print(f"{'OK  ' if ok else 'FAIL'} {address}: score={q.get('score')} "
                  f"({q.get('status')}) {q.get('error') or ''}".rstrip())
            failed = failed or not ok

    rows = compare_scores(args.reference_dir, scores_dir)
    if not rows:
        print(f"no reference files in {args.reference_dir}")
        return 1
    failed = report(rows) or failed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
