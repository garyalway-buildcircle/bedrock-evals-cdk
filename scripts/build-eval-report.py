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
import tempfile
import webbrowser
from pathlib import Path


def aws_json(*args):
    result = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"aws {' '.join(args)} failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


OPEN_WRAP = "\n<<<UNTRUSTED_CONTENT>>>\n"
CLOSE_WRAP = "\n<<<END_UNTRUSTED_CONTENT>>>"


def extract_transcript(prompt):
    """Pull the input out of the newline-delimited wrap.

    The system prompt may name both markers inline. Only the newline-delimited form is the real
    wrap, and it has to occur once. A fixture that contains either marker makes the split ambiguous.
    """
    opens = prompt.count(OPEN_WRAP)
    closes = prompt.count(CLOSE_WRAP)
    if opens != 1 or closes != 1 or not prompt.endswith(CLOSE_WRAP):
        raise SystemExit(
            "untrusted-content wrap is missing or ambiguous; the rendered prompt must contain it once, "
            "and a fixture must not contain the wrap markers"
        )
    payload = prompt[prompt.rfind(OPEN_WRAP) + len(OPEN_WRAP) : -len(CLOSE_WRAP)]
    if "<<<UNTRUSTED_CONTENT>>>" in payload or "<<<END_UNTRUSTED_CONTENT>>>" in payload:
        raise SystemExit(
            "untrusted-content wrap is ambiguous; a fixture must not contain the wrap markers"
        )
    return payload.strip()


def report_category(folder_name):
    """golden and edge-case keep the report's existing section names. Any other set uses its directory name."""
    if folder_name == "golden":
        return "golden"
    if folder_name == "edge-case":
        return "edge"
    return folder_name


def load_fixture_metadata(prompt_root):
    meta = {}
    fixtures = prompt_root / "fixtures"
    if not fixtures.is_dir():
        return meta
    for folder in sorted(p for p in fixtures.iterdir() if p.is_dir()):
        category = report_category(folder.name)
        for fp in folder.glob("*.json"):
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
    fixtures = prompt_root / "fixtures"
    if not fixtures.is_dir():
        return leads
    for folder in sorted(p for p in fixtures.iterdir() if p.is_dir()):
        idx = folder / "index.json"
        if not idx.exists():
            continue
        description = json.loads(idx.read_text()).get("description", "").strip()
        if description:
            leads[report_category(folder.name)] = description
    return leads


def output_keys_from_pages(pages):
    """Every *_output.jsonl key across list-objects pages, in listing order."""
    keys = []
    for listing in pages:
        for obj in listing.get("Contents") or []:
            key = obj.get("Key", "")
            if key.endswith("_output.jsonl"):
                keys.append(key)
    return keys


def list_output_keys(bucket, prefix):
    """Page list-objects-v2 explicitly. The CLI's own pager is turned off so a truncated page is not dropped."""
    pages = []
    token = None
    while True:
        cmd = ["--no-paginate", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix]
        if token:
            cmd.extend(["--continuation-token", token])
        listing = aws_json(*cmd)
        pages.append(listing)
        if not listing.get("IsTruncated"):
            break
        token = listing.get("NextContinuationToken")
        if not token:
            break
    return output_keys_from_pages(pages)


