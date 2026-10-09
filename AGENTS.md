# bedrock-evals-cdk

## Architecture

This repo is where prompts are written and measured. Bedrock evaluation jobs score them.
`prompts/<prompt>/*.txt` is the source of truth for prompt text. The CDK stack discovers
directories under `prompts/` and does not implement evaluation logic.

## Target model

Inference-profile IDs only, for example `us.anthropic.claude-sonnet-4-6`, not the bare ID.
Judge: a stronger model than the one under test, for example `us.anthropic.claude-opus-4-6-v1`.

## Regions

**Run evals in us-east-1** (`-c nameSuffix=-use1`). A smaller region can throttle short
Anthropic jobs.

`cdk deploy`/`destroy` against us-east-1 needs `-c nameSuffix=-use1` and `AWS_REGION`.
Stack `BedrockPromptEvals`; buckets/role `bedrock-prompt-evals-{dataset,output,job-role}*`;
IAM policy `bedrock-prompt-evals-steady-state`. The user template owns the input variable
name; do not hardcode it in the stack or renderer.

## Prompts

One prompt per directory under `prompts/`. The stack discovers them — adding one is a new
directory, never a stack edit. Layout: `docs/adding-a-prompt.md`.

`prompts/example` is a placeholder. Replace it, or add another directory beside it.

Do not call these "tasks": Bedrock already uses `taskType` for something else.

Prompt-specific docs live with that prompt. Repo-level `docs/` is setup, runbook, and adding a
prompt only.

## Iterating

Edit the prompt text or a fixture → `python3 scripts/render-datasets.py <prompt>` → redeploy → run both
sets **one at a time** → `scripts/build-eval-report.py --prompt <prompt>`. Skipping the
re-render scores the previous wording. Full loop: `docs/runbook.md`.

Worked examples in a prompt must use content that appears in no fixture.

## Conventions

- `iam/*.json` and `prompts/*/eval-jobs/*.json` are templates; `*.local.json` is gitignored.
- `raw-data/` is gitignored.
- Docs are reference material, not a changelog. No session narrative, no job IDs.
