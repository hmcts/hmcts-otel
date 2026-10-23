# Releasing & versioning

`hmcts-otel` is published to the HMCTS **`hmcts-lib`** Azure DevOps
Artifacts feed (the same feed as the HMCTS Java libraries) by
[`.github/workflows/build.yml`](../.github/workflows/build.yml). This is the
single source of truth for how versions are produced and published.

## Versioning scheme

The base version is always `version` in `pyproject.toml`.
[`scripts/artefact_version.sh`](../scripts/artefact_version.sh) computes the published
[PEP 440](https://peps.python.org/pep-0440/) version from it.
[`hmcts/artefact-version-action`](https://github.com/hmcts/artefact-version-action)
(the action the Java libraries use) supplies only the commit SHA for drafts and the tag
for releases. Its own draft base, the latest `v*` tag (`0.0.0` if there is none), is
not used.

| Trigger | Version | Example | Notes |
|---|---|---|---|
| **Push to `main`** (draft candidate) | `<pyproject version>.dev<run_number>+<sha>` | `0.2.1.dev47+238aa32` | Developmental release. `.dev<n>` sorts correctly across drafts; `+<sha>` is the commit (a hex SHA is only PEP 440-legal in the `+local` segment). Same form `setuptools-scm` produces. |
| **Published GitHub Release** | `<pyproject version>` (clean) | `0.2.1` | No `+local` segment. The release tag (`v0.2.1`) must match `pyproject.toml`, or the job fails. |

Drafts are PEP 440 pre-releases and sort before their base (`0.2.0 < 0.2.1.dev47 <
0.2.1`), so a draft of 0.2.1 satisfies `>=0.2.0` (a `0.0.0.dev<n>` draft never did).
Installers prefer a final release that satisfies the requirement; to install a draft,
pin it (`==0.2.1.dev47+238aa32`) or allow pre-releases (`pip --pre`,
`uv --prerelease=allow`). `pyproject.toml` must hold a release version (no `.dev` or
`+local` part); the publish job sets the computed version and checks the built wheel
carries it.

> **Why not just `X.Y.Z+sha`, `X.Y.Z-sha`, or `X.Y.Z_sha`?** PEP 440 forbids `-`
> and `_` as separators, and a hex SHA cannot follow a `.` (release/`.dev`/`.post`
> segments are numeric). A SHA is only valid in the `+local` segment — so keeping
> the SHA *requires* a `+`, and pairing it with `.dev<n>` makes it the recognised
> dev-build idiom rather than a bare local version.

## Cutting a release

1. Bump `version` in `pyproject.toml` (e.g. `0.2.0` → `0.2.1`) and merge that to `main`.
2. Create a **GitHub Release** with the tag `v<version>` (e.g. `v0.2.1`) and publish it.
3. The `release`-triggered `publish` job checks the tag matches `pyproject.toml`, then
   builds and uploads the clean version (`0.2.1`) to the `hmcts-lib` feed.

Every merge to `main` in between publishes a draft (`…​.dev<n>+<sha>`), so the feed
always has an installable build of the latest `main`.

## Consuming the package

The feed is private — see the [Install](../README.md#install) section of
the README for the `--index-url` and authentication options.

## Required infrastructure

- **Org secrets** `AZURE_DEVOPS_ARTIFACT_USERNAME` and `AZURE_DEVOPS_ARTIFACT_TOKEN`
  must be available to this repository (they authenticate the `twine` upload; the
  same credentials the Java Gradle publish uses).
- **CodeQL** only runs when the repository is public
  (`if: github.event.repository.visibility == 'public'`) — GitHub Advanced Security
  is disabled at org level for private/internal repos, so it is skipped until then
  and auto-enables on going public.
