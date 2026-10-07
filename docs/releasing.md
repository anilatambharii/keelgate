# Releasing Keelgate

Releases are automated end to end except for two deliberate human steps: merging the release pull
request, and approving the PyPI upload.

```text
Conventional Commits land on main
        |
        v
release-please keeps a "chore(main): release X.Y.Z" PR up to date
        |   (changes pyproject.toml + uv.lock, writes CHANGELOG.md)
        v
a human merges that PR  ---->  tag vX.Y.Z + GitHub release
        |
        v
publish.yml:  build -> check tag == version -> twine check -> smoke-test the wheel
        |
        v
a human approves the `pypi` environment  ---->  upload to PyPI (trusted publishing, no token)
        |
        v
verify:  pip install keelgate==X.Y.Z in a clean venv and run `keelgate quickstart`
```

## Choosing the version

You do not. release-please reads the commit types since the last tag:

| Commits since the last release | While 0.x | From 1.0 |
|---|---|---|
| only `fix:` / `docs:` / `perf:` | patch | patch |
| any `feat:` | minor | minor |
| any `feat!:` / `fix!:` / `BREAKING CHANGE:` | minor | **major** |

If a release PR proposes a version that looks wrong, the commit messages are wrong: fix them
(amend in the release PR with `Release-As: X.Y.Z` in a commit body if you must override).

## One-time setup

1. **PyPI trusted publisher.** On pypi.org, for the project `keelgate`, add a trusted publisher:
   owner `anilatambharii`, repository `keelgate`, workflow `publish.yml`, environment `pypi`.
2. **GitHub environments.** Create `pypi` and give it **required reviewers**, so a human approves
   each real upload. Create `testpypi` for dry runs.
3. **Optional, for a dry run:** on test.pypi.org add the same trusted publisher with environment
   `testpypi`. Then run the *Publish* workflow by hand (Actions, Publish, Run workflow): it uploads
   the current version to TestPyPI only.
4. **Release token (recommended).** GitHub does not start other workflows for a tag pushed with the
   default `GITHUB_TOKEN`. Create a fine-grained personal access token with **contents** and
   **pull requests** write on this repository and store it as the secret `RELEASE_PLEASE_TOKEN`; <!-- pragma: allowlist secret -->
   then the tag release-please creates triggers `publish.yml` by itself. Without it the release
   still works, but after merging the release PR you re-push the tag once by hand:

   ```bash
   git fetch --tags && git push origin :refs/tags/vX.Y.Z && git push origin vX.Y.Z
   ```

5. **GitHub Pages.** Settings, Pages, Source: *GitHub Actions*. The *Docs* workflow publishes the
   site on every push to `main`.

## Releasing

1. Make sure `main` is green.
2. Review and merge the open **release PR**. It shows the changelog that will be published. If its
   `uv lock --check` job fails, run `uv lock` on the release branch and push.
3. Approve the `pypi` environment when the *Publish* workflow asks.
4. The workflow's last job installs the new version from PyPI in a clean environment and runs
   `keelgate quickstart`. If it fails, the version is already public: fix forward with a patch
   release (a PyPI version number can never be reused).

## If something goes wrong

- **Yank, do not delete.** `pip install` skips a yanked version unless it is pinned exactly. Yank on
  pypi.org, then release a fix.
- **A bad tag that has not published yet:** delete the GitHub release and the tag, fix, and let
  release-please open the PR again.
- **Version already on PyPI** (the workflow cannot overwrite it): bump and release again.

Dependent projects pin a minor range (Tycheon declares `keelgate>=0.2,<0.3`), so a breaking 0.x
minor release does not reach them until they opt in.
