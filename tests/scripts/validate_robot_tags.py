#!/usr/bin/env python3
"""Validate Robot test tags against Chronicle's canonical allowlist."""

import argparse
import json
from pathlib import Path

from robot.api.parsing import get_model
from robot.parsing.model.blocks import TestCase, TestCaseSection
from robot.parsing.model.statements import Tags, TestTags

TEST_SUITE_DIRS = (
    "endpoints",
    "integration",
    "infrastructure",
    "asr",
    "browser",
    "configuration",
)


def load_allowed_tags(config_path: Path) -> set[str]:
    groups = json.loads(config_path.read_text())
    return {tag for tags in groups.values() for tag in tags}


def validate_file(path: Path, allowed_tags: set[str]) -> list[str]:
    errors: list[str] = []
    model = get_model(path)
    suite_tags: set[str] = set()

    for section in model.sections:
        for node in getattr(section, "body", ()):
            if isinstance(node, TestTags):
                suite_tags.update(node.values)

    invalid_suite_tags = suite_tags - allowed_tags
    if invalid_suite_tags:
        errors.append(
            f"{path}: invalid suite tags: {', '.join(sorted(invalid_suite_tags))}"
        )

    for section in model.sections:
        if not isinstance(section, TestCaseSection):
            continue
        for test in section.body:
            if not isinstance(test, TestCase):
                continue
            tag_statement = next(
                (node for node in test.body if isinstance(node, Tags)), None
            )
            test_tags = set(tag_statement.values) if tag_statement else set()
            invalid_tags = test_tags - allowed_tags
            location = f"{path}:{test.lineno} ({test.name})"

            if invalid_tags:
                errors.append(
                    f"{location}: invalid tags: {', '.join(sorted(invalid_tags))}"
                )
            if not suite_tags and not test_tags:
                errors.append(f"{location}: no business or execution tag")

    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path(name) for name in TEST_SUITE_DIRS],
        help="Robot files or suite directories (defaults to all test suite directories)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "robot-tags.json",
        help="Path to the canonical tag allowlist",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    allowed_tags = load_allowed_tags(args.config)
    files: set[Path] = set()

    for path in args.paths:
        if path.is_dir():
            files.update(path.rglob("*.robot"))
        elif path.suffix == ".robot" and path.is_file():
            files.add(path)
        else:
            print(f"Tag validation path does not exist or is not a Robot file: {path}")
            return 2

    errors = [
        error for path in sorted(files) for error in validate_file(path, allowed_tags)
    ]
    if errors:
        print("Robot tag validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Validated {len(files)} Robot suites against {len(allowed_tags)} tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
