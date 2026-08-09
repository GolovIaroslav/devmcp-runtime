# Release procedure

1. Verify the release branch, version source, CHANGELOG heading, required docs,
   wheel/sdist, clean wheel installation, and all Linux security gates.
2. Run a secret scanner over the current tree and the complete Git history.
3. Run `devmcp status`, `tunnel-client doctor --explain` when configured, and
   the deterministic Balanced dogfood loop.
4. Review the diff and generated artifacts. Do not publish the repository,
   rename the GitHub repository, merge main, publish the ChatGPT app, or create
   a GitHub Release automatically from this preparation task.

The beta tag is `v0.1.0-beta.1`. If a mandatory gate fails or supported
functionality is materially broken, report alpha readiness instead.
