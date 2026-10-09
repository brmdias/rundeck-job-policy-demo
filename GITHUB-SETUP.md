# GitHub Setup Guide - End-to-End Example

Goal: configure a GitHub pull-request gate and connect Rundeck projects to it:
**Rundeck development -> Git branch -> Pull Request (gate) -> main -> Rundeck production import**.

Mapping to GitLab: Merge Request = Pull Request, "Pipelines must succeed" = required status check,
code-owner approval = "Require review from Code Owners". The validator and policy are identical.

---

## Part 1 - Create the repository

1. Create a new repository on GitHub, e.g. `rundeck-job-policy-demo`.
   - Choose repository visibility according to your organization's policy and confirm that the
     required ruleset features are available for your GitHub plan. The included jobs are examples;
     review repository contents before making a repository public.
   - Do not add a README/license (keep it empty).
2. Copy the example into a new folder and push it (exclude the non-compliant sample jobs from
   `main`; you will use them later in a Pull Request):

```bash
mkdir ~/rundeck-job-policy-demo && cd ~/rundeck-job-policy-demo
cp -R <path-to-demo>/. .
mkdir -p ../bad-samples && mv jobs/bad-* ../bad-samples/    # keep them aside, outside the repo
rm -rf .gitlab-ci.yml CODEOWNERS                            # GitLab-only files (optional)
git init -b main
git add -A && git commit -m "Initial: job policy gate"
git remote add origin git@github.com:<you>/rundeck-job-policy-demo.git
git push -u origin main
```

3. Open **Settings > Actions > General** and confirm Actions are allowed. Under
   **Workflow permissions**, "Read repository contents" is enough.
4. Open the **Actions** tab. You should see two workflows: *Job policy gate* (runs on pull
   requests) and *Job policy audit* (scheduled / manual).

> Edit `.github/CODEOWNERS` and replace `@your-org/platform-team` with your team (organization) or
> your username, then commit to `main`.

## Part 2 - Make the gate binding (branch ruleset)

The gate only blocks merges once it is a **required status check**.

1. First make the check name known to GitHub: create any trivial PR (e.g. edit a comment in
   `policy/job-policy.yml` on a branch) so the workflow runs once. A check can only be
   selected after it has run at least once.
2. **Settings > Rules > Rulesets > New ruleset > New branch ruleset**
   - Ruleset name: `protect-main`
   - Enforcement status: **Active**
   - Target branches: **Add target > Include default branch**
   - Enable these rules:
     - **Restrict deletions**
     - **Block force pushes**
     - **Require a pull request before merging**
       - Required approvals: `1` (see Part 2 note for a single-account demo)
       - **Require review from Code Owners** (this protects `policy/`, `scripts/`, `.github/`)
     - **Require status checks to pass**
       - **Add checks** -> select `validate-jobs`
       - Tick **Require branches to be up to date before merging**
   - Bypass list: keep it **empty** (otherwise admins skip the gate).
3. Click **Create**.

**Single-account evaluation note:** GitHub does not let an author approve their own PR, so with
"Require review from Code Owners" a solo user cannot merge. Options:
- (A) Use an organization with a second account or team as code owner.
- (B) For an evaluation without a second reviewer, set approvals to `0` and leave "Require review
  from Code Owners" off. The required status check still validates job definitions.

> Do not add a `paths:` filter to the workflow trigger. If the workflow does not run for a PR the
> required check never reports and the PR is stuck waiting. The validator already ignores files
> outside `jobs/`.

## Part 3 - Run the demo (Git only)

### 3.1 Blocked: a dangerous job

```bash
git checkout -b feature/bulk-patch
cp ../bad-samples/bad-threads-100.yaml jobs/
git add jobs/bad-threads-100.yaml && git commit -m "Add bulk patch job" && git push -u origin feature/bulk-patch
```

1. Open a Pull Request into `main`.
2. Watch **Job policy gate / validate-jobs** fail:
   - an inline red annotation on `jobs/bad-threads-100.yaml` in **Files changed**,
   - a readable summary on the workflow run page,
   - the **Merge** button blocked.
   Message: `threadcount 100 exceeds the maximum allowed (20)`.

### 3.2 Fixed: compliant job passes

```bash
sed -i.bak 's/threadcount: 100/threadcount: 20/' jobs/bad-threads-100.yaml && rm jobs/*.bak
git commit -am "Reduce threadcount to cap" && git push
```

The check turns green; merge is allowed (after code-owner approval if enabled).

### 3.3 Show the bypass attempts are caught

