import tempfile
import unittest
from pathlib import Path

from palomar_reviewer.cli import (
    STEP_SCHEMA,
    SYNTHESIS_SCHEMA,
    authors_from_metadata,
    has_proof_account,
    parse_engine_json,
    registry_title,
    reviewer_model,
)


class ReviewerTests(unittest.TestCase):
    def test_json_fence_fallback(self):
        value = parse_engine_json('result\n```json\n{"step":"metadata"}\n```')
        self.assertEqual(value["step"], "metadata")

    def test_authors(self):
        data = {"project": {"authors": ["Ada", {"name": "Emmy", "github": "@emmy"}]}}
        self.assertEqual(
            authors_from_metadata(data, "fallback"),
            [{"name": "Ada"}, {"name": "Emmy", "github": "emmy"}],
        )

    def test_proof_account_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "formalization.yaml").write_text("proof_description: classical induction\n")
            self.assertTrue(has_proof_account(root))

    def test_model_id(self):
        self.assertEqual(reviewer_model("codex", "gpt-test", None), "codex:gpt-test")

    def test_registry_title_prefers_human_text(self):
        metadata = {"result": {"name": "machine_readable_name"}}
        self.assertEqual(
            registry_title(metadata, "[submission] A human-readable result"),
            "A human-readable result",
        )
        metadata["result"]["title"] = "Explicit metadata title"
        self.assertEqual(
            registry_title(metadata, "[submission] A human-readable result"),
            "Explicit metadata title",
        )

    def test_engine_schemas_are_strict(self):
        def assert_strict(schema):
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for value in schema.values():
                if isinstance(value, dict):
                    assert_strict(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            assert_strict(item)

        assert_strict(STEP_SCHEMA)
        assert_strict(SYNTHESIS_SCHEMA)


if __name__ == "__main__":
    unittest.main()
