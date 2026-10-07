# Releasing Keelgate

Keelgate publishes to PyPI with **trusted publishing** (OIDC); no token is stored anywhere.

## One-time setup

1. On pypi.org, add a *pending* trusted publisher for the project `keelgate`: owner
   `anilatambharii`, repository `keelgate`, workflow `publish.yml`, environment `pypi`.
2. In the GitHub repository, create the environment `pypi` and require a reviewer, so a human
   approves each real upload.

## Releasing

1. Make sure `version` in `pyproject.toml` is the version to release and `main` is green.
2. Push a tag `vX.Y.Z` that matches it. The `Publish` workflow builds, checks that the tag matches
   the package version, runs `twine check --strict`, smoke-tests the wheel in a clean environment,
   and waits for the `pypi` environment approval before uploading.
3. Verify from a clean virtual environment: `pip install keelgate==X.Y.Z`.

Dependent projects (Tycheon declares `keelgate>=0.1,<0.2`) can only install once step 2 is done.
