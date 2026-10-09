#!/usr/bin/env python3
"""Re-render datasets/*.jsonl after a prompt edit.

Jobs send each row's `prompt` field to the model; they do not call the Bedrock Prompt
resource. This swaps the system-prompt prefix and leaves the untrusted payload alone. It
does not rebuild payloads from fixtures/ (source format vs the normalised dataset form).

    python3 scripts/render-datasets.py                 # every prompt
    python3 scripts/render-datasets.py example
    python3 scripts/render-datasets.py --check
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "prompts"
OPEN_WRAP = "\n<<<UNTRUSTED_CONTENT>>>\n"
CLOSE_WRAP = "\n<<<END_UNTRUSTED_CONTENT>>>"
# Keep in sync with lib/eval-harness-stack.ts inputVariableNames.
INPUT_VARIABLE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]*)\}\}")


def extract_untrusted(prompt: str, fixture_id: str) -> str:
    """Pull the untrusted payload out from between the wrap markers.

    The system prompt's own last line names both markers inline, so anchor on the newline-delimited
    form and take the last occurrence — that is always the real wrap, never the prose mention.
    """
    start = prompt.rfind(OPEN_WRAP)
    if start == -1:
        raise SystemExit(f"{fixture_id}: no opening wrap marker found")
    if not prompt.endswith(CLOSE_WRAP):
        raise SystemExit(f"{fixture_id}: prompt does not end with the closing wrap marker")
    return prompt[start + len(OPEN_WRAP) : -len(CLOSE_WRAP)]


def input_variable_name(template: str, prompt_id: str) -> str:
    names = list(dict.fromkeys(INPUT_VARIABLE.findall(template)))
    if not names:
        raise SystemExit(f"{prompt_id}: user-message-template.txt has no {{variable}} placeholder")
    if len(names) > 1:
        raise SystemExit(
            f"{prompt_id}: user-message-template.txt has multiple placeholders ({', '.join(names)}); one untrusted input only"
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


def render_prompt(prompt_dir: Path, check_only: bool) -> int:
    """Re-render one prompt's datasets. Returns the number of stale/changed rows."""
    system_prompt = (prompt_dir / "system-prompt.txt").read_text()
    template = (prompt_dir / "user-message-template.txt").read_text()
    variable = input_variable_name(template, prompt_dir.name)
    placeholder = "{{" + variable + "}}"

    paths = sorted((prompt_dir / "datasets").glob("*.jsonl"))
    if not paths:
        # An empty glob is indistinguishable from a clean run in the output, so --check would
        # exit 0 having verified nothing.
        raise SystemExit(f"{prompt_dir.name}: no datasets/*.jsonl found")

    stale = 0
    print(f"{prompt_dir.name}:")
    for path in paths:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

        rendered = []
        changed = 0
        for row in rows:
            payload = extract_untrusted(row["prompt"], row["fixtureId"])
            body = template.replace(placeholder, payload).rstrip("\n")
            new_prompt = system_prompt + "\n" + body
            if new_prompt != row["prompt"]:
                changed += 1
            row["prompt"] = new_prompt
            rendered.append(row)

        stale += changed
        if check_only:
            state = f"{changed} row(s) stale" if changed else "up to date"
            print(f"  {path.stem:12} {len(rows)} rows — {state}")
            continue

        # ensure_ascii=True matches how these files are already serialised — leaving it off
        # rewrites every non-ASCII character and produces a diff on rows that did not change.
        path.write_text("".join(json.dumps(r) + "\n" for r in rendered))
        print(f"  {path.stem:12} {len(rows)} rows written — {changed} changed")

    return stale


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    check_only = "--check" in sys.argv

    stale = sum(render_prompt(prompt_dir, check_only) for prompt_dir in prompt_dirs(args))

    if check_only and stale:
        print(f"\n{stale} row(s) do not match their system-prompt.txt. Run without --check, then redeploy.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
