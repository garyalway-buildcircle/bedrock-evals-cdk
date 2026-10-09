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
# Keep in sync with scripts/build-eval-report.py. The newline-delimited form is the real wrap.
OPEN_WRAP = "\n<<<UNTRUSTED_CONTENT>>>\n"
CLOSE_WRAP = "\n<<<END_UNTRUSTED_CONTENT>>>"
RATING_DEFINITION_LIMIT = 100
DESCRIPTION_LIMIT = 200
DEFAULT_TEMPERATURE = 0
DEFAULT_MAX_TOKENS = 4000
MAX_MAX_TOKENS = 200_000
MODEL_PLACEHOLDER = "<MODEL_UNDER_TEST_ID>"
JOB_NAME_PLACEHOLDER = "<JOB_NAME>"
# CreateEvaluationJob JobName: 1-63 chars, and this pattern. Length is checked separately
# because a group in the pattern can be more than one character.
JOB_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
JUDGE_PLACEHOLDERS = ("{{prompt}}", "{{prediction}}", "{{ground_truth}}")


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


def assert_no_system_placeholders(prompt_id: str, system_prompt: str) -> None:
    """The deployed user message is this file plus the template, so a {{name}} here is a real input."""
    names = INPUT_VARIABLE.findall(system_prompt)
    if names:
        raise SystemExit(
            f"{prompt_id}: system-prompt.txt contains a placeholder ({', '.join(names)}). "
            "The deployed user message includes this file, so the only placeholder belongs in the user template."
        )


def assert_single_wrap(prompt_id: str, system_prompt: str, template: str, variable: str) -> None:
    """The report splits on the newline-delimited wrap. It has to occur once, in the user template."""
    if OPEN_WRAP in system_prompt or CLOSE_WRAP in system_prompt:
        raise SystemExit(
            f"{prompt_id}: system-prompt.txt contains the newline-delimited wrap. "
            "Mention the markers inline, or the report cannot find the input."
        )
    sample = assemble_prompt(system_prompt, template, "{{" + variable + "}}", "payload")
    if sample.count(OPEN_WRAP) != 1 or sample.count(CLOSE_WRAP) != 1 or not sample.endswith(CLOSE_WRAP):
        raise SystemExit(
            f"{prompt_id}: the rendered prompt must contain the newline-delimited wrap exactly once"
        )


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
                where = (
                    f"more than once in {set_name}"
                    if previous_id == set_name
                    else f"in both {previous_id} and {set_name}"
                )
                raise SystemExit(f"{prompt_id}: fixture id {fixture_id!r} is used {where}")
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


def job_files(prompt_dir: Path) -> list[Path]:
    jobs = prompt_dir / "eval-jobs"
    if not jobs.is_dir():
        return []
    return sorted(jobs.glob("*.json"))


