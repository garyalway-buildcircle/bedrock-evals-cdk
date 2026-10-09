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
  --region us-east-1 \
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
- `<JOB_NAME>` — ≤63 characters, unique in the account (case-insensitive). Append a unix
  timestamp; keep the prefix short (`example-edge-<ts>`). The tracked template keeps the
  placeholder so it cannot be submitted twice.

Run jobs in `us-east-1` against Claude Sonnet 4.6. Never submit two at once — they throttle
each other (`docs/setup.md` point 6).

```bash
aws bedrock create-evaluation-job --region us-east-1 --cli-input-json file://prompts/$P/eval-jobs/golden-job.local.json
aws bedrock get-evaluation-job --region us-east-1 --job-identifier <jobArn>
aws bedrock create-evaluation-job --region us-east-1 --cli-input-json file://prompts/$P/eval-jobs/edge-case-job.local.json
```

Per-row results land at
`<prefix>/<job-name>/<job-id>/models/.../<uuid>_output.jsonl`. Read scores by `metricName`.
A row passes only when every metric is `Pass`. You need `s3:ListBucket`/`s3:GetObject` on the
output bucket (`iam/steady-state-policy.json`, `ReadEvaluationResults`).

## Report

```bash
python3 scripts/build-eval-report.py <job-arn> [<job-arn> ...] [--prompt $P]
```

The script reads the region from each job ARN and passes `--region` on every AWS call, so a
second region does not depend on the CLI default. Default output:
`reports/<prompt>-eval-report.html` (gitignored, opens in the browser). Pass several ARNs to
compare runs. Pass `--prompt` for anything other than `example`.

Both S3 buckets expire objects after seven days. Build the report before then.
Redeploy if the datasets have expired.

## What constitutes a pass

- Golden and edge-case: every row `Pass` on every metric that job defines.
- A `Fail` on a fixture that tries to override the prompt is a security regression. In the example
  prompt those fixtures are `edge-08-prompt-injection` and `edge-09-spoofed-label`.
- Metrics live in that prompt's `eval-jobs/*.json`. Do not reuse another prompt's.
- Each `ratingScale[].definition` must stay under 100 characters or Bedrock rejects the job
  with `ValidationException` (not in the CLI help). `render-datasets.py` checks the same limit.
  Put the detail in `instructions`.

## Validating a prompt change

Jobs do not call the Bedrock Prompt resource. Each JSONL row's `prompt` field is what the
model sees.

1. Edit `prompts/$P/system-prompt.txt`, `user-message-template.txt`, or a fixture. The fixture
   field named by the template's `{{variable}}` is the untrusted input. `expected` is the reference.
2. `python3 scripts/render-datasets.py $P` rebuilds `datasets/*.jsonl` from `fixtures/` and writes
   `prompt.json` `temperature` and `maxTokens` into each `eval-jobs/*.json` as `inferenceParams`.
   Refresh the `.local.json` copy before you submit it. `--check` exits non-zero if a dataset file
   is not byte-for-byte what the fixtures and the current prompt text would write, or if those
   inference parameters differ, and it writes nothing. Any other flag, including a typo, is an error.
3. `AWS_REGION=us-east-1 npx cdk deploy -c modelUnderTestId=... -c nameSuffix=-use1` — synth runs that `--check` first
   and refuses a stale dataset. `cdk watch` does the same. Deploy uploads `datasets/<prompt>/`
   and updates every Prompt resource. The resource's user message is the dataset `prompt` string
   with the `{{variable}}` left in. Its `temperature` and `maxTokens` come from `prompt.json`,
   the same values the job template's `inferenceParams` carry.
4. Re-run both jobs, then build the report.
5. Golden Fail: prompt regression or a bad fixture. Fix the fixture only if its expectation
   is wrong, and say why in its optional `notes` field. The report shows that field when it is set.
6. Edge-case Fail: blocking. Do not ship until it passes.

## Tear down

```bash
AWS_REGION=us-east-1 npx cdk destroy -c nameSuffix=-use1
```

That matches the deploy command above. A stack deployed without a suffix is destroyed without
`-c nameSuffix`.

`cdk destroy` does not remove the per-region `CDKToolkit` bootstrap stack or Bedrock
evaluation-job records. Bucket contents go with the buckets.
