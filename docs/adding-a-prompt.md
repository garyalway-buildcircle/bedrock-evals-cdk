# Adding a prompt

One directory under `prompts/`. The stack discovers it at synth time. The only repo-level edit
is a row in the root `README.md` table.

```
prompts/<prompt>/
  readme.md                    what this prompt returns
  prompt.json                  promptName + description
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
2. Write the two `.txt` files. The user template has exactly one `{{variable}}` (name it for
   the input: `note`, `email`, …). The stack and render-datasets read that name from
   the template.
3. Write fixtures, then matching JSONL rows
   (`prompt`, `referenceResponse`, `category`, `fixtureId`). `prompt` is system text, a newline,
   and the user template with the wrapped untrusted input substituted in.
4. `python3 scripts/render-datasets.py <prompt>` (and `--check` later).
5. Copy `prompts/example/eval-jobs/*.json` and rewrite the judge metrics for this
   output schema. `ratingScale[].definition` must stay under 100 characters (`docs/runbook.md`).
   Point the S3 URIs at `datasets/<prompt>/<set>.jsonl` and `results/<prompt>/<set>/`. Keep
   `jobName` short enough for a timestamp (63-character Bedrock limit).
6. `npx cdk deploy`, run both jobs, build the report with `--prompt <prompt>`.
7. Write `readme.md` and add the table row in the root `README.md`.

Judge metrics are per-prompt. The report reads metric names from the results and section leads
from `index.json`, so new names need no script change. Keep the `golden` / `edge-case` fixture
directory names to get the existing report layout.
