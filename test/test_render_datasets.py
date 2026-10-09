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


if __name__ == "__main__":
    unittest.main()