def json_for_html(value):
    """JSON for a <script> tag. Escaping < stops a model response containing </script> from closing the tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def fetch_job_results(job_arn):
    job = aws_json("bedrock", "get-evaluation-job", "--job-identifier", job_arn)
    status = job.get("status")
    if status != "Completed":
        raise SystemExit(f"job {job_arn} has status {status!r}, not Completed. The report was not written.")

    job_name = job["jobName"]
    model_id = job["inferenceConfig"]["models"][0]["bedrockModel"]["modelIdentifier"]
    output_uri = job["outputDataConfig"]["s3Uri"]
    bucket, _, prefix = output_uri[len("s3://"):].partition("/")
    job_id = job_arn.rsplit("/", 1)[-1]
    search_prefix = f"{prefix.rstrip('/')}/{job_name}/{job_id}/models/"

    keys = list_output_keys(bucket, search_prefix)
    if not keys:
        raise SystemExit(
            f"no *_output.jsonl found for job {job_arn} under s3://{bucket}/{search_prefix}. The report was not written."
        )

    rows = []
    with tempfile.TemporaryDirectory(prefix=f"eval-report-{job_id}-") as tmp:
        for index, key in enumerate(keys):
            dest = Path(tmp) / f"{index}.jsonl"
            subprocess.run(
                ["aws", "s3", "cp", f"s3://{bucket}/{key}", str(dest)],
                check=True,
                capture_output=True,
                text=True,
            )
            rows.extend(load_jsonl(dest))

    return {"job_name": job_name, "model_id": model_id, "rows": rows}


def match_fixture(prompt, datasets, row_index):
    """Match a result row back to the dataset row it came from, by transcript.

    Unmatched rows get a per-row id rather than a shared one: cases are keyed by fixture id, so a
    single shared key would collapse every unmatched row into one case that renders as a plausible
    one-fixture report.
    """
    marker = extract_transcript(prompt)
    matches = [row for row in datasets if extract_transcript(row["prompt"]) == marker]
    if len(matches) > 1:
        ids = ", ".join(str(row.get("fixtureId")) for row in matches)
        raise SystemExit(
            f"result row {row_index} matches more than one dataset fixture ({ids}). "
            "Their input text is the same after trimming."
        )
    if len(matches) == 1:
        row = matches[0]
        return row["fixtureId"], row.get("referenceResponse", ""), row.get("category", "")
    return f"unknown-fixture-{row_index}", "", ""


def category_for(fixture_id, dataset_category, fixture_meta):
    """Prefer the fixture file's set. Fall back to the dataset row so a missing fixture file still renders."""
    meta_category = fixture_meta.get(fixture_id, {}).get("category")
    if meta_category:
        return meta_category
    if dataset_category == "edge-case":
        return "edge"
    if dataset_category:
        return dataset_category
    return "unknown"


def metrics_for(row):
    """Every custom metric the job rated this row with, keyed by name.

    Metric names are whatever the eval-job template asked for, and they differ per prompt, so
    nothing here may hardcode one. A score with no metricName (older runs predate the field)
    is keyed "Score" so the run still renders rather than dropping out of a comparison.
    A row with no scores still renders, as a Fail, instead of aborting the report."""
    result = row.get("automatedEvaluationResult") if isinstance(row, dict) else None
    scores = result.get("scores") if isinstance(result, dict) else None
    if not isinstance(scores, list) or not scores:
        return {"Score": {"result": "Fail", "explanation": "This row has no automatedEvaluationResult.scores."}}
    out = {}
    for score in scores:
        if not isinstance(score, dict):
            continue
        name = score.get("metricName") or "Score"
        details = score.get("evaluatorDetails") or [{}]
        explanation = ""
        if isinstance(details, list) and details and isinstance(details[0], dict):
            explanation = details[0].get("explanation", "")
        out[name] = {"result": score.get("result") or "Fail", "explanation": explanation}
    if not out:
        return {"Score": {"result": "Fail", "explanation": "This row has no readable scores."}}
    return out


