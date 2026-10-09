#!/usr/bin/env python3
"""HTML report from completed Bedrock evaluation job ARNs.

    python3 scripts/build-eval-report.py <job-arn> [<job-arn> ...] [--prompt example]

Several ARNs compare runs. Metric names come from the results; section leads from
fixtures/<set>/index.json. Default output: reports/<prompt>-eval-report.html (opens unless
--no-open). Needs AWS CLI + S3 read on the output bucket.
"""
import argparse
import html
import json
import subprocess
import sys
import webbrowser
from pathlib import Path


def aws_json(*args):
    result = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"aws {' '.join(args)} failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def extract_transcript(prompt):
    """Pull the transcript out of the untrusted-content wrap.

    Uses rsplit (last occurrence), not split (first) - the system prompt's own rule text
    mentions the wrap delimiters as an example before the real wrapped transcript appears later
    in the string, and a first-occurrence split matches that mention instead.
    """
    if "<<<END_UNTRUSTED_CONTENT>>>" not in prompt:
        return ""
    before_end = prompt.rsplit("<<<END_UNTRUSTED_CONTENT>>>", 1)[0]
    if "<<<UNTRUSTED_CONTENT>>>" not in before_end:
        return ""
    return before_end.rsplit("<<<UNTRUSTED_CONTENT>>>", 1)[1].strip()


def load_fixture_metadata(prompt_root):
    meta = {}
    for folder, category in [("fixtures/golden", "golden"), ("fixtures/edge-case", "edge")]:
        d = prompt_root / folder
        if not d.exists():
            continue
        for fp in d.glob("*.json"):
            if fp.name == "index.json":
                continue
            fx = json.loads(fp.read_text())
            meta[fx["id"]] = {
                "category": category,
                "description": fx.get("description", ""),
                "rule": fx.get("ruleValidated", ""),
            }
    return meta


SET_LEAD_FALLBACKS = {
    "golden": "Cases the prompt should get right. A Fail is a prompt bug or a bad fixture.",
    "edge": "Adversarial and near-miss cases, including injection. A Fail is blocking.",
}


def load_set_leads(prompt_root):
    """Section leads come from each set's fixtures/<set>/index.json, so a new prompt
    describes its own sets without this script knowing what they test."""
    leads = dict(SET_LEAD_FALLBACKS)
    for folder, key in [("fixtures/golden", "golden"), ("fixtures/edge-case", "edge")]:
        idx = prompt_root / folder / "index.json"
        if not idx.exists():
            continue
        description = json.loads(idx.read_text()).get("description", "").strip()
        if description:
            leads[key] = description
    return leads


def fetch_job_results(job_arn):
    job = aws_json("bedrock", "get-evaluation-job", "--job-identifier", job_arn)
    status = job.get("status")
    if status != "Completed":
        print(f"warning: job {job_arn} has status {status!r}, not Completed - skipping", file=sys.stderr)
        return None

    job_name = job["jobName"]
    model_id = job["inferenceConfig"]["models"][0]["bedrockModel"]["modelIdentifier"]
    output_uri = job["outputDataConfig"]["s3Uri"]
    bucket, _, prefix = output_uri[len("s3://"):].partition("/")
    job_id = job_arn.rsplit("/", 1)[-1]
    search_prefix = f"{prefix.rstrip('/')}/{job_name}/{job_id}/models/"

    listing = aws_json("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", search_prefix)
    keys = [obj["Key"] for obj in listing.get("Contents", []) if obj["Key"].endswith("_output.jsonl")]
    if not keys:
        print(f"warning: no *_output.jsonl found for job {job_arn} under s3://{bucket}/{search_prefix}", file=sys.stderr)
        return None

    tmp_path = Path(f"/tmp/{job_id}_output.jsonl")
    subprocess.run(["aws", "s3", "cp", f"s3://{bucket}/{keys[0]}", str(tmp_path)], check=True, capture_output=True)
    rows = load_jsonl(tmp_path)
    tmp_path.unlink()

    return {"job_name": job_name, "model_id": model_id, "rows": rows}


def match_fixture(prompt, datasets, row_index):
    """Match a result row back to the dataset row it came from, by transcript.

    Unmatched rows get a per-row id rather than a shared one: cases are keyed by fixture id, so a
    single shared key would collapse every unmatched row into one case that renders as a plausible
    one-fixture report.
    """
    marker = extract_transcript(prompt)
    for row in datasets:
        if extract_transcript(row["prompt"]) == marker:
            return row["fixtureId"], row.get("referenceResponse", "")
    return f"unknown-fixture-{row_index}", ""


