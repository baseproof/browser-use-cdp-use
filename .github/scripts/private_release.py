"""Publish the qualified private archives and independently read back both files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "https://us-python.pkg.dev/legalai-460612/baseproof-ai/"
PUBLISHER = "rpa-sdk-publisher@legalai-460612.iam.gserviceaccount.com"
READER = "rpa-sdk-reader@legalai-460612.iam.gserviceaccount.com"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def checked_bytes(path: Path, expected: str, size: int | None = None) -> bytes:
    require(
        path.is_file() and not path.is_symlink(), f"Expected regular file: {path.name}"
    )
    require(
        0 < path.stat().st_size <= MAX_ARCHIVE_BYTES, f"Invalid file size: {path.name}"
    )
    with path.open("rb") as stream:
        data = stream.read(MAX_ARCHIVE_BYTES + 1)
    require(len(data) <= MAX_ARCHIVE_BYTES, f"File exceeds limit: {path.name}")
    require(hashlib.sha256(data).hexdigest() == expected, f"Hash mismatch: {path.name}")
    require(size is None or len(data) == size, f"Length mismatch: {path.name}")
    return data


def verify(
    archives: Path, root: Path = ROOT
) -> tuple[str, dict[str, dict[str, str | int]]]:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    receipt = json.loads((root / "docs/qualification" / f"{version}.json").read_text())
    audit = json.loads(
        (root / "docs/qualification/source-archive-audit.json").read_text()
    )
    require(
        receipt["version"] == version
        and receipt["qualification_status"] == "installed_wheel_pass",
        "Missing installed qualification for this version",
    )
    require(
        audit["passed"] is True and audit["source_commit"] == receipt["source_commit"],
        "Source audit mismatch",
    )
    require(
        receipt["totals"]["failed"]
        == receipt["totals"]["errors"]
        == receipt["totals"]["skipped"]
        == 0,
        "Installed qualification contains failed or skipped cases",
    )
    require(receipt["totals"]["passed"] > 0, "Empty installed qualification")
    require(
        receipt["coverage"]["percent"] >= receipt["coverage"]["required_percent"],
        "Coverage gate failed",
    )
    for name, digest in audit["source_file_sha256"].items():
        checked_bytes(root / name, digest)
    recorded_modules = {
        name for name in audit["source_file_sha256"] if name.startswith("cdp_use/")
    }
    actual_modules = {
        str(path.relative_to(root)) for path in (root / "cdp_use").rglob("*.py")
    }
    require(actual_modules == recorded_modules, "Package source inventory changed")
    recorded_tests = {item["file"] for item in receipt["tests"]}
    require(
        recorded_tests
        == {str(path.relative_to(root)) for path in (root / "tests").glob("test_*.py")},
        "Qualified test inventory changed",
    )
    for item in receipt["tests"]:
        checked_bytes(root / item["file"], item["source_sha256"])
    expected = {item["filename"]: item for item in receipt["archives"]}
    names = {f"cdp_use-{version}-py3-none-any.whl", f"cdp_use-{version}.tar.gz"}
    require(
        set(expected) == set(audit["archive_sha256"]) == names,
        "Archive inventory mismatch",
    )
    require(
        {path.name for path in archives.iterdir()} == names,
        "Expected exactly the qualified wheel and sdist",
    )
    for name, item in expected.items():
        require(
            item["sha256"] == audit["archive_sha256"][name],
            f"Archive audit mismatch: {name}",
        )
        checked_bytes(archives / name, item["sha256"], item["bytes"])
    return version, expected


def credential_environment(path: Path, expected_identity: str) -> dict[str, str]:
    credential = json.loads(path.read_text())
    require(
        credential["type"] == "service_account"
        and credential["client_email"] == expected_identity,
        "Publication credential identity mismatch",
    )
    return {**os.environ, "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": str(path.resolve())}


def read_published(
    version: str,
    expected: dict,
    archives: Path,
    destination: Path,
    environment: dict[str, str],
) -> dict:
    destination.mkdir(parents=True, exist_ok=False)
    metadata = subprocess.check_output(
        [
            "gcloud",
            "artifacts",
            "files",
            "list",
            "--package=cdp-use",
            f"--version={version}",
            "--repository=baseproof-ai",
            "--location=us",
            "--project=legalai-460612",
            "--format=json(name,sizeBytes)",
        ],
        env=environment,
    )
    rows = json.loads(metadata)
    require(isinstance(rows, list) and len(rows) <= 2, "Unexpected registry inventory")
    (destination / "metadata.json").write_bytes(metadata)
    found = {}
    for row in rows:
        name = unquote(row["name"]).rsplit("/", 1)[1]
        require(name in expected and name not in found, "Unexpected registry filename")
        item = expected[name]
        require(
            int(row["sizeBytes"]) == item["bytes"], f"Registry size mismatch: {name}"
        )
        subprocess.run(
            [
                "gcloud",
                "artifacts",
                "files",
                "download",
                row["name"],
                f"--destination={destination}",
                f"--local-filename={name}",
            ],
            env=environment,
            check=True,
        )
        data = checked_bytes(destination / name, item["sha256"], item["bytes"])
        require(
            data == (archives / name).read_bytes(), f"Registry bytes differ: {name}"
        )
        found[name] = {
            "sha256": item["sha256"],
            "bytes": len(data),
            "resource": row["name"],
        }
    return found


def publish(
    archives: Path, output: Path, publisher_key: Path, reader_key: Path
) -> None:
    version, expected = verify(archives)
    output.mkdir(parents=True, exist_ok=False)
    publisher = credential_environment(publisher_key, PUBLISHER)
    reader = credential_environment(reader_key, READER)
    before = read_published(version, expected, archives, output / "before", reader)
    missing = sorted(set(expected) - set(before))
    upload_exit = 0
    if missing:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], env=publisher, text=True
        ).strip()
        require(bool(token), "Publisher token unavailable")
        upload = subprocess.run(
            [
                sys.executable,
                "-m",
                "twine",
                "upload",
                "--non-interactive",
                "--repository-url",
                REPOSITORY,
                *[str(archives / name) for name in missing],
            ],
            env={
                **publisher,
                "TWINE_USERNAME": "oauth2accesstoken",
                "TWINE_PASSWORD": token,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        upload_exit = upload.returncode
    after = read_published(version, expected, archives, output / "after", reader)
    complete = set(after) == set(expected)
    record = {
        "package": "cdp-use",
        "version": version,
        "repository": REPOSITORY,
        "publisher_identity": PUBLISHER,
        "reader_identity": READER,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "requested_uploads": missing,
        "upload_exit": upload_exit,
        "independently_verified": after,
        "complete": complete,
    }
    (output / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
    require(
        upload_exit == 0 and complete,
        "Publication incomplete; inspect retained readback before retrying",
    )
    print(
        json.dumps(
            {"version": version, "verified_archives": len(after), "complete": True}
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify", "publish"))
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publisher-key", type=Path)
    parser.add_argument("--reader-key", type=Path)
    args = parser.parse_args()
    if args.operation == "verify":
        version, expected = verify(args.archives)
        print(json.dumps({"version": version, "verified_archives": sorted(expected)}))
        return
    require(
        args.output is not None
        and args.publisher_key is not None
        and args.reader_key is not None,
        "Publication requires output and distinct publisher/reader credentials",
    )
    publish(args.archives, args.output, args.publisher_key, args.reader_key)


if __name__ == "__main__":
    main()