Add `../bad-samples/bad-dynamic-unbounded-option.yaml` and `bad-jobref-override.yaml` in one PR:
both are rejected (unbounded `${option.threads}` and a `jobref` step overriding the thread count).

### 3.4 Governance

On a PR that edits `policy/job-policy.yml` (e.g. raising `max_threadcount` to 200), the code-owner
review requirement applies to policy changes. For the exception flow, compare
`ok-approved-exception.yaml` (valid exception) and the expired exception.

### 3.5 Legacy jobs: the audit

1. To evaluate the audit without weakening protections, use a separate test repository or an
   existing non-compliant job already present on `main`.
2. **Actions > Job policy audit > Run workflow**.
3. Open the run: the summary lists non-compliant jobs, and the **job-audit-report** artifact has
   the CSV/JSON for remediation tracking.

## Part 4 - Connect Rundeck (end-to-end)

Use a non-production Rundeck environment with two projects:
`demo-dev` (where users build jobs) and `demo-prod` (import-only).

### 4.1 Credentials

- **Public repo, read-only import:** HTTPS URL, no credentials needed.
- **Export (write) or private repo:** create an SSH key pair and add the public key to the repo as
  a **deploy key**:

```bash
ssh-keygen -t ed25519 -f rundeck-demo-key -C ""
```

  - GitHub: **Settings > Deploy keys > Add deploy key** (tick **Allow write access** only for the
    Dev export key; use a separate read-only key for Prod).
  - Rundeck: **Project Settings > Key Storage > Add private key** (e.g. `keys/project/demo-dev/github-write`).

### 4.2 Dev project: export to a working branch

1. Create the branch used by Dev exports and push it:
   `git branch dev-export main && git push origin dev-export`
2. In `demo-dev`: **Project Settings > Setup SCM > Git Export**:
   - Git URL: `ssh://git@github.com/<you>/rundeck-job-policy-demo.git`
   - Branch: `dev-export`
   - Export UUID behavior: `preserve`; Format: `yaml`
   - File Path Template: `jobs/${job.group}${job.name}.${config.format}`
   - SSH Key Storage Path: the write key.
3. Create a job in `demo-dev` with **Dispatch to Nodes > Thread count = 100**, then in the job
   list use **Commit** (export). It lands in `dev-export` under `jobs/...`.

### 4.3 Promote through the gate

1. Open a **Pull Request `dev-export` -> `main`**. The gate fails because of the 100 threads.
2. Back in Rundeck, set Thread count to 20, commit again; the PR check turns green, then merge.

### 4.4 Prod project: import-only

1. In `demo-prod`: **Project Settings > Setup SCM > Git Import**:
   - Git URL: same repo; Branch: `main`
   - Import UUID behavior: `preserve`
   - File Path Template: `jobs/${job.group}${job.name}.${config.format}`
   - Match regex: `jobs/.*\.(yaml|yml)`; read-only key.
2. In the Jobs page choose **Import** and select the merged job. Only compliant jobs ever reach Prod.
3. (After sandbox validation) apply `acl/prod-scm-import-only.aclpolicy` (edit group and project
   names), then verify that users cannot create or edit jobs in `demo-prod` through the UI/API,
   while the `scm-importers` group can import. Validate this sample ACL in the target environment
   before deployment.

## Validation checklist

- [ ] `./tests/run_tests.sh` passes locally (10 passed).
- [ ] Ruleset `protect-main` is Active with `validate-jobs` required and an empty bypass list.
- [ ] `dev-export` branch exists; deploy keys work (`ssh -T git@github.com`).
- [ ] Production ACL tested in a non-production environment.
- [ ] Explain the policy's scope and any complementary platform-level controls.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `validate-jobs` not selectable in the ruleset | Workflow has not run yet; open a PR first, then add the check |
| PR waits forever on "Expected - waiting for status" | Workflow skipped (e.g. a `paths:` filter was added) or the check name does not match the job name `validate-jobs` |
| `git diff` error / "unknown revision origin/main" | Checkout not full-depth; keep `fetch-depth: 0` |
| Cannot merge your own PR | Author cannot self-approve; see Part 2 single-account note |
| Rundeck: "SCM disabled" message | User lacks access to the Key Storage key used by the plugin |
| Rundeck SSH URL rejected | Use `ssh://git@github.com/<owner>/<repo>.git`, not `git@github.com:<owner>/<repo>.git` |
| Audit workflow shows nothing | No jobs under `jobs/` on `main` match `job_paths` in `policy/job-policy.yml` |
