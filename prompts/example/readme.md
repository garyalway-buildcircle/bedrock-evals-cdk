# example

Placeholder prompt so the harness has one directory to discover. It labels a short note
`positive`, `negative`, or `neutral`. Replace this directory with a real prompt, or add another
one beside it (`docs/adding-a-prompt.md`).

## Output

```json
{ "label": "positive", "reason": "one plain sentence" }
```

`label` is `positive`, `negative`, or `neutral`. `reason` is one sentence and does not copy the note.

Cases: `fixtures/`. After a prompt edit: `python3 scripts/render-datasets.py example`, then
`docs/runbook.md`.
