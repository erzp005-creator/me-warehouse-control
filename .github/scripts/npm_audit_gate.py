#!/usr/bin/env python3
"""npm audit gate with a documented exception allowlist.

pip-audit takes `--ignore-vuln <ID>`, and audit.yml's convention is that
every such exclusion carries an inline comment naming the reason. npm audit
has no equivalent flag, so the npm jobs had no way to accept a single
advisory short of dropping the gate entirely. This wraps `npm audit --json`
and applies the same convention.

Usage (from the package directory):

    AUDIT_ALLOW="GHSA-xxxx-yyyy-zzzz" python3 .../npm_audit_gate.py [npm flags]

Any extra arguments are passed through to `npm audit`, so the prod-tree job
can still scope itself with `--omit=dev`.

Behaviour:

  * Fails on any high or critical advisory that is NOT in AUDIT_ALLOW. An
    accepted advisory suppresses only its own GHSA id, so a new one on the
    same package still breaks the build. This is the property that makes an
    allowlist safe: it is not a severity threshold and not a package mute.
  * Warns, without failing, when an allowlisted id is no longer reported.
    A stale exception is how an allowlist silently rots into a blanket
    bypass, so it gets surfaced for removal.
  * Prints every high/critical advisory it saw, accepted or not, so the log
    shows what was waived rather than hiding it.
"""

import json
import os
import subprocess
import sys


def _ghsa(via: dict) -> str:
    url = via.get("url") or ""
    if url:
        return url.rstrip("/").rsplit("/", 1)[-1]
    return str(via.get("source") or "unknown")


def parse_allow(raw: str) -> set:
    """AUDIT_ALLOW is whitespace- or comma-separated GHSA ids."""
    return {token.strip() for token in (raw or "").replace(",", " ").split() if token.strip()}


def evaluate(data: dict, allow: set):
    """Split an `npm audit --json` body into (blocking, accepted, stale).

    Kept free of I/O so the enforcement rules are directly testable:
    severity filtering, per-id (not per-package) acceptance, and stale
    allowlist detection are the properties the CI gate depends on.
    """
    blocking: dict = {}
    accepted: dict = {}
    for pkg, vuln in (data.get("vulnerabilities") or {}).items():
        for via in vuln.get("via") or []:
            if not isinstance(via, dict):
                continue
            if (via.get("severity") or "").lower() not in ("high", "critical"):
                continue
            ident = _ghsa(via)
            bucket = accepted if ident in allow else blocking
            entry = bucket.setdefault(
                ident,
                {
                    "severity": via.get("severity", "?"),
                    "title": via.get("title", ""),
                    "range": via.get("range", ""),
                    "packages": set(),
                },
            )
            entry["packages"].add(pkg)
    stale = set(allow) - set(accepted)
    return blocking, accepted, stale


def main() -> int:
    extra = sys.argv[1:]
    allow = parse_allow(os.environ.get("AUDIT_ALLOW", ""))

    proc = subprocess.run(
        ["npm", "audit", "--json", *extra],
        capture_output=True,
        text=True,
    )
    # npm audit exits non-zero whenever findings exist, so the exit code is
    # not the signal here; the JSON body is.
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stdout.write(proc.stdout[-4000:])
        sys.stderr.write(proc.stderr[-4000:])
        print("::error::npm audit did not return parseable JSON")
        return 1

    blocking, accepted, stale = evaluate(data, allow)

    for ident, e in sorted(accepted.items()):
        print(
            f"ACCEPTED  {e['severity'].upper():<8} {ident}  {e['title'][:70]}\n"
            f"          packages: {', '.join(sorted(e['packages']))}  range: {e['range']}"
        )

    for ident in sorted(stale):
        print(
            f"::warning::AUDIT_ALLOW entry {ident} is no longer reported. "
            "Remove it so the allowlist cannot mask a future advisory."
        )

    for ident, e in sorted(blocking.items()):
        print(
            f"::error::{e['severity'].upper()} {ident}  {e['title'][:70]}\n"
            f"          packages: {', '.join(sorted(e['packages']))}  range: {e['range']}"
        )

    if blocking:
        print(
            f"\n{len(blocking)} unaccepted high/critical advisor"
            f"{'y' if len(blocking) == 1 else 'ies'}. "
            "Fix it, or add the GHSA id to this job's AUDIT_ALLOW with a "
            "comment naming why it does not apply."
        )
        return 1

    total = data.get("metadata", {}).get("vulnerabilities", {})
    print(
        f"\nNo unaccepted high/critical advisories "
        f"({len(accepted)} accepted). Full counts: {total}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
