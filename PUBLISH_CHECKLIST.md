# Publish checklist — arc-agent-pay public SDK

This directory (`public-repo-staging/`) is the **SDK-only** tree destined for the
public repo + PyPI. It contains only `arc_agent_pay/` package code, SDK tests,
docs, and CI — no `demo/`, `frontend/`, or ops config. Verified: `ruff` clean,
187 tests pass, wheel builds with package code only.

Do the steps below **in order**. Steps 1–3 are hard gates — do not make the repo
public until they're done.

## 1. Rotate the leaked server key set (BLOCKING)

The `cf0f0f…` / `0x80Aa…` server key passed through a session in cleartext.
Deferral was justified only while the repo was private — going public ends that.
Rotate all four together (they're the same wallet), in `.env` **and** on the host:

- `SERVER_PRIVATE_KEY`
- `SELLER_ADDRESS`
- `REPUTATION_FEEDBACK_PRIVATE_KEY`
- `VALIDATION_VALIDATOR_PRIVATE_KEY`

Also rotate the local `OPENAI_API_KEY` and any ArcAPIs / testnet keys that touched
a session. The agent wallet (`0xEa77…`) did **not** leak, but rotating it too is
cheap hygiene if you want a clean slate. Fund the new server wallet with Arc
Testnet USDC afterward.

## 2. Scan the tree you actually publish for secrets (BLOCKING)

CI's gitleaks scans commit *diffs*, not the pre-existing tree. Scan this staging
directory before the first commit:

```bash
cd public-repo-staging
gitleaks detect --source . --no-git    # scans working tree, not git history
```

Because this becomes a **fresh repo with a clean initial commit** (not a filter of
the old history), there's no historical leak to excavate — but scan the tree once
to be sure nothing sensitive rode along in a doc or test fixture.

## 3. Confirm `.env` is absent and ignored (BLOCKING)

There is no `.env` in this staging tree (only `.env.example`). `.gitignore`
already ignores `.env`, `.venv`, `dist/`, `build/`, `.claude/`. Double-check
before committing:

```bash
ls -a public-repo-staging | grep -E '^\.env$'    # should print nothing
```

## 4. Create the public repo + first commit

```bash
cd public-repo-staging
git init -b main
git add .
git commit -m "Initial public release: arc-agent-pay SDK"
gh repo create arc-agent-pay --public --source . --remote origin --push
```

(If the private repo already owns the name `hamedkharazmi/arc-agent-pay`, either
rename the private one — e.g. `arc-agent-pay-platform` — or pick a new public
name and update `pyproject.toml [project.urls]` + README badges to match.)

## 5. Set up PyPI Trusted Publishing (recommended over API tokens)

The staged `release.yml` uses OIDC Trusted Publishing (no long-lived token in
GitHub secrets) and attaches provenance attestations. To enable:

1. Register the project on PyPI (or TestPyPI first).
2. PyPI → project → **Publishing** → add a GitHub Actions trusted publisher:
   - Owner: `hamedkharazmi` (or your org)
   - Repo: `arc-agent-pay`
   - Workflow: `release.yml`
   - Environment: `pypi` (matches the workflow's `environment:` — create it in
     repo Settings → Environments)
3. Cut a release: tag `v0.1.0` and publish a GitHub Release → `release.yml` builds,
   attests, and uploads to PyPI.

```bash
git tag v0.1.0 && git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --generate-notes
```

Test the install once it's live:

```bash
pip install "arc-agent-pay[all]"
```

## 6. Point the private platform repo at the published SDK

In the private repo, drop the vendored SDK source and depend on the published
package instead (`arc-agent-pay[demo]` or a pinned version). The private repo
keeps `demo/`, `frontend/`, Railway/Cloudflare config, and the server wallet.

## 7. Undo the private-beta scaffolding (in the private repo's frontend)

Now that install is real:

- `docs.tsx`: replace the "request access / private beta" section with the real
  Quickstart (`pip install "arc-agent-pay[all]"`) + SDK reference.
- Restore the GitHub repo link in the navbar (it 404'd while private).
- Keep the in-app Feedback path — still the right private channel for security
  reports.

---

### What changed from the private repo's copies

- `pyproject.toml`: dropped `server` / `scale` / `demo` extras and the `demo`
  dev-groups (all server-side); added PyPI classifiers, keywords, and
  `[tool.hatch.build.targets.wheel]`; URLs point at `agentpay.bond` + the public
  repo.
- `ci.yml`: installs SDK extras (`--extra agent/rag/onchain/mcp/observability`)
  instead of demo groups; triggers on `main`; keeps the pip-audit + gitleaks job.
- `release.yml`: new — OIDC Trusted Publishing + provenance on tagged releases.
- `README.md` / `SECURITY.md`: rewritten for the SDK audience; the hosted
  playground is described as a separate service, security reports routed to the
  feedback channel.
- `registry/__init__.py`: removed the stale `demo/mock_services.py` code comment
  (the only private-side reference in the SDK source).
- Excluded entirely: `demo/`, `frontend/`, `docs/PRODUCTION_READINESS.md`,
  `docs/website-roadmap.md`, `Dockerfile`, `Procfile`, `railway.toml`,
  `main.py`, `entrypoint.sh`, `scripts/` (ops/smoke scripts), and the four
  server-side tests (`test_web_api*`, `test_auth`, `test_providers`,
  `test_reliability`, `test_observability`, `test_state_backends`,
  `test_mock_services_fail_closed`, `test_reputation_feedback`,
  `test_web_api_config`, `test_web_api_security`).