def model_response(row):
    """The generator text, or empty when Bedrock omitted modelResponses. Missing data must not abort the report."""
    responses = row.get("modelResponses") if isinstance(row, dict) else None
    if not isinstance(responses, list) or not responses or not isinstance(responses[0], dict):
        return ""
    response = responses[0].get("response")
    return response if isinstance(response, str) else ""


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
            input_record = row.get("inputRecord") if isinstance(row, dict) else None
            prompt = input_record.get("prompt") if isinstance(input_record, dict) else None
            if not isinstance(prompt, str):
                matched.append((f"unknown-fixture-{index}", "unknown", "", row if isinstance(row, dict) else {}))
                continue
            fid, ref, dataset_category = match_fixture(prompt, datasets, index)
            matched.append((fid, category_for(fid, dataset_category, fixture_meta), ref, row))

        if not matched:
            raise SystemExit(f"{result['job_name']}: the job returned no rows. The report was not written.")

        unknown = [fid for fid, *_ in matched if str(fid).startswith("unknown-fixture")]
        if unknown:
            raise SystemExit(
                f"{result['job_name']}: {len(unknown)} of {len(matched)} row(s) did not match a dataset fixture "
                f"({', '.join(unknown)}). Those rows would be missing from the report. "
                "Pass the --prompt these jobs were run for."
            )
        categories = {category for _, category, _, _ in matched}
        found = {fid for fid, _, _, _ in matched}
        missing = []
        for row in datasets:
            fid = row.get("fixtureId")
            category = category_for(fid, row.get("category", ""), fixture_meta)
            if category in categories and fid not in found:
                missing.append(str(fid))
        if missing:
            raise SystemExit(
                f"{result['job_name']}: {len(missing)} fixture(s) in the scored set have no result "
                f"({', '.join(missing)}). A short result file would otherwise look complete."
            )

        job_run_number = {}
        for category in sorted({m[1] for m in matched}):
            category_run_counters[category] = category_run_counters.get(category, 0) + 1
            job_run_number[category] = category_run_counters[category]

        for fid, category, ref, row in matched:
            version_key = f"run{job_run_number[category]}"
            transcript = extract_transcript(row["inputRecord"]["prompt"])
            if fid in cases and version_key in cases[fid]["versions"]:
                raise SystemExit(
                    f"{result['job_name']}: fixture {fid} matched more than once in one run. "
                    "Duplicate fixture ids or identical input text would drop a row."
                )
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
                "response": model_response(row),
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
    font-family: -apple-system, 'Segoe UI', sans-serif;
    line-height: 1.5; max-width: 920px; margin: 0 auto; padding: 40px 24px 80px;
  }
  h1, h2 { text-wrap: balance; margin: 0; }
  code, .mono { font-family: ui-monospace, monospace; }
  .eyebrow {
    font-family: ui-monospace, monospace; font-size: 12px; letter-spacing: 0.08em;
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
    font-family: ui-monospace, monospace; font-weight: 600; font-size: 20px;
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
  .case > summary .id { font-family: ui-monospace, monospace; font-size: 13.5px; font-weight: 500; flex: 1; min-width: 0; }
  .pill {
    font-family: ui-monospace, monospace; font-size: 11px; font-weight: 600; letter-spacing: 0.03em;
    padding: 3px 9px; border-radius: 20px; flex-shrink: 0;
  }
  .pill.Pass { background: var(--pass-soft); color: var(--pass); }
  .pill.Fail { background: var(--fail-soft); color: var(--fail); }
  .version-chips { display: flex; gap: 5px; flex-shrink: 0; }
  .vchip {
    width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-family: ui-monospace, monospace; font-size: 10px; font-weight: 700;
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
  .version-head .vmodel { font-family: ui-monospace, monospace; font-size: 11.5px; color: var(--ink-muted); }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 640px) { .grid-2 { grid-template-columns: 1fr; } }
  .block-label {
    font-size: 10.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--ink-muted); margin-bottom: 5px;
  }
  pre.transcript, pre.response {
    background: var(--quote-bg); color: var(--quote-text); border: 1px solid var(--quote-border);
    border-radius: 7px; padding: 12px 14px; font-family: ui-monospace, monospace; font-size: 12px;
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
<section class="dataset" id="golden-section">
  <h2>Golden set</h2>
  <p class="lead">__GOLDEN_LEAD__</p>
  <div id="golden-cases"></div>
</section>
<section class="dataset" id="edge-section">
  <h2>Edge-case set</h2>
  <p class="lead">__EDGE_LEAD__</p>
  <div id="edge-cases"></div>
</section>
<div id="extra-sets"></div>
<footer class="page">Generated by scripts/build-eval-report.py from live Bedrock evaluation job results.</footer>
<script id="eval-data" type="application/json">__DATA__</script>
<script id="eval-metrics" type="application/json">__METRICS__</script>
<script id="eval-leads" type="application/json">__LEADS__</script>
<script>
(function () {
  const cases = JSON.parse(document.getElementById('eval-data').textContent);
  const METRICS = JSON.parse(document.getElementById('eval-metrics').textContent);
  const leads = JSON.parse(document.getElementById('eval-leads').textContent);
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

  const golden = cases.filter(c => c.category === 'golden');
  const edge = cases.filter(c => c.category === 'edge');
  const other = cases.filter(c => c.category !== 'golden' && c.category !== 'edge');
  document.getElementById('meta-row').innerHTML =
    `<span>Golden: <b>${golden.length}</b> fixtures</span>` +
    `<span>Edge-case: <b>${edge.length}</b> fixtures</span>` +
    (other.length ? `<span>Other: <b>${other.length}</b> fixtures</span>` : '');

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
    return `<div class="prog-card"><div class="prog-title">${esc(title)}</div><div class="prog-track">${html}</div></div>`;
  }

  // One track per metric per set. Metric names come from the results, so a prompt with its own
  // judge metrics reports on those without this file knowing anything about them. A metric no run
  // actually rated draws nothing rather than an empty track.
  const rated = (list, m) => list.some(c => Object.values(c.versions).some(v => v.metrics[m]));
  const extraCategories = [...new Set(other.map(c => c.category))];
  const groups = [['Golden set', golden], ['Edge-case set', edge]]
    .concat(extraCategories.map(category => [category, other.filter(c => c.category === category)]));
  document.getElementById('progression').innerHTML =
    groups
      .flatMap(([label, list]) => METRICS.filter(m => rated(list, m)).map(m => progCard(`${label}: ${m}`, list, m)))
      .join('');

  function versionBlock(vk, v) {
    if (!v) return '';
    return `
      <div class="version-block">
        <div class="version-head">
          <span class="vname">${esc(v.jobName)}</span>
          ${Object.entries(v.metrics).map(([m, s]) => {
            const resultClass = s.result === 'Pass' ? 'Pass' : 'Fail';
            return `<span class="pill ${resultClass}">${esc(m)}: ${esc(s.result)}</span>`;
          }).join('')}
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

  document.getElementById('golden-section').hidden = golden.length === 0;
  document.getElementById('edge-section').hidden = edge.length === 0;
  document.getElementById('golden-cases').innerHTML = golden.map(caseHtml).join('');
  document.getElementById('edge-cases').innerHTML = edge.map(caseHtml).join('');
  document.getElementById('extra-sets').innerHTML = extraCategories.map(category => {
    const list = other.filter(c => c.category === category);
    const lead = leads[category] || ('Rows from fixtures/' + category + '.');
    return `<section class="dataset"><h2>${esc(category)}</h2><p class="lead">${esc(lead)}</p><div>${list.map(caseHtml).join('')}</div></section>`;
  }).join('');
})();
</script>
"""


def fill_template(template, replacements):
    """Insert each value once. Later replacements do not scan earlier values."""
    rest = template
    parts = []
    for key, value in replacements:
        before, sep, rest = rest.partition(key)
        if not sep:
            raise SystemExit(f"report template is missing {key}")
        parts.append(before)
        parts.append(value)
    parts.append(rest)
    return "".join(parts)


def load_datasets(prompt_root):
    datasets_dir = prompt_root / "datasets"
    files = sorted(datasets_dir.glob("*.jsonl")) if datasets_dir.is_dir() else []
    if not files:
        raise SystemExit(f"prompts/{prompt_root.name}: datasets/*.jsonl is missing. Run scripts/render-datasets.py.")
    rows = []
    for path in files:
        try:
            rows.extend(load_jsonl(path))
        except OSError as err:
            raise SystemExit(f"{path}: {err}") from err
    return rows


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

    datasets = load_datasets(prompt_root)
    fixture_meta = load_fixture_metadata(prompt_root)
    set_leads = load_set_leads(prompt_root)

    job_results = [fetch_job_results(arn) for arn in args.job_arns]

    cases = build_cases(job_results, datasets, fixture_meta)
    ordered = sorted(cases.values(), key=lambda c: (c["category"], c["id"]))

    label = args.prompt.replace("-", " ").title()
    categories = {c["category"] for c in ordered}
    if categories == {"golden"}:
        title = f"{label} — Golden Set Eval Report"
    elif categories == {"edge"}:
        title = f"{label} — Edge Case Eval Report"
    else:
        title = f"{label} Eval Report"

    page = fill_template(
        HTML_TEMPLATE,
        [
            ("__TITLE__", html.escape(title)),
            ("__GOLDEN_LEAD__", html.escape(set_leads.get("golden", ""))),
            ("__EDGE_LEAD__", html.escape(set_leads.get("edge", ""))),
            ("__DATA__", json_for_html(ordered)),
            ("__METRICS__", json_for_html(metric_names(ordered))),
            ("__LEADS__", json_for_html(set_leads)),
        ],
    )

    out_path = repo_root / (args.out or f"reports/{args.prompt}-eval-report.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    print(f"wrote {out_path} ({len(ordered)} fixtures from {len(job_results)} job(s))")

    if not args.no_open:
        webbrowser.open(out_path.as_uri())


if __name__ == "__main__":
    main()
