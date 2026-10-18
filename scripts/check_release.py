#!/usr/bin/env python3
"""Heuristic privacy check of files that would be published by Git; no network calls."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "possible API key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "possible JWT": re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal absolute path": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/(?:Users|home)/[^/\s]+/"),
    "personal email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


def candidate_files():
    root_check = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if root_check.returncode == 0 and Path(root_check.stdout.strip()).resolve() == ROOT.resolve():
        listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, check=True)
        return sorted({ROOT / name.decode("utf-8") for name in listing.stdout.split(b"\0") if name})
    return sorted(path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.relative_to(ROOT).parts)


def check():
    findings = []
    files = candidate_files()
    for path in files:
        rel = path.relative_to(ROOT)
        parts = set(rel.parts)
        if parts & {".secrets", ".cache", "outputs", "private"} or path.name in {"auth.json", "config.toml", "cc-switch.db"} or path.suffix.lower() in {".dpapi", ".pem", ".key", ".pfx", ".p12"} or (path.name.startswith(".env") and path.name != ".env.example"):
            findings.append((str(rel), 0, "private runtime/configuration file"))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append((str(rel), 0, "non-text file requires manual review"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((str(rel), number, label))
    return files, findings


def main():
    files, findings = check()
    for path, line, label in findings:
        # Never echo the matched secret or personal content.
        print(f"REVIEW {path}:{line}: {label}")
    if findings:
        print(f"Needs review: {len(findings)} finding(s). No matched content was printed.")
        return 1
    print(f"PASS: inspected {len(files)} Git candidate files. Review staged diff/history as well; this is not a guarantee of anonymity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
