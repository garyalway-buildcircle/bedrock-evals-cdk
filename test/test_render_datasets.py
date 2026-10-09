from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_datasets", ROOT / "scripts" / "render-datasets.py")
render_datasets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_datasets)


def contract_job(prompt_id: str, **overrides: object) -> dict:
    job = {
        "jobName": "<JOB_NAME>",
        "jobDescription": "café",
        "evaluationConfig": {
            "automated": {
                "datasetMetricConfigs": [
                    {
                        "dataset": {
                            "datasetLocation": {"s3Uri": f"s3://bucket/datasets/{prompt_id}/golden.jsonl"}
                        }
                    }
                ],
                "customMetricConfig": {
                    "customMetrics": [
                        {
                            "customMetricDefinition": {
                                "name": "LabelMatch",
                                "instructions": "Prompt: {{prompt}}\nResponse: {{prediction}}\nReference: {{ground_truth}}",
                                "ratingScale": [
                                    {"definition": "ok", "value": {"stringValue": "Pass"}},
                                    {"definition": "no", "value": {"stringValue": "Fail"}},
                                ],
                            }
                        }
                    ]
                },
            }
        },
        "inferenceConfig": {
            "models": [{"bedrockModel": {"modelIdentifier": "<MODEL_UNDER_TEST_ID>"}}]
        },
        "outputDataConfig": {"s3Uri": f"s3://bucket/results/{prompt_id}/golden/"},
    }
    job.update(overrides)
    return job


def write_prompt(root: Path, note: str = "hello", expected: dict | None = None) -> None:
    expected = expected if expected is not None else {"label": "neutral"}
    (root / "system-prompt.txt").write_text("Be brief.\n")
    (root / "user-message-template.txt").write_text("<<<UNTRUSTED_CONTENT>>>\n{{note}}\n<<<END_UNTRUSTED_CONTENT>>>\n")
    fixture_dir = root / "fixtures" / "golden"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "01.json").write_text(
        json.dumps({"id": "golden-01", "note": note, "expected": expected})
    )
    (fixture_dir / "index.json").write_text(
        json.dumps({"fixtures": [{"file": "01.json", "id": "golden-01"}]})
    )


