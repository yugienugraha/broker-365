# Releasing broker365

1. Bump `__version__` in `src/broker365/__init__.py`. The package version is read from there.
   Also bump `version:` in `src/broker365/hermes_skill/SKILL.md`, which is the skill copy that
   `broker365 hermes install` writes into Hermes.
2. Run the checks: `pytest`, then `ruff check .`.
3. Commit, tag and push:
   ```bash
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z && git push origin main --tags
   ```
4. Build, verify and upload with your own PyPI API token:
   ```bash
   python -m build && python -m twine check dist/*
   python -m twine upload dist/*
   ```

Optional: replace step 4 with a tag-triggered GitHub Actions workflow that uses PyPI Trusted
Publishing (OIDC), so no API token is stored locally. This needs a trusted publisher set up for
this repository on pypi.org first.
