# Releasing broker365 (private)

broker365 is distributed **privately** from the GitHub repo. It is intentionally not published
on PyPI, so don't run `twine upload`. A release is a version bump plus a git tag.

1. Bump `__version__` in `src/broker365/__init__.py`. The package version is read from there.
   Also bump `version:` in `src/broker365/hermes_skill/SKILL.md`, which is the skill copy that
   `broker365 hermes install` writes into Hermes.
2. Run the checks: `pytest`, then `ruff check .`.
3. Commit, tag and push:
   ```bash
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z && git push origin main --tags
   ```
4. On each machine, update: `pipx upgrade broker365`. To pin a version, use
   `pipx install --force git+https://github.com/yugienugraha/broker-365.git@vX.Y.Z`.

To hand an offline copy to someone you trust, `python -m build` produces a wheel in `dist/`.
They install it with `pipx install broker365-X.Y.Z-py3-none-any.whl`. Don't upload it anywhere
public.