def metrics_for(row):
    """Every custom metric the job rated this row with, keyed by name.

    Metric names are whatever the eval-job template asked for, and they differ per prompt, so
    nothing here may hardcode one. A score with no metricName (older runs predate the field)
    is keyed "Score" so the run still renders rather than dropping out of a comparison."""
    out = {}
    for score in row["automatedEvaluationResult"]["scores"]:
        name = score.get("metricName") or "Score"
        details = score.get("evaluatorDetails") or [{}]
        out[name] = {"result": score.get("result"), "explanation": details[0].get("explanation", "")}
    return out


def build_cases(job_results, datasets, fixture_meta):
    """Assign each job a run number *per fixture category*, not by its raw position in the
    argument list. A golden job and an edge-case job passed together (in any order, interleaved
    or grouped) each become "run 1" for their own category - this is what lets one combined
    report show both datasets side by side instead of needing two separate report files."""
    cases = {}
    category_run_counters = {}
    for result in job_results:
        matched = []
        for index, row in enumerate(result["rows"], start=1):
            fid, ref = match_fixture(row["inputRecord"]["prompt"], datasets, index)
            category = fixture_meta.get(fid, {}).get("category", "unknown")
            matched.append((fid, category, ref, row))

        if all(fid.startswith("unknown-fixture") for fid, *_ in matched):
            raise SystemExit(
                f"{result['job_name']}: no row matched a fixture in this prompt's datasets — "
                "wrong --prompt for these jobs?"
            )

        job_run_number = {}
        for category in sorted({m[1] for m in matched}):
            category_run_counters[category] = category_run_counters.get(category, 0) + 1
            job_run_number[category] = category_run_counters[category]

        for fid, category, ref, row in matched:
            version_key = f"run{job_run_number[category]}"
            transcript = extract_transcript(row["inputRecord"]["prompt"])
            if fid not in cases:
                meta = fixture_meta.get(fid, {})
                cases[fid] = {
                    "id": fid,
                    "category": category,
                    "description": meta.get("description", ""),
                    "rule": meta.get("rule", ""),
                    "transcript": transcript,
                    "reference": ref,
                    "versions": {},
                }
            cases[fid]["versions"][version_key] = {
                "model": result["model_id"],
                "jobName": result["job_name"],
                "response": row["modelResponses"][0]["response"],
                "metrics": metrics_for(row),
            }
    return cases


def metric_names(cases):
    """Metric names in first-seen order, so the report's column order is the job's order."""
    names = []
    for case in cases:
        for version in case["versions"].values():
            for name in version["metrics"]:
                if name not in names:
                    names.append(name)
    return names


