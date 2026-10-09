import importlib.util
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
        self.assertIn("unknown-fixture-2", str(ctx.exception))

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


if __name__ == "__main__":
    unittest.main()
