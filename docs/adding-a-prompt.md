# Adding a prompt

One directory under `prompts/`. The stack discovers it at synth time. The only repo-level edit
is a row in the root `README.md` table.

```
prompts/<prompt>/
  readme.md                    what this prompt returns
  prompt.json                  description (required); optional promptName, temperature, maxTokens
  system-prompt.txt
  user-message-template.txt    one {{variable}} — this prompt's untrusted input
  docs/*.md                    the rules in plain language
  datasets/<set>.jsonl         what the evaluation job reads
  fixtures/<set>/*.json        the same cases, readable
  fixtures/<set>/index.json    what the set covers; the report uses this as the section lead
  eval-jobs/<set>-job.json     create-evaluation-job template
```

`<prompt>` is also the S3 prefix: `datasets/<prompt>/` and `results/<prompt>/`.

1. `mkdir -p prompts/<prompt>/{docs,datasets,fixtures,eval-jobs}` and write `prompt.json`.
   `description` is required and must be at most 200 characters. `temperature` defaults to `0` and `maxTokens` to `4000` when omitted.
   `maxTokens` must be from 1 to 200000. Set them in `prompt.json` when this prompt needs different
   inference settings. `render-datasets.py` copies both into each eval-job template as
   `inferenceParams` (`{"inferenceConfig":{"maxTokens":…,"temperature":…}}`, the object a model-evaluation job reads; do not also set `topP`). `promptName` must be unique across `prompts/`, and it must be 1–100 letters,
   digits, single hyphens, or single underscores.
2. Write the two `.txt` files. The user template has exactly one occurrence of one `{{variable}}`
   (name it for the input: `note`, `email`, …). The stack and render-datasets read that name from
   the template. `system-prompt.txt` must not contain a `{{variable}}`: the deployed user message
   is the contents of that file, one newline, then the template with trailing newlines removed.
   A trailing newline already in `system-prompt.txt` is what shows up as a blank line. An evaluation
   job sends that same string with the variable filled in.
3. Write fixtures. Each one has a unique `id`, an `expected` object, and a string field whose name
   is the template variable. Optional `notes` records why an expectation changed; the report shows
   it. The input text must be unique after trimming, and it must not contain
   `<<<UNTRUSTED_CONTENT>>>` or `<<<END_UNTRUSTED_CONTENT>>>`. `fixtures/<set>/index.json` lists
   every fixture file in that set, in row order.
4. `python3 scripts/render-datasets.py <prompt>` writes `datasets/<set>.jsonl` from those fixtures.
   `--check` later fails if the JSONL has drifted. Do not hand-edit the JSONL.
5. Copy `prompts/example/eval-jobs/*.json` and rewrite the judge metrics for this
   output schema. `ratingScale` values are the strings `Pass` and `Fail`, each once.
   `ratingScale[].definition` must stay under 100 characters (`docs/runbook.md`).
   Name the file `<set>-job.json`. Its dataset URI is exactly
   `s3://<DATASET_BUCKET_NAME>/datasets/<prompt>/<set>.jsonl` and its output URI is exactly
   `s3://<OUTPUT_BUCKET_NAME>/results/<prompt>/<set>/`. The tracked file also keeps
   `<EVAL_JOB_ROLE_ARN>`, `<JOB_NAME>`, `<MODEL_UNDER_TEST_ID>`, and `<JUDGE_MODEL_ID>`.
   The `.local.json` copy fills those in. `jobName` there must be short enough for a timestamp
   (63-character Bedrock limit). Each metric's `instructions` must contain `{{prompt}}`,
   `{{prediction}}`, and `{{ground_truth}}`.
6. `AWS_REGION=us-east-1 npx cdk deploy -c modelUnderTestId=... -c nameSuffix=-use1`, run both jobs, build the report
   with `--prompt <prompt>`.
7. Write `readme.md` and add the table row in the root `README.md`.

Judge metrics are per-prompt. The report reads metric names from the results and section leads
from `index.json`, so new names need no script change. Keep the `golden` / `edge-case` fixture
directory names to get the existing report layout.