def load_job(prompt_dir: Path, path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise SystemExit(f"{prompt_dir.name}/eval-jobs/{path.name} is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise SystemExit(f"{prompt_dir.name}/eval-jobs/{path.name} must be a JSON object")
    return data


def inference_settings(prompt_dir: Path) -> tuple[int | float, int]:
    """temperature and maxTokens from prompt.json. Defaults match lib/prompts.ts."""
    path = prompt_dir / "prompt.json"
    if not path.is_file():
        raise SystemExit(f"{prompt_dir.name}/prompt.json is missing")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise SystemExit(f"{prompt_dir.name}/prompt.json is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise SystemExit(f"{prompt_dir.name}/prompt.json must be a JSON object")
    temperature = data.get("temperature", DEFAULT_TEMPERATURE)
    max_tokens = data.get("maxTokens", DEFAULT_MAX_TOKENS)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 1:
        raise SystemExit(f"{prompt_dir.name}/prompt.json temperature must be a number from 0 to 1")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_MAX_TOKENS:
        raise SystemExit(
            f"{prompt_dir.name}/prompt.json maxTokens must be an integer from 1 to {MAX_MAX_TOKENS}"
        )
    return temperature, max_tokens


def inference_params(temperature: int | float, max_tokens: int) -> str:
    """What a model-evaluation job reads from inferenceParams.

    The model-evaluation user guide's Claude example uses inferenceConfig.maxTokens and
    inferenceConfig.temperature (and omits topP; Anthropic rejects temperature and top_p together).
    This is not the raw Anthropic InvokeModel body.
    """
    return json.dumps(
        {"inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature}},
        separators=(",", ":"),
    )


def write_inference_params(path: Path, model_identifier: str, current: object, expected: str) -> None:
    """Replace or insert only the inferenceParams string. Leave the rest of the file as it is."""
    text = path.read_text()
    replacement = '"inferenceParams": ' + json.dumps(expected)
    if isinstance(current, str):
        pattern = re.compile(r'"inferenceParams"\s*:\s*' + re.escape(json.dumps(current)))
        match = pattern.search(text)
        if not match:
            raise SystemExit(
                f"{path.name}: inferenceParams is present but not a plain JSON string, so it was not rewritten"
            )
        updated = text[: match.start()] + replacement + text[match.end() :]
    else:
        encoded_id = json.dumps(model_identifier)
        ident = re.compile(r'("modelIdentifier"\s*:\s*' + re.escape(encoded_id) + r")")
        bedrock_at = text.find('"bedrockModel"')
        match = None
        for candidate in ident.finditer(text):
            if bedrock_at == -1 or candidate.start() > bedrock_at:
                match = candidate
                break
        if match is None:
            raise SystemExit(f"{path.name}: cannot insert inferenceParams next to modelIdentifier")
        updated = text[: match.end()] + ", " + replacement + text[match.end() :]
    if not updated.endswith("\n"):
        updated += "\n"
    path.write_text(updated)


def sync_inference_params(prompt_dir: Path, check_only: bool) -> None:
    """Job templates score with prompt.json's temperature and maxTokens, or --check fails."""
    files = job_files(prompt_dir)
    if not files:
        return
    expected = inference_params(*inference_settings(prompt_dir))
    for path in files:
        data = load_job(prompt_dir, path)
        models = data.get("inferenceConfig", {}).get("models") if isinstance(data.get("inferenceConfig"), dict) else None
        bedrock_model = models[0].get("bedrockModel") if isinstance(models, list) and models and isinstance(models[0], dict) else None
        rel = f"{prompt_dir.name}/eval-jobs/{path.name}"
        if not isinstance(bedrock_model, dict):
            raise SystemExit(f"{rel}: inferenceConfig.models[0].bedrockModel is missing")
        current = bedrock_model.get("inferenceParams")
        if current == expected:
            continue
        if check_only:
            raise SystemExit(
                f"{rel}: inferenceParams must be {expected} (prompt.json temperature and maxTokens). "
                "Run without --check, then refresh the .local.json copy you submit."
            )
        if current is not None and not isinstance(current, str):
            raise SystemExit(f"{rel}: inferenceParams must be a string")
        model_identifier = bedrock_model.get("modelIdentifier")
        if not isinstance(model_identifier, str):
            raise SystemExit(f"{rel}: modelIdentifier must be a string")
        write_inference_params(path, model_identifier, current if isinstance(current, str) else None, expected)
        print(f"  updated {path.name} inferenceParams")


def check_prompt_description(prompt_dir: Path) -> None:
    path = prompt_dir / "prompt.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise SystemExit(f"{prompt_dir.name}/prompt.json is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise SystemExit(f"{prompt_dir.name}/prompt.json must be a JSON object")
    description = data.get("description")
    if isinstance(description, str) and len(description) > DESCRIPTION_LIMIT:
        raise SystemExit(
            f"{prompt_dir.name}/prompt.json description is {len(description)} characters (max {DESCRIPTION_LIMIT})"
        )


def s3_uri(value: object) -> str:
    return value if isinstance(value, str) else ""


def check_job_contract(prompt_dir: Path, path: Path, data: dict) -> None:
    """Tracked templates stay unsubmittable. A .local.json must be ready to submit."""
    rel = f"{prompt_dir.name}/eval-jobs/{path.name}"
    local = ".local." in path.name
    prompt_id = prompt_dir.name

    job_name = data.get("jobName")
    if local:
        if (
            not isinstance(job_name, str)
            or not job_name
            or len(job_name) > 63
            or "<" in job_name
            or JOB_NAME_PATTERN.fullmatch(job_name) is None
        ):
            raise SystemExit(
                f"{rel}: jobName must be 1-63 characters matching {JOB_NAME_PATTERN.pattern}, with no placeholders"
            )
    elif job_name != JOB_NAME_PLACEHOLDER:
        raise SystemExit(
            f"{rel}: jobName must be {JOB_NAME_PLACEHOLDER}. "
            "Copy the file to *.local.json and set a unique name there."
        )

    models = data.get("inferenceConfig", {}).get("models") if isinstance(data.get("inferenceConfig"), dict) else None
    bedrock_model = models[0].get("bedrockModel") if isinstance(models, list) and models and isinstance(models[0], dict) else None
    model_id = bedrock_model.get("modelIdentifier") if isinstance(bedrock_model, dict) else None
    if local:
        if not isinstance(model_id, str) or not model_id or "<" in model_id:
            raise SystemExit(f"{rel}: modelIdentifier must be the real model id, not a placeholder")
    elif model_id != MODEL_PLACEHOLDER:
        raise SystemExit(f"{rel}: modelIdentifier must stay {MODEL_PLACEHOLDER}")

    configs = (
        data.get("evaluationConfig", {}).get("automated", {}).get("datasetMetricConfigs", [])
        if isinstance(data.get("evaluationConfig"), dict)
        else []
    )
    dataset_uris = []
    if isinstance(configs, list):
        for config in configs:
            if not isinstance(config, dict):
                continue
            dataset = config.get("dataset") if isinstance(config.get("dataset"), dict) else {}
            location = dataset.get("datasetLocation") if isinstance(dataset.get("datasetLocation"), dict) else {}
            dataset_uris.append(s3_uri(location.get("s3Uri")))
    if not any(f"/datasets/{prompt_id}/" in uri for uri in dataset_uris):
        raise SystemExit(f"{rel}: dataset s3Uri must contain /datasets/{prompt_id}/")

    output = data.get("outputDataConfig") if isinstance(data.get("outputDataConfig"), dict) else {}
    if f"/results/{prompt_id}/" not in s3_uri(output.get("s3Uri")):
        raise SystemExit(f"{rel}: output s3Uri must contain /results/{prompt_id}/")

    metrics = (
        data.get("evaluationConfig", {}).get("automated", {}).get("customMetricConfig", {}).get("customMetrics", [])
        if isinstance(data.get("evaluationConfig"), dict)
        else []
    )
    instructions = ""
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            definition = metric.get("customMetricDefinition")
            text = definition.get("instructions") if isinstance(definition, dict) else None
            if isinstance(text, str):
                instructions += "\n" + text
    missing = [token for token in JUDGE_PLACEHOLDERS if token not in instructions]
    if missing:
        raise SystemExit(f"{rel}: judge instructions must contain {', '.join(missing)}")


def check_eval_jobs(prompt_dir: Path) -> None:
    for path in job_files(prompt_dir):
        data = load_job(prompt_dir, path)
        metrics = (
            data.get("evaluationConfig", {})
            .get("automated", {})
            .get("customMetricConfig", {})
            .get("customMetrics", [])
        )
        if isinstance(metrics, list):
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                definition = metric.get("customMetricDefinition") or {}
                if not isinstance(definition, dict):
                    continue
                name = definition.get("name") or path.name
                scales = definition.get("ratingScale")
                if not isinstance(scales, list) or not scales:
                    raise SystemExit(
                        f"{prompt_dir.name}/eval-jobs/{path.name}: {name} ratingScale must list Pass and Fail"
                    )
                values = []
                for scale in scales:
                    if not isinstance(scale, dict):
                        continue
                    text = scale.get("definition", "")
                    if isinstance(text, str) and len(text) > RATING_DEFINITION_LIMIT:
                        raise SystemExit(
                            f"{prompt_dir.name}/eval-jobs/{path.name}: {name} ratingScale definition is "
                            f"{len(text)} characters (max {RATING_DEFINITION_LIMIT})"
                        )
                    value = scale.get("value")
                    string_value = value.get("stringValue") if isinstance(value, dict) else None
                    if string_value not in ("Pass", "Fail"):
                        raise SystemExit(
                            f"{prompt_dir.name}/eval-jobs/{path.name}: {name} ratingScale values must be the strings Pass and Fail"
                        )
                    values.append(string_value)
                if sorted(values) != ["Fail", "Pass"]:
                    raise SystemExit(
                        f"{prompt_dir.name}/eval-jobs/{path.name}: {name} ratingScale must contain Pass and Fail exactly once"
                    )
        check_job_contract(prompt_dir, path, data)


def render_prompt(prompt_dir: Path, check_only: bool) -> int:
    """Rebuild one prompt's datasets from its fixtures. Returns the number of stale rows."""
    check_prompt_description(prompt_dir)
    system_prompt = read_prompt_text(prompt_dir, "system-prompt.txt")
    template = read_prompt_text(prompt_dir, "user-message-template.txt")
    variable = input_variable_name(template, prompt_dir.name)
    assert_no_system_placeholders(prompt_dir.name, system_prompt)
    assert_single_wrap(prompt_dir.name, system_prompt, template, variable)
    sets = fixture_sets(prompt_dir)
    loaded = [(set_dir.name, load_set(set_dir, variable, prompt_dir.name)) for set_dir in sets]
    assert_unique_fixtures(prompt_dir.name, loaded)
    check_eval_jobs(prompt_dir)
    sync_inference_params(prompt_dir, check_only)

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
        help="Exit non-zero if datasets/*.jsonl or eval-job inferenceParams do not match the fixtures and prompt.json. Writes nothing.",
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
