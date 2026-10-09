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
   `description` is required. `temperature` defaults to `0` and `maxTokens` to `4000` when omitted.
   `maxTokens` must be from 1 to 200000. Set them in `prompt.json` when this prompt needs different
   inference settings. `render-datasets.py` copies both into each eval-job template as
   `inferenceParams`. `promptName` must be unique across `prompts/`, and it must be 1–100 letters,
   digits, single hyphens, or single underscores.
2. Write the two `.txt` files. The user template has exactly one occurrence of one `{{variable}}`
   (name it for the input: `note`, `email`, …). The stack and render-datasets read that name from
   the template. The deployed prompt's user message is the system prompt, a blank line, then the
   template. An evaluation job sends that same string with the variable filled in.
3. Write fixtures. Each one has a unique `id`, an `expected` object, and a string field whose name
   is the template variable. That text must be unique after trimming, and it must not contain
   `<<<UNTRUSTED_CONTENT>>>` or `<<<END_UNTRUSTED_CONTENT>>>`. `fixtures/<set>/index.json` lists
   every fixture file in that set, in row order.
4. `python3 scripts/render-datasets.py <prompt>` writes `datasets/<set>.jsonl` from those fixtures.
   `--check` later fails if the JSONL has drifted. Do not hand-edit the JSONL.
5. Copy `prompts/example/eval-jobs/*.json` and rewrite the judge metrics for this
   output schema. `ratingScale` values are the strings `Pass` and `Fail`, each once.
   `ratingScale[].definition` must stay under 100 characters (`docs/runbook.md`).
   Point the S3 URIs at `datasets/<prompt>/<set>.jsonl` and `results/<prompt>/<set>/`. Keep
   `jobName` short enough for a timestamp (63-character Bedrock limit).
6. `npx cdk deploy -c modelUnderTestId=... -c nameSuffix=-use1`, run both jobs, build the report
   with `--prompt <prompt>`.
7. Write `readme.md` and add the table row in the root `README.md`.

Judge metrics are per-prompt. The report reads metric names from the results and section leads
from `index.json`, so new names need no script change. Keep the `golden` / `edge-case` fixture
directory names to get the existing report layout.
