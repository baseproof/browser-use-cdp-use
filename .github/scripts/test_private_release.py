"""Offline refusal checks; real registry readback remains the publication gate."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import private_release as release


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archives = self.root / "dist"
        self.archives.mkdir()
        (self.root / "cdp_use").mkdir()
        (self.root / "tests").mkdir()
        self.records = self.root / "docs/qualification"
        self.records.mkdir(parents=True)
        self.version = "1.4.5+baseproof.1"
        (self.root / "pyproject.toml").write_text(
            f'[project]\nversion = "{self.version}"\n'
        )
        (self.root / ".gitignore").write_text("dist/\n")
        (self.root / "cdp_use/__init__.py").write_text("qualified = True\n")
        (self.root / "tests/test_owner.py").write_text("def test_owner(): pass\n")
        names = (
            f"cdp_use-{self.version}-py3-none-any.whl",
            f"cdp_use-{self.version}.tar.gz",
        )
        self.expected = {}
        for name in names:
            data = ("retained " + name).encode()
            (self.archives / name).write_bytes(data)
            self.expected[name] = {
                "filename": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        self.receipt = {
            "version": self.version,
            "source_commit": "a" * 40,
            "qualification_status": "installed_wheel_pass",
            "totals": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "coverage": {"percent": 90, "required_percent": 80},
            "tests": [
                {
                    "file": "tests/test_owner.py",
                    "source_sha256": self.digest("tests/test_owner.py"),
                }
            ],
            "archives": list(self.expected.values()),
        }
        self.audit = {
            "passed": True,
            "source_commit": "a" * 40,
            "archive_sha256": {
                name: item["sha256"] for name, item in self.expected.items()
            },
            "source_file_sha256": {
                name: self.digest(name)
                for name in (".gitignore", "pyproject.toml", "cdp_use/__init__.py")
            },
        }
        self.write_receipts()

    def digest(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def write_receipts(self):
        (self.records / f"{self.version}.json").write_text(json.dumps(self.receipt))
        (self.records / "source-archive-audit.json").write_text(json.dumps(self.audit))

    def test_accepts_only_the_recorded_source_tests_and_archive_bytes(self):
        self.assertEqual(
            release.verify(self.archives, self.root), (self.version, self.expected)
        )

    def test_rejects_source_and_test_changes(self):
        for name in (".gitignore", "cdp_use/__init__.py", "tests/test_owner.py"):
            with self.subTest(name=name):
                original = (self.root / name).read_bytes()
                (self.root / name).write_bytes(original + b"# changed\n")
                with self.assertRaisesRegex(ValueError, "Hash mismatch"):
                    release.verify(self.archives, self.root)
                (self.root / name).write_bytes(original)

    def test_rejects_unqualified_extra_module(self):
        (self.root / "cdp_use/new.py").write_text("extra = True\n")
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            release.verify(self.archives, self.root)

    def test_rejects_corrupt_or_extra_archive(self):
        name = next(iter(self.expected))
        original = (self.archives / name).read_bytes()
        (self.archives / name).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Hash mismatch"):
            release.verify(self.archives, self.root)
        (self.archives / name).write_bytes(original)
        (self.archives / "unexpected.whl").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "exactly"):
            release.verify(self.archives, self.root)

    def test_rejects_failed_qualification(self):
        self.receipt["totals"]["failed"] = 1
        self.write_receipts()
        with self.assertRaisesRegex(ValueError, "failed or skipped"):
            release.verify(self.archives, self.root)

    def test_rejects_wrong_credential_identity(self):
        key = self.root / "fake-identity.json"
        key.write_text(
            json.dumps({"type": "service_account", "client_email": release.READER})
        )
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            release.credential_environment(key, release.PUBLISHER)

    def test_existing_registry_mismatch_is_refused_before_download(self):
        name = next(iter(self.expected))
        metadata = json.dumps(
            [{"name": "files/" + name, "sizeBytes": self.expected[name]["bytes"] + 1}]
        ).encode()
        with (
            patch.object(release.subprocess, "check_output", return_value=metadata),
            patch.object(release.subprocess, "run") as download,
        ):
            with self.assertRaisesRegex(ValueError, "Registry size mismatch"):
                release.read_published(
                    self.version,
                    self.expected,
                    self.archives,
                    self.root / "readback",
                    {},
                )
            download.assert_not_called()

    def test_existing_registry_bytes_are_verified_with_reader_environment(self):
        name = next(iter(self.expected))
        metadata = json.dumps(
            [{"name": "files/" + name, "sizeBytes": self.expected[name]["bytes"]}]
        ).encode()
        destination = self.root / "readback"
        reader = {"READER_IDENTITY_TEST": "reader"}

        def download(command, *, env, check):
            self.assertEqual(env, reader)
            self.assertTrue(check)
            (destination / name).write_bytes((self.archives / name).read_bytes())

        with (
            patch.object(
                release.subprocess, "check_output", return_value=metadata
            ) as listing,
            patch.object(release.subprocess, "run", side_effect=download),
        ):
            result = release.read_published(
                self.version, self.expected, self.archives, destination, reader
            )
            self.assertEqual(set(result), {name})
            self.assertEqual(listing.call_args.kwargs["env"], reader)


if __name__ == "__main__":
    unittest.main()
