# Bedrock Prompt Evals

A prompt evaluation harness, built on Amazon Bedrock's native Prompt Management and Evaluation Jobs.

No evaluation logic is implemented here — the CDK stack is the only code needed to deploy and run this, the prompts and datasets are plain data files, and running an evaluation is a single AWS CLI call. The one deliberate exception is `scripts/build-eval-report.py`, a report generator for
results Bedrock already produced; it doesn't touch the evaluation itself.

## Prompts

| Prompt                                         | Description                                      | Output                          |
| ---------------------------------------------- | ------------------------------------------------ | ------------------------------- |
| `[example](prompts/example/readme.md)`         | Placeholder. Replace it with your own prompt.    | `positive` / `negative` / `neutral` label |


Each prompt owns its text, fixtures, datasets and judge metrics. The CDK stack discovers
directories under `prompts/`. Layout and how to add one: `docs/adding-a-prompt.md`. First
deploy: `docs/setup.md`. Day-to-day evals: `docs/runbook.md`. After a prompt edit, re-render
that prompt's `datasets/*.jsonl` before redeploying.

`prompts/example` is dummy content so the layout is visible. It is not a real evaluation.

## Quick start

```bash
npm install
AWS_REGION=us-east-1 npx cdk deploy \
  -c modelUnderTestId=us.anthropic.claude-sonnet-4-6 \
  -c nameSuffix=-use1
```
