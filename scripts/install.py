#!/usr/bin/env python3
"""Install the self-contained skill without overwriting any existing installation."""
from __future__ import annotations
import argparse
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "imagegen-relay"
# A file-level allowlist also excludes unexpected files inside public directories.
PUBLIC_FILES = (
    "SKILL.md", "agents/openai.yaml", "prompts/example.txt",
    "references/components.json", "references/components.md", "references/routing.md",
    "scripts/gateway.py", "scripts/credentials.py", "scripts/relay_gen.py",
    "scripts/prompt_search.py", "scripts/awake_engine.py",
    "third_party/relay-imagegen/LICENSE",
    "third_party/relay-imagegen/scripts/image_client.py",
)


def install(target=None):
    if target is None:
        home = Path.home()
        codex_home = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        candidates = [home / ".agents" / "skills" / "imagegen-relay", codex_home / "skills" / "imagegen-relay"]
        if any(path.exists() or path.is_symlink() for path in candidates):
            raise ValueError("An imagegen-relay installation already exists; it has not been changed. Back it up first, or explicitly provide a different --target.")
        target = candidates[0]
    target = Path(target).expanduser().absolute()
    if target.name != "imagegen-relay":
        raise ValueError("The target directory must be named imagegen-relay.")
    if target.exists() or target.is_symlink():
        raise ValueError("Target exists; refusing to overwrite or delete it.")
    # Explicit whitelist: never copy runtime secrets, caches, outputs or Git metadata.
    files = [(SOURCE / name, target / name) for name in PUBLIC_FILES]
    files.extend((ROOT / name, target / name) for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"))
    for source, _ in files:
        if not source.is_file() or source.is_symlink():
            raise ValueError("A required public source file is absent or a symlink: " + source.name)
    for file, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, destination)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="An absent directory named imagegen-relay; existing files are never replaced.")
    args = parser.parse_args(argv)
    try:
        target = install(args.target)
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    print("Installed 智能生图skills at " + str(target))
    print("Describe your image request naturally in Codex. API credentials are only requested if required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
