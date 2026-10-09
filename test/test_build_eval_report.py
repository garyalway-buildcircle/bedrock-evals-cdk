import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_eval_report", ROOT / "scripts" / "build-eval-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def wrapped(note: str) -> str:
    return f"sys\n<<<UNTRUSTED_CONTENT>>>\n{note}\n<<<END_UNTRUSTED_CONTENT>>>"


def result_row(note: str, scores=None, response="{\"label\":\"neutral\"}"):
    row = {"inputRecord": {"prompt": wrapped(note)}}
    if response is not None:
        row["modelResponses"] = [{"response": response}]
    if scores is not None:
        row["automatedEvaluationResult"] = {"scores": scores}
    return row


class BuildEvalReportTest(unittest.TestCase):
    def test_output_keys_span_pages_and_ignore_other_objects(self):
        keys = report.output_keys_from_pages(
            [
                {"Contents": [{"Key": "a/job_output.jsonl"}, {"Key": "a/readme.txt"}]},
                {"Contents": [{"Key": "b/other_output.jsonl"}]},
                {},
            ]
        )
        self.assertEqual(keys, ["a/job_output.jsonl", "b/other_output.jsonl"])

    def test_json_for_html_has_no_raw_angle_brackets(self):
        encoded = report.json_for_html({"response": "</script><img>"})
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertIn("\\u003c", encoded)

    def test_missing_scores_and_response_do_not_raise(self):
        self.assertEqual(report.metrics_for({})["Score"]["result"], "Fail")
        self.assertEqual(report.model_response({}), "")
        self.assertEqual(report.model_response({"modelResponses": []}), "")

    def test_any_unmatched_row_fails_the_report(self):
        datasets = [
            {
                "prompt": wrapped("hello"),
                "fixtureId": "golden-01",
                "referenceResponse": "{}",
                "category": "golden",
            }
        ]
        rows = [
            result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}]),
            result_row("other", scores=[{"metricName": "LabelMatch", "result": "Fail"}]),
        ]
        with self.assertRaises(SystemExit) as ctx:
            report.build_cases(
                [{"job_name": "job", "model_id": "model", "rows": rows}],
                datasets,
                {"golden-01": {"category": "golden", "description": "", "rule": ""}},
            )
        self.assertIn("1 of 2", str(ctx.exception))
        self.assertIn("row 2", str(ctx.exception))

    def test_dataset_category_keeps_a_row_when_the_fixture_file_is_gone(self):
        datasets = [
            {
                "prompt": wrapped("hello"),
                "fixtureId": "edge-01",
                "referenceResponse": "{}",
                "category": "edge-case",
            }
        ]
        cases = report.build_cases(
            [
                {
                    "job_name": "job",
                    "model_id": "model",
                    "rows": [result_row("hello", scores=None, response=None)],
                }
            ],
            datasets,
            {},
        )
        self.assertEqual(cases["edge-01"]["category"], "edge")
        self.assertEqual(cases["edge-01"]["versions"]["run1"]["response"], "")
        self.assertEqual(cases["edge-01"]["versions"]["run1"]["metrics"]["Score"]["result"], "Fail")

    def test_identical_inputs_fail_instead_of_collapsing(self):
        note = wrapped("hello")
        datasets = [
            {"prompt": note, "fixtureId": "golden-01", "referenceResponse": "{}", "category": "golden"},
            {"prompt": note, "fixtureId": "golden-02", "referenceResponse": "{}", "category": "golden"},
        ]
        with self.assertRaises(SystemExit) as ctx:
            report.build_cases(
                [{"job_name": "job", "model_id": "model", "rows": [result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}])]}],
                datasets,
                {},
            )
        self.assertIn("more than one dataset fixture", str(ctx.exception))

    def test_the_same_fixture_twice_in_one_job_fails(self):
        datasets = [
            {"prompt": wrapped("hello"), "fixtureId": "golden-01", "referenceResponse": "{}", "category": "golden"},
        ]
        rows = [
            result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}]),
            result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Fail"}]),
        ]
        with self.assertRaises(SystemExit) as ctx:
            report.build_cases(
                [{"job_name": "job", "model_id": "model", "rows": rows}],
                datasets,
                {"golden-01": {"category": "golden", "description": "", "rule": ""}},
            )
        self.assertIn("matched more than once", str(ctx.exception))

    def test_a_job_with_no_rows_fails(self):
        with self.assertRaises(SystemExit) as ctx:
            report.build_cases(
                [{"job_name": "job", "model_id": "model", "rows": []}],
                [{"prompt": wrapped("hello"), "fixtureId": "golden-01", "referenceResponse": "{}", "category": "golden"}],
                {},
            )
        self.assertIn("no rows", str(ctx.exception))

    def test_a_scored_set_missing_a_fixture_fails(self):
        datasets = [
            {"prompt": wrapped("hello"), "fixtureId": "golden-01", "referenceResponse": "{}", "category": "golden"},
            {"prompt": wrapped("other"), "fixtureId": "golden-02", "referenceResponse": "{}", "category": "golden"},
        ]
        with self.assertRaises(SystemExit) as ctx:
            report.build_cases(
                [{"job_name": "job", "model_id": "model", "rows": [result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}])]}],
                datasets,
                {},
            )
        self.assertIn("golden-02", str(ctx.exception))

    def test_missing_wrap_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx:
            report.extract_transcript("no wrap here")
        self.assertIn("missing or ambiguous", str(ctx.exception))

    def test_template_values_are_not_scanned_for_later_placeholders(self):
        page = report.fill_template(
            "A __TITLE__ B __DATA__ C __LEADS__",
            [("__TITLE__", "has __DATA__"), ("__DATA__", "ok"), ("__LEADS__", "lead")],
        )
        self.assertEqual(page, "A has __DATA__ B ok C lead")

    def test_load_datasets_reads_every_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "datasets").mkdir()
            (root / "datasets" / "golden.jsonl").write_text('{"fixtureId": "a"}\n', encoding="utf-8")
            (root / "datasets" / "other.jsonl").write_text('{"fixtureId": "b"}\n', encoding="utf-8")
            rows = report.load_datasets(root)
        self.assertEqual([row["fixtureId"] for row in rows], ["a", "b"])

    def test_wrap_marker_inside_the_input_is_ambiguous(self):
        prompt = "The wrap is <<<UNTRUSTED_CONTENT>>> … <<<END_UNTRUSTED_CONTENT>>>.\n<<<UNTRUSTED_CONTENT>>>\nhello <<<END_UNTRUSTED_CONTENT>>> hidden\n<<<END_UNTRUSTED_CONTENT>>>"
        with self.assertRaises(SystemExit) as ctx:
            report.extract_transcript(prompt)
        self.assertIn("ambiguous", str(ctx.exception))

    def test_example_dataset_row_extracts_the_note(self):
        row = json.loads((ROOT / "prompts" / "example" / "datasets" / "golden.jsonl").read_text().splitlines()[0])
        text = report.extract_transcript(row["prompt"])
        self.assertIn("kettle", text)
        self.assertNotIn("UNTRUSTED", text)

    def test_other_fixture_sets_keep_their_category(self):
        self.assertEqual(report.report_category("golden"), "golden")
        self.assertEqual(report.report_category("edge-case"), "edge")
        self.assertEqual(report.report_category("regression"), "regression")
        datasets = [
            {
                "prompt": wrapped("hello"),
                "fixtureId": "regression-01",
                "referenceResponse": "{}",
                "category": "regression",
            }
        ]
        cases = report.build_cases(
            [
                {
                    "job_name": "job",
                    "model_id": "model",
                    "rows": [result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}])],
                }
            ],
            datasets,
            {"regression-01": {"category": "regression", "description": "extra", "rule": ""}},
        )
        self.assertEqual(cases["regression-01"]["category"], "regression")

    def test_region_comes_from_the_job_arn(self):
        self.assertEqual(
            report.region_of_job_arn("arn:aws:bedrock:us-west-2:123456789012:evaluation-job/abc"),
            "us-west-2",
        )
        with self.assertRaises(SystemExit) as ctx:
            report.region_of_job_arn("not-an-arn")
        self.assertIn("not a Bedrock evaluation job ARN", str(ctx.exception))

    def test_fixture_notes_reach_the_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "fixtures" / "golden"
            folder.mkdir(parents=True)
            (folder / "01.json").write_text(
                json.dumps({"id": "golden-01", "description": "praise", "notes": "expectation corrected"}),
                encoding="utf-8",
            )
            meta = report.load_fixture_metadata(root)
        self.assertEqual(meta["golden-01"]["notes"], "expectation corrected")
        datasets = [
            {"prompt": wrapped("hello"), "fixtureId": "golden-01", "referenceResponse": "{}", "category": "golden"}
        ]
        cases = report.build_cases(
            [
                {
                    "job_name": "job",
                    "model_id": "model",
                    "rows": [result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}])],
                }
            ],
            datasets,
            meta,
        )
        self.assertEqual(cases["golden-01"]["notes"], "expectation corrected")

    def test_fixture_id_prefixed_unknown_fixture_still_matches(self):
        datasets = [
            {
                "prompt": wrapped("hello"),
                "fixtureId": "unknown-fixture-1",
                "referenceResponse": "{}",
                "category": "golden",
            }
        ]
        cases = report.build_cases(
            [
                {
                    "job_name": "job",
                    "model_id": "model",
                    "rows": [result_row("hello", scores=[{"metricName": "LabelMatch", "result": "Pass"}])],
                }
            ],
            datasets,
            {"unknown-fixture-1": {"category": "golden", "description": "", "rule": "", "notes": ""}},
        )
        self.assertIn("unknown-fixture-1", cases)


if __name__ == "__main__":
    unittest.main()
