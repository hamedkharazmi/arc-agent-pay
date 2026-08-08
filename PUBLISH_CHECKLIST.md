# Release checklist — arc-agent-pay

Releases publish to PyPI from version tags through GitHub OIDC Trusted
Publishing. No PyPI API token is stored in the repository.

## 1. Prepare the release

- Confirm `main` is clean, pushed, and green in CI.
- Choose the next semantic version.
- Update the version in both `pyproject.toml` and
  `arc_agent_pay/__init__.py`.
- Add the release notes to `CHANGELOG.md`.
- Run `uv lock` and verify that the project package version changed in
  `uv.lock`.

## 2. Verify locally

```bash
uv lock --check
uv run ruff check .
uv run python -m pytest -q
uv build
uvx twine check dist/*
```

Inspect both distributions and install the wheel into fresh Python 3.11 and
3.12 environments. Confirm `arc_agent_pay.__version__` and exercise the public
imports introduced by the release.

## 3. Commit and wait for CI

```bash
git add pyproject.toml uv.lock arc_agent_pay/__init__.py CHANGELOG.md
git commit -m "release: prepare vX.Y.Z"
git push origin main
```

Do not tag until the commit's lint, test, audit, and secret-scan jobs pass.

## 4. Tag and publish

The release workflow rejects a tag that does not exactly match the package
version.

```bash
git tag -a vX.Y.Z -m "arc-agent-pay vX.Y.Z"
git push origin vX.Y.Z
```

Monitor `.github/workflows/release.yml`. It builds the sdist and wheel, checks
their metadata, publishes through the configured `pypi` environment, and
uploads PEP 740 provenance attestations.

## 5. Verify the public release

Wait until the new version appears in the PyPI JSON API, then install the exact
version from PyPI in a fresh environment—never from the repository checkout or
wheel cache.

```bash
uv venv --python 3.11 /tmp/arc-agent-pay-release-check
VIRTUAL_ENV=/tmp/arc-agent-pay-release-check \
  uv pip install --refresh --no-cache "arc-agent-pay==X.Y.Z"
/tmp/arc-agent-pay-release-check/bin/python -c \
  "import arc_agent_pay; print(arc_agent_pay.__version__)"
```

Finally, verify the SDK-to-app automation updated the app lock only when the
release commit changed installable SDK code or package metadata.
