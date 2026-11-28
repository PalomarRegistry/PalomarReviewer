"""A confidential review reads the Server's tarball, never the repository (contract v3.1, C2 to C4)."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_confidential import (
    PIPELINE,
    PUBLIC_URL,
    RUNS_URL,
    SUBMISSION,
    confidential_state,
    mechanical_report,
)
from test_confidential_redaction import BUCKET_ENV, FakeR2

import palomar_reviewer.cli as cli
import palomar_reviewer.mechanical as mechanical_evidence
import palomar_reviewer.private_bucket as private_bucket
from palomar_reviewer.errors import RenderRequestError, ReviewerError


def tarball(files: dict[str, bytes], *, top="canary-owner-repo-3333333", extra=()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as bundle:
        bundle.pax_headers = {"comment": "3" * 40}
        directory = tarfile.TarInfo(top)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        bundle.addfile(directory)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        for info in extra:
            bundle.addfile(info, io.BytesIO(b"x" * info.size))
    return buffer.getvalue()


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.r2 = FakeR2()
        cli._BUCKET.clear()
        cli._BUCKET.append(private_bucket.Bucket(BUCKET_ENV, runner=self.r2))
        self.addCleanup(cli._BUCKET.clear)
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory))

    def upload(self, data: bytes) -> dict:
        self.r2.objects[private_bucket.source_key(SUBMISSION)] = data
        digest = hashlib.sha256(data).hexdigest()
        state = confidential_state(confidential={"source_sha256": digest})
        return state, {**mechanical_report(), "source_sha256": digest}


class RestoreTests(SourceCase):
    def test_the_tarball_becomes_the_checkout(self):
        state, mechanical = self.upload(tarball({"Challenge.lean": b"theorem x : True := trivial\n",
                                                 "sub/dir/README.md": b"readme"}))
        destination = self.directory / "source"
        with mock.patch.object(cli, "clone_at", side_effect=AssertionError("cloned")):
            cli.restore_confidential_source(state, mechanical, destination)
        self.assertEqual((destination / "Challenge.lean").read_bytes(), b"theorem x : True := trivial\n")
        self.assertEqual((destination / "sub/dir/README.md").read_bytes(), b"readme")
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["source"])

    def test_a_tarball_that_does_not_match_its_digest_is_refused(self):
        state, mechanical = self.upload(tarball({"a": b"1"}))
        self.r2.objects[private_bucket.source_key(SUBMISSION)] = tarball({"a": b"2"})
        with self.assertRaisesRegex(ReviewerError, "does not match its recorded digest"):
            cli.restore_confidential_source(state, mechanical, self.directory / "source")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_a_report_of_another_tarball_is_refused(self):
        state, mechanical = self.upload(tarball({"a": b"1"}))
        with self.assertRaisesRegex(ReviewerError, "different source tarball"):
            cli.restore_confidential_source(
                state, {**mechanical, "source_sha256": "f" * 64}, self.directory / "source"
            )

    def test_escaping_members_are_refused(self):
        escape = tarfile.TarInfo("canary-owner-repo-3333333/../../escape")
        escape.size = 1
        link = tarfile.TarInfo("canary-owner-repo-3333333/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        for member in (escape, link):
            with self.subTest(member=member.name):
                state, mechanical = self.upload(tarball({"a": b"1"}, extra=(member,)))
                with self.assertRaises(ReviewerError):
                    cli.restore_confidential_source(state, mechanical, self.directory / "source")
                self.assertFalse((self.directory.parent / "escape").exists())

    def test_two_top_directories_are_refused(self):
        other = tarfile.TarInfo("other/file")
        other.size = 1
        state, mechanical = self.upload(tarball({"a": b"1"}, extra=(other,)))
        with self.assertRaisesRegex(ReviewerError, "exactly one top directory"):
            cli.restore_confidential_source(state, mechanical, self.directory / "source")


class ReportBindingTests(unittest.TestCase):
    def run_data(self, url=RUNS_URL):
        return {"url": url, "headSha": "2b" * 20, "event": "workflow_dispatch", "pipelineCommit": PIPELINE}

    def test_a_confidential_report_must_name_the_recorded_tarball(self):
        for digest in (None, "f" * 64):
            report = {key: value for key, value in mechanical_report().items() if key != "source_sha256"}
            if digest:
                report["source_sha256"] = digest
            with self.subTest(digest=digest), self.assertRaisesRegex(
                ReviewerError, "different source tarball"
            ):
                mechanical_evidence.validate_report_contract(report, confidential_state(), self.run_data())

    def test_a_public_report_cannot_name_a_private_tarball(self):
        public = {key: value for key, value in confidential_state().items()
                  if key not in {"visibility", "confidential"}}
        report = {**mechanical_report(PUBLIC_URL), "pipeline_commit": "2b" * 20}
        run_data = {**self.run_data(PUBLIC_URL), "pipelineCommit": None}
        with self.assertRaisesRegex(ReviewerError, "private source tarball"):
            mechanical_evidence.validate_report_contract(report, public, run_data)


class RenderInputTests(SourceCase):
    def test_a_confidential_render_gets_presigned_reads_and_the_digest(self):
        _state, mechanical = self.upload(tarball({"a": b"1"}))
        inputs = cli.confidential_render_inputs(mechanical)
        values = dict(item.split("=", 1) for item in inputs[1::2])
        self.assertEqual(set(values), {"source_url", "source_sha256", "tombstone_url"})
        self.assertEqual(values["source_sha256"], mechanical["source_sha256"])
        self.assertIn(f"conf/{SUBMISSION}/source.tar.gz?X-Amz-Expires=86400", values["source_url"])
        self.assertIn(f"conf-tombstones/{SUBMISSION}?X-Amz-Expires=604800", values["tombstone_url"])

    def test_a_withdrawn_submission_is_never_rendered(self):
        _state, mechanical = self.upload(tarball({"a": b"1"}))
        tombstone = json.dumps({"cleanup": "pending"}).encode()
        self.r2.objects[private_bucket.tombstone_key(SUBMISSION)] = tombstone
        with self.assertRaisesRegex(RenderRequestError, "withdrawn"):
            cli.confidential_render_inputs(mechanical)
        self.assertFalse(any(call[3:5] == ["s3", "presign"] for call in self.r2.calls))


if __name__ == "__main__":
    unittest.main()
