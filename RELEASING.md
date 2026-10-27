# Releasing

Publishing is tag-driven (`.github/workflows/release.yml`) and uses **trusted publishing**, so no PyPI or npm
token lives in GitHub.

## One-time setup (owner)
- **PyPI:** for each of `promptli` and `promptli-gateway`, add a trusted publisher: owner `promptli-dev`, repo
  `promptli-comms`, workflow `release.yml`, environment `pypi` for `promptli` and `pypi-gateway` for `promptli-gateway` (PyPI allows only one pending publisher per repo/workflow/environment). For a first release, use a *pending* publisher.
- **npm:** the package is `@promptli-npm/sdk` (the `@promptli` org is taken and npm blocks the plain name `promptli`
  as too similar to `prompt`/`prompts`). Publish 0.1.0 once by hand (`npm publish --access public` in `sdk/typescript`),
  then on that package add a trusted publisher for the same repo, with
  workflow `release.yml` and environment `npm`.
- **GitHub:** create the environments `pypi`, `pypi-gateway` and `npm`, and add required reviewers if you want a manual gate.

## Python (`promptli`, `promptli-gateway`)
1. Bump `version` in `sdk/python/pyproject.toml` and `gateway/pyproject.toml`. They release together, and the
   gateway pins `promptli>=X.Y,<X.(Y+1)`.
2. Merge the change to main.
3. Tag and push: `git tag py-v0.1.0 && git push origin py-v0.1.0`.

The workflow:
- runs the tests and checks that both versions match the tag;
- builds, runs `twine check`, and installs the wheels into a clean venv;
- publishes.

## TypeScript (`@promptli-npm/sdk` on npm)
1. Bump `version` in `sdk/typescript/package.json`.
2. Merge the change to main.
3. Tag and push: `git tag ts-v0.1.0 && git push origin ts-v0.1.0`.

The package is published with npm provenance.

## After the first PyPI release
Switch `claude-code-plugin` from git pins to the published versions. Update `pyproject.toml` `[tool.uv.sources]`
and the `install.sh` pip specs, then resync the server's `public/install/claude.sh` (`scripts/sync_installer.sh`).