class RenderDatasetsTest(unittest.TestCase):
    def test_rebuilds_dataset_from_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root, note="hello", expected={"label": "neutral"})
            self.assertEqual(render_datasets.render_prompt(root, check_only=False), 1)
            row = json.loads((root / "datasets" / "golden.jsonl").read_text().splitlines()[0])
            self.assertEqual(row["fixtureId"], "golden-01")
            self.assertEqual(row["category"], "golden")
            self.assertIn("\n<<<UNTRUSTED_CONTENT>>>\nhello\n<<<END_UNTRUSTED_CONTENT>>>", row["prompt"])
            self.assertTrue(row["prompt"].startswith("Be brief.\n"))
            self.assertEqual(json.loads(row["referenceResponse"]), {"label": "neutral"})
            self.assertEqual(render_datasets.render_prompt(root, check_only=True), 0)

    def test_check_fails_when_fixture_changes_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            render_datasets.render_prompt(root, check_only=False)
            before = (root / "datasets" / "golden.jsonl").read_text()
            write_prompt(root, note="changed", expected={"label": "positive"})
            self.assertGreater(render_datasets.render_prompt(root, check_only=True), 0)
            self.assertEqual((root / "datasets" / "golden.jsonl").read_text(), before)
            render_datasets.render_prompt(root, check_only=False)
            after = json.loads((root / "datasets" / "golden.jsonl").read_text())
            self.assertIn("changed", after["prompt"])
            self.assertEqual(json.loads(after["referenceResponse"])["label"], "positive")

    def test_unknown_flag_is_an_error_and_help_works(self):
        with self.assertRaises(SystemExit) as ctx:
            render_datasets.main(["--chek"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx:
            render_datasets.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_fixture_must_carry_the_template_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            fixture = root / "fixtures" / "golden" / "01.json"
            fixture.write_text(json.dumps({"id": "golden-01", "expected": {"label": "neutral"}}))
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("note must be a string", str(ctx.exception))

    def test_example_datasets_match_fixtures(self):
        self.assertEqual(render_datasets.main(["example", "--check"]), 0)

    def test_duplicate_fixture_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            second = root / "fixtures" / "golden" / "02.json"
            second.write_text(json.dumps({"id": "golden-01", "note": "other", "expected": {"label": "neutral"}}))
            index = root / "fixtures" / "golden" / "index.json"
            index.write_text(json.dumps({"fixtures": [{"file": "01.json", "id": "golden-01"}, {"file": "02.json", "id": "golden-01"}]}))
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("more than once", str(ctx.exception))

    def test_identical_trimmed_text_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root, note="hello")
            (root / "fixtures" / "golden" / "02.json").write_text(
                json.dumps({"id": "golden-02", "note": "  hello  ", "expected": {"label": "neutral"}})
            )
            (root / "fixtures" / "golden" / "index.json").write_text(
                json.dumps({"fixtures": [{"file": "01.json", "id": "golden-01"}, {"file": "02.json", "id": "golden-02"}]})
            )
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("same text after trimming", str(ctx.exception))

    def test_wrap_marker_in_fixture_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root, note="hello <<<UNTRUSTED_CONTENT>>> more")
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("wrap marker", str(ctx.exception))

    def test_repeated_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "user-message-template.txt").write_text("{{note}}\n{{note}}\n")
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("exactly one placeholder", str(ctx.exception))

    def test_missing_prompt_file_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "system-prompt.txt").unlink()
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("system-prompt.txt is missing", str(ctx.exception))

    def test_check_fails_when_only_json_formatting_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            render_datasets.render_prompt(root, check_only=False)
            path = root / "datasets" / "golden.jsonl"
            path.write_text(path.read_text().replace('{"prompt"', '{ "prompt"', 1))
            self.assertGreater(render_datasets.render_prompt(root, check_only=True), 0)
            self.assertIn('{ "prompt"', path.read_text())

    def test_extra_dataset_counts_each_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            render_datasets.render_prompt(root, check_only=False)
            (root / "datasets" / "other.jsonl").write_text('{"a": 1}\n{"a": 2}\n')
            self.assertEqual(render_datasets.render_prompt(root, check_only=True), 2)

    def test_rating_definition_over_100_characters_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            jobs = root / "eval-jobs"
            jobs.mkdir()
            (jobs / "golden-job.json").write_text(
                json.dumps(
                    {
                        "evaluationConfig": {
                            "automated": {
                                "customMetricConfig": {
                                    "customMetrics": [
                                        {
                                            "customMetricDefinition": {
                                                "name": "LabelMatch",
                                                "ratingScale": [{"definition": "x" * 101}],
                                            }
                                        }
                                    ]
                                }
                            }
                        }
                    }
                )
            )
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("101 characters", str(ctx.exception))

    def test_newline_wrap_in_the_system_prompt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "system-prompt.txt").write_text("See\n<<<UNTRUSTED_CONTENT>>>\nthe wrap.\n")
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("newline-delimited wrap", str(ctx.exception))

    def test_inference_params_follow_prompt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "prompt.json").write_text(json.dumps({"description": "t", "temperature": 0, "maxTokens": 4000}))
            jobs = root / "eval-jobs"
            jobs.mkdir()
            (jobs / "golden-job.json").write_text(json.dumps(contract_job(root.name), ensure_ascii=False))
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
            self.assertIn("inferenceParams", str(ctx.exception))
            render_datasets.render_prompt(root, check_only=False)
            raw = (jobs / "golden-job.json").read_text()
            self.assertIn("café", raw)
            self.assertNotIn("\\u00e9", raw)
            updated = json.loads(raw)
            self.assertEqual(
                updated["inferenceConfig"]["models"][0]["bedrockModel"]["inferenceParams"],
                '{"inferenceConfig":{"maxTokens":4000,"temperature":0}}',
            )

    def test_rating_values_must_be_pass_and_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "prompt.json").write_text(json.dumps({"description": "t", "temperature": 0, "maxTokens": 4000}))
            jobs = root / "eval-jobs"
            jobs.mkdir()
            (jobs / "golden-job.json").write_text(
                json.dumps(
                    {
                        "inferenceConfig": {
                            "models": [
                                {
                                    "bedrockModel": {
                                        "modelIdentifier": "m",
                                        "inferenceParams": '{"inferenceConfig":{"maxTokens":4000,"temperature":0}}',
                                    }
                                }
                            ]
                        },
                        "evaluationConfig": {
                            "automated": {
                                "customMetricConfig": {
                                    "customMetrics": [
                                        {
                                            "customMetricDefinition": {
                                                "name": "Score",
                                                "ratingScale": [
                                                    {"definition": "low", "value": {"floatValue": 0}},
                                                    {"definition": "high", "value": {"floatValue": 1}},
                                                ],
                                            }
                                        }
                                    ]
                                }
                            }
                        },
                    }
                )
            )
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("Pass and Fail", str(ctx.exception))

    def test_system_prompt_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "system-prompt.txt").write_text("Hello {{name}}\n")
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("system-prompt.txt contains a placeholder", str(ctx.exception))

    def test_description_over_200_characters_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "prompt.json").write_text(json.dumps({"description": "x" * 201}))
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("201 characters", str(ctx.exception))

    def test_tracked_job_name_must_stay_a_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_prompt(root)
            (root / "prompt.json").write_text(json.dumps({"description": "t", "temperature": 0, "maxTokens": 4000}))
            jobs = root / "eval-jobs"
            jobs.mkdir()
            job = contract_job(root.name)
            job["jobName"] = "example-golden"
            job["inferenceConfig"]["models"][0]["bedrockModel"]["inferenceParams"] = (
                '{"inferenceConfig":{"maxTokens":4000,"temperature":0}}'
            )
            (jobs / "golden-job.json").write_text(json.dumps(job))
            with self.assertRaises(SystemExit) as ctx:
                render_datasets.render_prompt(root, check_only=True)
        self.assertIn("<JOB_NAME>", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