HTML_TEMPLATE = r"""<title>__TITLE__</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
  :root {
    --bg: #EEF1F0; --surface: #FFFFFF; --surface-2: #E3E8E6;
    --ink: #16211E; --ink-muted: #57655F; --border: #D2DAD7;
    --accent: #0E6E66; --accent-soft: #DFEEEB;
    --pass: #2F7A4F; --pass-soft: #E3F1E7;
    --fail: #AE372F; --fail-soft: #FBEAE8;
    --quote-bg: #12201C; --quote-text: #CBE9DF; --quote-border: #274139;
    --pending: #8A7A2E; --pending-soft: #F3EDD8;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #101614; --surface: #182420; --surface-2: #1E2B26;
      --ink: #E7EEEC; --ink-muted: #94A29D; --border: #2A3A34;
      --accent: #57C7B8; --accent-soft: #163430;
      --pass: #74CB93; --pass-soft: #17281D;
      --fail: #E79A94; --fail-soft: #2E1B19;
      --quote-bg: #060B09; --quote-text: #9FE0D2; --quote-border: #1D2E28;
      --pending: #D8C560; --pending-soft: #2A2716;
    }
  }
  :root[data-theme="dark"] {
    --bg: #101614; --surface: #182420; --surface-2: #1E2B26;
    --ink: #E7EEEC; --ink-muted: #94A29D; --border: #2A3A34;
    --accent: #57C7B8; --accent-soft: #163430;
    --pass: #74CB93; --pass-soft: #17281D;
    --fail: #E79A94; --fail-soft: #2E1B19;
    --quote-bg: #060B09; --quote-text: #9FE0D2; --quote-border: #1D2E28;
    --pending: #D8C560; --pending-soft: #2A2716;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--ink);
    font-family: 'IBM Plex Sans', -apple-system, 'Segoe UI', sans-serif;
    line-height: 1.5; max-width: 920px; margin: 0 auto; padding: 40px 24px 80px;
  }
  h1, h2 { text-wrap: balance; margin: 0; }
  code, .mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
  .eyebrow {
    font-family: 'IBM Plex Mono', monospace; font-size: 12px; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--accent); font-weight: 600;
  }
  header.page {
    display: flex; flex-direction: column; gap: 10px; padding-bottom: 28px;
    border-bottom: 1px solid var(--border); margin-bottom: 28px;
  }
  header.page h1 { font-size: 30px; font-weight: 700; letter-spacing: -0.01em; }
  .meta-row { display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 13px; color: var(--ink-muted); }
  .meta-row span b { color: var(--ink); font-weight: 600; }
  .progression { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 32px; }
  .prog-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .prog-card .prog-title { font-size: 13px; font-weight: 600; color: var(--ink-muted); margin-bottom: 12px; }
  .prog-track { display: flex; align-items: center; gap: 8px; }
  .prog-step { display: flex; flex-direction: column; align-items: center; gap: 4px; flex: 1; }
  .prog-score {
    font-family: 'IBM Plex Mono', monospace; font-weight: 600; font-size: 20px;
    font-variant-numeric: tabular-nums; width: 100%; text-align: center; padding: 6px 0; border-radius: 6px;
  }
  .prog-score.pass-full { background: var(--pass-soft); color: var(--pass); }
  .prog-score.pass-partial { background: var(--fail-soft); color: var(--fail); }
  .prog-label { font-size: 11px; color: var(--ink-muted); text-align: center; }
  .prog-arrow { color: var(--border); font-size: 16px; }
  section.dataset { margin-bottom: 36px; }
  section.dataset > h2 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
  section.dataset > p.lead { font-size: 13.5px; color: var(--ink-muted); margin: 0 0 16px; max-width: 65ch; }
  .case { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
  .case > summary { list-style: none; cursor: pointer; padding: 14px 18px; display: flex; align-items: center; gap: 12px; }
  .case > summary::-webkit-details-marker { display: none; }
  .case > summary .chevron { font-size: 11px; color: var(--ink-muted); transition: transform 0.15s ease; flex-shrink: 0; }
  .case[open] > summary .chevron { transform: rotate(90deg); }
  .case > summary .id { font-family: 'IBM Plex Mono', monospace; font-size: 13.5px; font-weight: 500; flex: 1; min-width: 0; }
  .pill {
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; letter-spacing: 0.03em;
    padding: 3px 9px; border-radius: 20px; flex-shrink: 0;
  }
  .pill.Pass { background: var(--pass-soft); color: var(--pass); }
  .pill.Fail { background: var(--fail-soft); color: var(--fail); }
  .version-chips { display: flex; gap: 5px; flex-shrink: 0; }
  .vchip {
    width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 700;
  }
  .vchip.Pass { background: var(--pass-soft); color: var(--pass); }
  .vchip.Fail { background: var(--fail-soft); color: var(--fail); }
  .case-body { padding: 4px 18px 20px; border-top: 1px solid var(--border); }
  .case-body .rule {
    font-size: 13px; color: var(--ink-muted); margin: 14px 0 18px; padding-left: 12px; border-left: 2px solid var(--accent);
  }
  .case-body .rule b { color: var(--ink); }
  .version-block { margin-bottom: 18px; }
  .version-block:last-child { margin-bottom: 0; }
  .version-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .version-head .vname { font-size: 12.5px; font-weight: 600; color: var(--ink-muted); }
  .version-head .vmodel { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--ink-muted); }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 640px) { .grid-2 { grid-template-columns: 1fr; } }
  .block-label {
    font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-muted); margin-bottom: 5px;
  }
  pre.transcript, pre.response {
    background: var(--quote-bg); color: var(--quote-text); border: 1px solid var(--quote-border);
    border-radius: 7px; padding: 12px 14px; font-family: 'IBM Plex Mono', monospace; font-size: 12px;
    line-height: 1.55; white-space: pre-wrap; word-break: break-word; overflow-x: auto;
    margin: 0 0 12px; max-height: 220px; overflow-y: auto;
  }
  .explanation { font-size: 13px; color: var(--ink); background: var(--surface-2); border-radius: 7px; padding: 12px 14px; line-height: 1.55; }
  footer.page { margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border); font-size: 12px; color: var(--ink-muted); }
</style>
<header class="page">
  <div class="eyebrow">Bedrock Model Evaluation</div>
  <h1 id="page-title"></h1>
  <div class="meta-row" id="meta-row"></div>
</header>
<div class="progression" id="progression"></div>
<section class="dataset">
  <h2>Golden set</h2>
  <p class="lead">__GOLDEN_LEAD__</p>
  <div id="golden-cases"></div>
</section>
<section class="dataset">
  <h2>Edge-case set</h2>
  <p class="lead">__EDGE_LEAD__</p>
  <div id="edge-cases"></div>
</section>
<footer class="page">Generated by scripts/build-eval-report.py from live Bedrock evaluation job results.</footer>
<script id="eval-data" type="application/json">__DATA__</script>
<script id="eval-metrics" type="application/json">__METRICS__</script>
<script>
(function () {
  const cases = JSON.parse(document.getElementById('eval-data').textContent);
  const METRICS = JSON.parse(document.getElementById('eval-metrics').textContent);
  document.getElementById('page-title').textContent = document.title;

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function prettyJson(text) {
    try { return JSON.stringify(JSON.parse(text), null, 2); } catch (e) { return text; }
  }

  // Run numbering is per fixture-category (see build_cases in the Python script) - golden and
  // edge-case can have a different number of runs, so compute version keys separately per list
  // rather than assuming one global count.
  function versionKeysFor(list) {
    const maxRun = list.reduce((max, c) => {
      const nums = Object.keys(c.versions).map(k => parseInt(k.replace('run', ''), 10));
      return Math.max(max, ...nums, 0);
    }, 0);
    return Array.from({ length: maxRun }, (_, i) => 'run' + (i + 1));
  }

  document.getElementById('meta-row').innerHTML =
    `<span>Golden: <b>${cases.filter(c => c.category === 'golden').length}</b> fixtures</span>` +
    `<span>Edge-case: <b>${cases.filter(c => c.category === 'edge').length}</b> fixtures</span>`;

  function progCard(title, list, metric) {
    const versionKeys = versionKeysFor(list);
    const steps = versionKeys.map((vk, i) => {
      const present = list.filter(c => c.versions[vk] && c.versions[vk].metrics[metric]);
      if (present.length === 0) return null;
      const pass = present.filter(c => c.versions[vk].metrics[metric].result === 'Pass').length;
      return { pass, total: present.length };
    });
    const html = steps.map((s, i) => {
      const inner = !s ? '' : `<div class="prog-score ${s.pass === s.total ? 'pass-full' : 'pass-partial'}">${s.pass}/${s.total}</div><div class="prog-label">Run ${i + 1}</div>`;
      const arrow = i < steps.length - 1 ? '<div class="prog-arrow">&rarr;</div>' : '';
      return `<div class="prog-step">${inner}</div>${arrow}`;
    }).join('');
    return `<div class="prog-card"><div class="prog-title">${title}</div><div class="prog-track">${html}</div></div>`;
  }

  // One track per metric per set. Metric names come from the results, so a prompt with its own
  // judge metrics reports on those without this file knowing anything about them. A metric no run
  // actually rated draws nothing rather than an empty track.
  const golden = cases.filter(c => c.category === 'golden');
  const edge = cases.filter(c => c.category === 'edge');
  const rated = (list, m) => list.some(c => Object.values(c.versions).some(v => v.metrics[m]));
  document.getElementById('progression').innerHTML =
    [['Golden set', golden], ['Edge-case set', edge]]
      .flatMap(([label, list]) => METRICS.filter(m => rated(list, m)).map(m => progCard(`${label}: ${m}`, list, m)))
      .join('');

  function versionBlock(vk, v) {
    if (!v) return '';
    return `
      <div class="version-block">
        <div class="version-head">
          <span class="vname">${esc(v.jobName)}</span>
          ${Object.entries(v.metrics).map(([m, s]) => `<span class="pill ${s.result}">${esc(m)}: ${s.result}</span>`).join('')}
          <span class="vmodel">${esc(v.model)}</span>
        </div>
        <div class="grid-2">
          <div><div class="block-label">Response</div><pre class="response">${esc(prettyJson(v.response))}</pre></div>
          <div>
            ${Object.entries(v.metrics).map(([m, s]) =>
              `<div class="block-label">Judge explanation: ${esc(m)}</div><div class="explanation">${esc(s.explanation)}</div>`).join('')}
          </div>
        </div>
      </div>`;
  }

  function caseHtml(c) {
    const versionKeys = versionKeysFor([c]);
    const latestKey = [...versionKeys].reverse().find(k => c.versions[k]);
    const latest = c.versions[latestKey];
    // A row only passes if every metric rated on it passed - a correct verdict reached through
    // fabricated evidence is still a failing row, whatever the metrics happen to be called.
    const overall = v => Object.values(v.metrics).every(s => s.result === 'Pass') ? 'Pass' : 'Fail';
    const chips = versionKeys.map(vk => {
      if (!c.versions[vk]) return '';
      const r = overall(c.versions[vk]);
      return `<div class="vchip ${r}" title="${r}">${r === 'Pass' ? '&#10003;' : '&#10005;'}</div>`;
    }).join('');
    const versionsHtml = versionKeys.map(vk => versionBlock(vk, c.versions[vk])).join('');
    return `
      <details class="case">
        <summary>
          <span class="chevron">&#9656;</span>
          <span class="id">${esc(c.id)}</span>
          <span class="version-chips">${chips}</span>
          <span class="pill ${overall(latest)}">${overall(latest)}</span>
        </summary>
        <div class="case-body">
          <div class="rule"><b>${c.rule ? 'Rule:' : 'Case:'}</b> ${esc(c.rule || c.description)}</div>
          <div class="version-block">
            <div class="block-label">Input</div>
            <pre class="transcript">${esc(c.transcript)}</pre>
            <div class="block-label">Reference (expected)</div>
            <pre class="response">${esc(prettyJson(c.reference))}</pre>
          </div>
          ${versionsHtml}
        </div>
      </details>`;
  }

  document.getElementById('golden-cases').innerHTML = cases.filter(c => c.category === 'golden').map(caseHtml).join('');
  document.getElementById('edge-cases').innerHTML = cases.filter(c => c.category === 'edge').map(caseHtml).join('');
})();
</script>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("job_arns", nargs="+", help="One or more completed Bedrock evaluation job ARNs")
    parser.add_argument("--prompt", default="example", help="Prompt directory under prompts/ the jobs were run for (default: example)")
    parser.add_argument("--out", help="Output HTML file path (default: reports/<prompt>-eval-report.html)")
    parser.add_argument("--no-open", action="store_true", help="Write the report without opening it in a browser")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    prompt_root = repo_root / "prompts" / args.prompt
    if not prompt_root.is_dir():
        print(f"no such prompt: prompts/{args.prompt}", file=sys.stderr)
        sys.exit(1)

    golden_ds = load_jsonl(prompt_root / "datasets" / "golden.jsonl")
    edge_ds = load_jsonl(prompt_root / "datasets" / "edge-case.jsonl")
    fixture_meta = load_fixture_metadata(prompt_root)
    set_leads = load_set_leads(prompt_root)

    job_results = [r for r in (fetch_job_results(arn) for arn in args.job_arns) if r]
    if not job_results:
        print("no completed jobs with results found", file=sys.stderr)
        sys.exit(1)

    cases = build_cases(job_results, golden_ds + edge_ds, fixture_meta)
    ordered = sorted(cases.values(), key=lambda c: (c["category"], c["id"]))

    label = args.prompt.replace("-", " ").title()
    categories = {c["category"] for c in ordered}
    if categories == {"golden"}:
        title = f"{label} — Golden Set Eval Report"
    elif categories == {"edge"}:
        title = f"{label} — Edge Case Eval Report"
    else:
        title = f"{label} Eval Report"

    page = (HTML_TEMPLATE
            .replace("__DATA__", json.dumps(ordered))
            .replace("__METRICS__", json.dumps(metric_names(ordered)))
            .replace("__TITLE__", title)
            .replace("__GOLDEN_LEAD__", html.escape(set_leads["golden"]))
            .replace("__EDGE_LEAD__", html.escape(set_leads["edge"])))

    out_path = repo_root / (args.out or f"reports/{args.prompt}-eval-report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    print(f"wrote {out_path} ({len(ordered)} fixtures from {len(job_results)} job(s))")

    if not args.no_open:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
