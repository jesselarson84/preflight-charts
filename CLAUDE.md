# preflight-charts

Instructions for AI coding assistants working in this repo (Claude Code, Grok Bot and others).

## CI artifacts

- Finished builds (APK, MSI) publish to the CI-managed `preview-pr-N` pre-release (or a release), never as GitHub Actions artifacts. Never add `upload-artifact` for build outputs.
- Only failure logs and test reports may upload as artifacts, with `if: failure()`, `continue-on-error: true` and `retention-days: 1`.
- Each repo's artifact and log retention stays at 3 days.
- Why: GitHub Free gives the whole jesselarson84 account 500 MB of artifact storage, counted cumulatively over the billing month. Deleting artifacts doesn't free it, and once it's used up every upload in every repo fails until the billing cycle resets.
- Current exceptions, each with `retention-days: 1`: Preflight `chart.yml` `faa-chart` (no other delivery path) and Slate `windows-msi.yml` `Slate-msi` (until Slate gets a preview pre-release). Don't add more.
