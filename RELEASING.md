# Releasing rift

Releases are published to PyPI by GitHub Actions ([publish.yml](.github/workflows/publish.yml)) using
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): there are no API tokens to create or rotate.

## One-time setup

1. **Push the repo to GitHub** (e.g. `github.com/<you>/rift`).
2. **Register the publisher on PyPI.** Go to <https://pypi.org/manage/account/publishing/> and, under
   *Add a new pending publisher*, enter:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `riftdiff` |
   | Owner | your GitHub user or org |
   | Repository name | `rift` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. **Do the same on TestPyPI** at <https://test.pypi.org/manage/account/publishing/>, with environment
   name `testpypi`. TestPyPI is a separate site with a separate account.
4. **Create the environments in GitHub.** Go to *Settings → Environments* and create `pypi` and `testpypi`.
   On `pypi`, add yourself under *Required reviewers* so every real release waits for your click.

## Dry run on TestPyPI

*Actions → Publish to PyPI → Run workflow.* This runs the tests, builds the package and uploads it to
TestPyPI. Check the page at <https://test.pypi.org/p/riftdiff>, then try installing it:

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ riftdiff
```

## Cutting a release

1. Update `__version__` in [src/riftdiff/\_\_init\_\_.py](src/riftdiff/__init__.py) (e.g. `0.2.0`) and commit.
2. Tag the commit and push the tag:

   ```bash
   git tag v0.2.0
   git push origin main v0.2.0
   ```

3. The workflow then does the following:
   - runs the test suite on Linux, macOS and Windows;
   - checks that the tag matches `__version__`;
   - builds the sdist and wheel;
   - publishes them to PyPI after your approval;
   - creates a GitHub release with the files attached.

PyPI never accepts the same version twice, so a broken release is fixed with a new version number.

## Building locally

```bash
pip install build twine
python -m build
twine check --strict dist/*
```
