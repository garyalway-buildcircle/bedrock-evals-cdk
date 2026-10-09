#!/usr/bin/env python3
"""Rebuild datasets/*.jsonl from fixtures/ and the current prompt text.

Jobs send each row's `prompt` field to the model; they do not call the Bedrock Prompt
resource. Fixtures are the source: each fixture's field named by the user template's
`{{variable}}` is the untrusted input, and `expected` is the reference response.

    python3 scripts/render-datasets.py                 # every prompt
    python3 scripts/render-datasets.py example
    python3 scripts/render-datasets.py --check         # exit non-zero if stale; writes nothing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "prompts"
# Keep in sync with lib/prompts.ts inputVariableNames.
INPUT_VARIABLE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")
WRAP_MARKERS = ("<<<UNTRUSTED_CONTENT>>>", "<<<END_UNTRUSTED_CONTENT>>>")
RATING_DEFINITION_LIMIT = 100


def read_prompt_text(prompt_dir: Path, file_name: str) -> str:
    path = prompt_dir / file_name
    if not path.is_file():
        raise SystemExit(f"{prompt_dir.name}: {file_name} is missing")
    return path.read_text()


def input_variable_name(template: str, prompt_id: str) -> str:
    names = INPUT_VARIABLE.findall(template)
    if not names:
        raise SystemExit(f"{prompt_id}: user-message-template.txt has no {{variable}} placeholder")
    if len(names) > 1:
        raise SystemExit(
            f"{prompt_id}: user-message-template.txt must contain exactly one placeholder, found {len(names)} ({', '.join(names)})"
        )
    return names[0]


def prompt_dirs(requested: list[str]) -> list[Path]:
    if requested:
        dirs = [PROMPTS / name for name in requested]
        for d in dirs:
            if not d.is_dir():
                raise SystemExit(f"no such prompt: prompts/{d.name}")
        return dirs
    return sorted(d for d in PROMPTS.iterdir() if d.is_dir())


def fixture_sets(prompt_dir: Path) -> list[Path]:
    fixtures = prompt_dir / "fixtures"
    if not fixtures.is_dir():
        raise SystemExit(f"{prompt_dir.name}: no fixtures/ directory")
    sets = sorted(p for p in fixtures.iterdir() if p.is_dir())
    if not sets:
        raise SystemExit(f"{prompt_dir.name}: no fixtures/<set>/ directories")
    return sets


def load_set(set_dir: Path, variable: str, prompt_id: str) -> list[dict]:
    """Fixture rows in index.json order. The index and the files on disk must name the same set."""
    index_path = set_dir / "index.json"
    rel = f"{prompt_id}/fixtures/{set_dir.name}"
    if not index_path.is_file():
        raise SystemExit(f"{rel}: index.json is missing")
    try:
        index = json.loads(index_path.read_text())
    except json.JSONDecodeError as err:
        raise SystemExit(f"{rel}/index.json is not valid JSON: {err}") from err
    listed = index.get("fixtures") if isinstance(index, dict) else None
    if not isinstance(listed, list) or not listed:
        raise SystemExit(f"{rel}/index.json fixtures must be a non-empty list")

    ordered_names = []
    listed_ids = []
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not entry["file"]:
            raise SystemExit(f"{rel}/index.json: each fixtures entry needs a file name")
        ordered_names.append(entry["file"])
        listed_ids.append(entry.get("id"))
    if len(set(ordered_names)) != len(ordered_names):
        raise SystemExit(f"{rel}/index.json lists the same file more than once")

    on_disk = {p.name for p in set_dir.glob("*.json") if p.name != "index.json"}
    missing = [name for name in ordered_names if name not in on_disk]
    extra = sorted(on_disk - set(ordered_names))
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("not listed in index.json: " + ", ".join(extra))
        raise SystemExit(f"{rel}: " + "; ".join(detail))

    rows = []
    for file_name, listed_id in zip(ordered_names, listed_ids):
        path = set_dir / file_name
        try:
            fixture = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            raise SystemExit(f"{rel}/{file_name} is not valid JSON: {err}") from err
        if not isinstance(fixture, dict):
            raise SystemExit(f"{rel}/{file_name} must be a JSON object")
        fixture_id = fixture.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise SystemExit(f"{rel}/{file_name}: id must be a non-empty string")
        if listed_id is not None and listed_id != fixture_id:
            raise SystemExit(f"{rel}/{file_name}: id is {fixture_id!r} but index.json says {listed_id!r}")
        payload = fixture.get(variable)
        if not isinstance(payload, str):
            raise SystemExit(
                f"{rel}/{file_name}: {variable} must be a string "
                f"(the {{{{{variable}}}}} placeholder in user-message-template.txt is the untrusted input)"
            )
        for marker in WRAP_MARKERS:
            if marker in payload:
                raise SystemExit(
                    f"{rel}/{file_name}: {variable} contains {marker}. "
                    "The report cannot find the end of the input if a fixture includes a wrap marker."
                )
        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            raise SystemExit(f"{rel}/{file_name}: expected must be a JSON object")
        rows.append({"id": fixture_id, "payload": payload, "expected": expected})
    return rows


def assemble_prompt(system_prompt: str, template: str, placeholder: str, payload: str) -> str:
    body = template.replace(placeholder, payload).rstrip("\n")
    return system_prompt + "\n" + body


def render_rows(system_prompt: str, template: str, variable: str, set_name: str, fixtures: list[dict]) -> list[dict]:
    placeholder = "{{" + variable + "}}"
    rows = []
    for fixture in fixtures:
        rows.append(
            {
                "prompt": assemble_prompt(system_prompt, template, placeholder, fixture["payload"]),
                "referenceResponse": json.dumps(fixture["expected"], ensure_ascii=True),
                "category": set_name,
                "fixtureId": fixture["id"],
            }
        )
    return rows


def serialise(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=True) + "\n" for row in rows)


def changed_rows(previous: str | None, rows: list[dict], text: str) -> int:
    """Any byte difference is stale, including a hand-edit that keeps the same JSON values."""
    if previous == text:
        return 0
    if not previous:
        return len(rows)
    try:
        old_rows = [json.loads(line) for line in previous.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return max(len(rows), 1)
    if len(old_rows) != len(rows):
        return max(len(old_rows), len(rows))
    semantic = sum(1 for old, new in zip(old_rows, rows) if old != new)
    return semantic or len(rows)


def assert_unique_fixtures(prompt_id: str, loaded: list[tuple[str, list[dict]]]) -> None:
    """The report keys a case by fixture id and matches rows on trimmed input text. Either collision drops a row."""
    seen_ids: dict[str, str] = {}
    seen_text: dict[str, str] = {}
    for set_name, fixtures in loaded:
        for fixture in fixtures:
            fixture_id = fixture["id"]
            previous_id = seen_ids.get(fixture_id)
            if previous_id is not None:
                raise SystemExit(
                    f"{prompt_id}: fixture id {fixture_id!r} is used in both {previous_id} and {set_name}"
                )
            seen_ids[fixture_id] = set_name
            text = fixture["payload"].strip()
            previous_text = seen_text.get(text)
            if previous_text is not None:
                raise SystemExit(
                    f"{prompt_id}: {fixture_id} and {previous_text} have the same text after trimming. "
                    "The report would score them as one case."
                )
            seen_text[text] = fixture_id


def jsonl_row_count(path: Path) -> int:
    try:
        text = path.read_text()
    except OSError as err:
        raise SystemExit(f"{path}: {err}") from err
    return max(sum(1 for line in text.splitlines() if line.strip()), 1)


def check_eval_jobs(prompt_dir: Path) -> None:
    jobs = prompt_dir / "eval-jobs"
    if not jobs.is_dir():
        return
    for path in sorted(jobs.glob("*.json")):
        if ".local." in path.name:
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            raise SystemExit(f"{prompt_dir.name}/eval-jobs/{path.name} is not valid JSON: {err}") from err
        metrics = (
            data.get("evaluationConfig", {})
            .get("automated", {})
            .get("customMetricConfig", {})
            .get("customMetrics", [])
        )
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            definition = metric.get("customMetricDefinition") or {}
            if not isinstance(definition, dict):
                continue
            name = definition.get("name") or path.name
            for scale in definition.get("ratingScale") or []:
                if not isinstance(scale, dict):
                    continue
                text = scale.get("definition", "")
                if isinstance(text, str) and len(text) > RATING_DEFINITION_LIMIT:
                    raise SystemExit(
                        f"{prompt_dir.name}/eval-jobs/{path.name}: {name} ratingScale definition is "
                        f"{len(text)} characters (max {RATING_DEFINITION_LIMIT})"
                    )


def render_prompt(prompt_dir: Path, check_only: bool) -> int:
    """Rebuild one prompt's datasets from its fixtures. Returns the number of stale rows."""
    system_prompt = read_prompt_text(prompt_dir, "system-prompt.txt")
    template = read_prompt_text(prompt_dir, "user-message-template.txt")
    variable = input_variable_name(template, prompt_dir.name)
    sets = fixture_sets(prompt_dir)
    loaded = [(set_dir.name, load_set(set_dir, variable, prompt_dir.name)) for set_dir in sets]
    assert_unique_fixtures(prompt_dir.name, loaded)
    check_eval_jobs(prompt_dir)

    datasets_dir = prompt_dir / "datasets"
    expected_files = {f"{set_dir.name}.jsonl" for set_dir in sets}
    extras = []
    if datasets_dir.is_dir():
        extras = sorted(p for p in datasets_dir.glob("*.jsonl") if p.name not in expected_files)

    stale = 0
    print(f"{prompt_dir.name}:")
    if not check_only:
        datasets_dir.mkdir(parents=True, exist_ok=True)

    for set_dir, (_, fixtures) in zip(sets, loaded):
        rows = render_rows(system_prompt, template, variable, set_dir.name, fixtures)
        text = serialise(rows)
        path = datasets_dir / f"{set_dir.name}.jsonl"
        previous = path.read_text() if path.is_file() else None
        changed = changed_rows(previous, rows, text)
        stale += changed
        if check_only:
            state = f"{changed} row(s) stale" if changed else "up to date"
            print(f"  {set_dir.name:12} {len(rows)} rows — {state}")
            continue
        path.write_text(text)
        print(f"  {set_dir.name:12} {len(rows)} rows written — {changed} changed")

    if extras:
        for path in extras:
            count = jsonl_row_count(path)
            if check_only:
                stale += count
                print(f"  {path.name:12} {count} row(s) with no fixture set")
            else:
                path.unlink()
                print(f"  removed {path.name} ({count} row(s) with no fixture set)")

    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "prompts",
        nargs="*",
        help="Prompt directory names under prompts/. Default: every prompt.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if datasets/*.jsonl do not match fixtures and the current prompt text. Writes nothing.",
    )
    args = parser.parse_args(argv)

    stale = sum(render_prompt(prompt_dir, args.check) for prompt_dir in prompt_dirs(args.prompts))

    if args.check and stale:
        print(
            f"\n{stale} row(s) do not match fixtures and the current system-prompt.txt / user-message-template.txt. "
            "Run without --check, then redeploy."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
