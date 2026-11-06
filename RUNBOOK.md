# cdp-use private release runbook

This maintained fork publishes distinctly versioned patches to Baseproof's
private Artifact Registry. Follow [the release contract](docs/private-release.md).
It does not publish upstream versions or use public PyPI trusted publishing.

1. Prepare the next explicit local version and lockfile in a dedicated branch.
2. Build and qualify the exact wheel and sdist; record source, test and archive
   hashes. Review and merge the source plus qualification evidence.
3. Dispatch `private-publication.yml` on this repository's main branch. The
   preparation job must reproduce the accepted archives. The publication job
   uses those retained bytes and independently verifies both registry downloads.
4. Create the matching version tag and GitHub release only after successful
   private publication and readback. Attach the archives and publication receipt.

For a failed publication, inspect its retained receipt and registry readback
before retrying. Existing files must match the accepted bytes; never overwrite a
published version. Consumers require explicit version/hash pins and their own
installed-image qualification.
