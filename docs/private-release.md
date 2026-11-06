# Private observation patch release

The maintained source is `baseproof/browser-use-cdp-use`; upstream authorship
and MIT licensing are retained. The package remains `cdp-use`, with the distinct
private version `1.4.5+baseproof.1`. Browser-use 0.13.14's public-version pin
accepts this local version. Consumers must explicitly select and hash-pin it;
an upstream 1.4.5 installation does not provide the observation contract.

The extension observes transport messages and ownership identities. It does not
change navigation, waits, retries, scheduling or site policy. The caller owns
observer admission, storage, redaction and access policy. rpa-sdk supplies the
bounded delivery adapter; RPA supplies execution and artifact ownership.

## Qualification and publication

Build the sdist and wheel from the reviewed source once, and retain those exact
archives. Qualify the installed wheel with the real loopback WebSocket tests,
type/lint checks, original-source comparison and the consuming browser stack.
Record the source/test input hashes and both archive hashes under
`docs/qualification`. Include `.gitignore`: Hatchling includes it in the sdist.
Source and installed tests prove mechanisms; they do not prove arbitrary site
behavior or production capacity.

The manual `private-publication.yml` workflow is restricted to main and private
Artifact Registry `baseproof-ai`. Preparation reproduces only already-qualified
bytes; mismatched source, tests or archives stop publication. Its publication job
uses the retained preparation bundle, including a failed-job retry, and never
substitutes rebuilt archives. Existing registry files must match exactly.

The workflow uses the existing organization publisher and separate reader
identities. Both downloads must match their accepted hashes and lengths before
publication succeeds. No public PyPI publishing path remains. Version tags and
GitHub releases are created after private publication and readback succeed.

The qualified rpa-sdk wheel and immutable image must then select this exact
published patch and the published Bubus ownership patch. Their full installed
browser/provider/storage qualification remains required; this dependency release
alone does not update RPA's shipping image.
