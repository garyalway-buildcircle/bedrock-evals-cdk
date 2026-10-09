# Evaluation runbook

## Deploy

```bash
npm install
AWS_REGION=us-east-1 npx cdk deploy \
  -c modelUnderTestId=us.anthropic.claude-sonnet-4-6 \
  -c nameSuffix=-use1
```

Evals run in us-east-1. A different region or an unsuffixed stack is a different deployment —
see `docs/setup.md`. Capture the Outputs
(`DatasetBucketName`, `OutputBucketName`, `EvalJobRoleArn`, `PromptArn<Name>` per prompt), or:

```bash
aws cloudformation describe-stacks --stack-name BedrockPromptEvals \
  --query "Stacks[0].Outputs" --output table
```

## Run an evaluation

Jobs are not a CloudFormation resource. `$P` is a directory under `prompts/`.

Templates in git keep `<PLACEHOLDER>` tokens. Copy them to gitignored `*.local.json` and fill
in real values there:

```bash
P=example
cp prompts/$P/eval-jobs/golden-job.json prompts/$P/eval-jobs/golden-job.local.json
cp prompts/$P/eval-jobs/edge-case-job.json prompts/$P/eval-jobs/edge-case-job.local.json
```

Replace:

- `<EVAL_JOB_ROLE_ARN>`, `<DATASET_BUCKET_NAME>`, `<OUTPUT_BUCKET_NAME>` — stack Outputs.
- `<MODEL_UNDER_TEST_ID>` — same ID as `-c modelUnderTestId=...` at deploy. See
  `docs/setup.md` step 4; listed ≠ usable.
- `<JUDGE_MODEL_ID>` — a stronger model than the one under test. Bare foundation-model ID or
  system profile only, not an application-inference-profile ARN (`docs/setup.md` step 4).
- `jobName` — ≤63 characters, unique in the account (case-insensitive). Append a unix
  timestamp; keep the prefix short (`example-edge-<ts>`).

Run jobs in `us-east-1` against Claude Sonnet 4.6. Never submit two at once — they throttle
each other (`docs/setup.md` point 6).

```bash
aws bedrock create-evaluation-job --cli-input-json file://prompts/$P/eval-jobs/golden-job.local.json
aws bedrock get-evaluation-job --job-identifier <jobArn>
aws bedrock create-evaluation-job --cli-input-json file://prompts/$P/eval-jobs/edge-case-job.local.json
```

Per-row results land at
`<prefix>/<job-name>/<job-id>/models/.../<uuid>_output.jsonl`. Read scores by `metricName`.
A row passes only when every metric is `Pass`. You need `s3:ListBucket`/`s3:GetObject` on the
output bucket (`iam/steady-state-policy.json`, `ReadEvaluationResults`).

## Report

```bash
python3 scripts/build-eval-report.py <job-arn> [<job-arn> ...] [--prompt $P]
```

Default output: `reports/<prompt>-eval-report.html` (gitignored, opens in the browser). Pass
several ARNs to compare runs. Pass `--prompt` for anything other than `example`.

Both S3 buckets expire objects after seven days. Build the report before then.
Redeploy if the datasets have expired.

## What constitutes a pass

- Golden and edge-case: every row `Pass` on every metric that job defines.
- Injection rows (`*-08`, `*-09`): a `Fail` is a security regression.
- Metrics live in that prompt's `eval-jobs/*.json`. Do not reuse another prompt's.
- Each `ratingScale[].definition` must stay under 100 characters or Bedrock rejects the job
  with `ValidationException` (not in the CLI help). `render-datasets.py` checks the same limit.
  Put the detail in `instructions`.

## Validating a prompt change

Jobs do not call the Bedrock Prompt resource. Each JSONL row's `prompt` field is what the
model sees.

1. Edit `prompts/$P/system-prompt.txt`, `user-message-template.txt`, or a fixture. The fixture
   field named by the template's `{{variable}}` is the untrusted input. `expected` is the reference.
2. `python3 scripts/render-datasets.py $P` rebuilds `datasets/*.jsonl` from `fixtures/`.
   `--check` exits non-zero if a dataset file is not byte-for-byte what the fixtures and the
   current prompt text would write, and it writes nothing. Any other flag, including a typo, is an error.
3. `npx cdk deploy -c modelUnderTestId=... -c nameSuffix=-use1` — synth runs that `--check` first
   and refuses a stale dataset. `cdk watch` does the same. Deploy uploads `datasets/<prompt>/`
   and updates every Prompt resource. `temperature` and `maxTokens` come from that prompt's
   `prompt.json`.
4. Re-run both jobs, then build the report.
5. Golden Fail: prompt regression or a bad fixture. Fix the fixture only if its expectation
   is wrong, and say why in `notes`.
6. Edge-case Fail: blocking. Do not ship until it passes.

## Tear down

```bash
AWS_REGION=<region> npx cdk destroy
```

A stack deployed with `-c nameSuffix=<suffix>` must be destroyed with the same context and
region (`AWS_REGION=us-east-1 npx cdk destroy -c nameSuffix=-use1`). Omitting the suffix
targets a different deployment.

`cdk destroy` does not remove the per-region `CDKToolkit` bootstrap stack or Bedrock
evaluation-job records. Bucket contents go with the buckets.
