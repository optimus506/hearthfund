# Releasing

A release is a tag `vX.Y.Z` on `main`. Pushing the tag runs `.github/workflows/release.yml`: every CI check
(lint, types, tests, the privacy scan, the image build and container tests for both platforms, the
vulnerability scan), then the image is built for `linux/amd64` and `linux/arm64` and pushed to
`ghcr.io/optimus506/hearthfund` as `X.Y.Z`, `X.Y` and `latest`, with an SBOM and build provenance
attached, and a GitHub release is made with the version's CHANGELOG.md section as its notes.

## Steps

1. While working, add each user-visible change under `## Unreleased` in CHANGELOG.md (Added / Changed / Fixed).
2. On an up-to-date `main` with a clean working tree:

   ```sh
   python3 scripts/release.py prepare 0.2.0
   git push origin HEAD v0.2.0
   ```

   `prepare` sets `__version__` in `src/hbudget/__init__.py` (the only place the version is written;
   `pyproject.toml` reads it), turns `## Unreleased` into `## 0.2.0 - <date>` with a new empty `Unreleased`
   above it, commits "Release 0.2.0" and makes the annotated tag. Nothing is pushed until you push.
3. Watch the Release workflow under Actions. If a check fails, nothing is published: fix it on `main`, delete
   the tag (`git push origin :v0.2.0 && git tag -d v0.2.0`) and prepare it again.
4. After the release, set `PREVIOUS_REF` in `.github/workflows/ci.yml` and the Makefile to the new tag, so the
   upgrade test starts from the latest release.

Versions follow semantic versioning: a new patch version (1.0.1) for fixes only; a new minor version (1.1.0)
for new features or a database migration; a new major version only for a change that breaks something
[STABILITY.md](STABILITY.md) promises to keep. Before tagging, read the changes since the last release against
that page.

## Who can pull the image

The image is public: anyone can `docker pull` it without signing in. (A package on GitHub's registry starts
with the visibility of its repository; a public package stays public.)
