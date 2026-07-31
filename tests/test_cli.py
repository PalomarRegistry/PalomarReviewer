import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from palomar_reviewer.cli import (
    ReviewerError,
    STEP_SCHEMA,
    SYNTHESIS_SCHEMA,
    authors_from_metadata,
    has_proof_account,
    parse_engine_json,
    publication_entry_path,
    render_bundle_manifest,
    registry_title,
    reviewer_model,
    validate_render_result,
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

    def test_publication_entry_path(self):
        pr = {
            "files": [
                {"path": "entries/PALOMAR-000012-v2.json"},
                {"path": "index.json"},
            ]
        }
        self.assertEqual(publication_entry_path(pr), "entries/PALOMAR-000012-v2.json")

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

    def test_render_result_is_bound_to_source_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory)
            bundle = result / "bundle"
            entrypoint = bundle / "Challenge" / "index.html"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("<html>safe</html>", encoding="utf-8")
            files, tree_hash = render_bundle_manifest(bundle)
            (bundle / "artifact-manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifact_tree_sha256": tree_hash,
                        "files": files,
                    }
                ),
                encoding="utf-8",
            )
            source = {
                "repository": "example/challenge",
                "repository_url": "https://github.com/example/challenge",
                "commit": "1" * 40,
                "challenge_sha256": hashlib.sha256(b"Challenge").hexdigest(),
            }
            report = {
                "status": "pass",
                "source": source,
                "format": "verso-html",
                "entrypoint": "Challenge/index.html",
                "artifact_tree_sha256": tree_hash,
                "verso_commit": "2" * 40,
                "renderer_commit": "3" * 40,
                "landrun_commit": "4" * 40,
                "rendered_at": "2026-07-31T00:00:00Z",
            }
            (result / "challenge-render.json").write_text(json.dumps(report), encoding="utf-8")
            mechanical = {
                "source": {
                    "repository": source["repository"],
                    "repository_url": source["repository_url"],
                    "commit": source["commit"],
                },
                "challenge": {"sha256": source["challenge_sha256"]},
            }
            validated, validated_bundle = validate_render_result(result, mechanical)
            self.assertEqual(validated["artifact_tree_sha256"], tree_hash)
            self.assertEqual(validated_bundle, bundle)

            with mock.patch("palomar_reviewer.cli.MAX_RENDER_FILE_BYTES", 1):
                with self.assertRaisesRegex(ReviewerError, "size cap"):
                    validate_render_result(result, mechanical)

            report["source"]["commit"] = "9" * 40
            (result / "challenge-render.json").write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ReviewerError, "does not match"):
                validate_render_result(result, mechanical)

    def test_missing_render_report_is_a_reviewer_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ReviewerError, "no valid challenge-render.json"):
                validate_render_result(Path(directory), {})


if __name__ == "__main__":
    unittest.main()
