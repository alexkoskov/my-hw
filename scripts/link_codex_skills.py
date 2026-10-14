#!/usr/bin/env python3
"""Expose provisioned shared skills inside a Git root without copying them."""

import argparse
import os
from pathlib import Path


SKILLS = (
    "decision-quality",
    "grilling",
    "integration-reliability",
    "project-progress",
    "test-quality",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills_dir", type=Path, help="absolute shared skill directory")
    args = parser.parse_args()
    source = args.skills_dir
    if not source.is_absolute() or not source.is_dir():
        parser.error("skills_dir must be an existing absolute directory")
    destination = Path(__file__).resolve().parent.parent / ".agents" / "skills"
    links = []
    for name in SKILLS:
        target = source / name
        link = destination / name
        if not (target / "SKILL.md").is_file():
            parser.error(f"Missing shared skill: {name}")
        if os.path.lexists(link):
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            parser.error(f"Existing project skill would be overwritten: {link}")
        links.append((link, target))
    destination.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for link, target in links:
            link.symlink_to(os.path.relpath(target, link.parent), target_is_directory=True)
            created.append(link)
    except OSError:
        for link in created:
            link.unlink()
        raise
    print(f"Shared Codex skills available: {len(SKILLS)}; new links: {len(created)}")


if __name__ == "__main__":
    main()
