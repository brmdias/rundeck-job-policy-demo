# GitLab Job Definition Validation Demo

A merge-request gate that blocks dangerous Rundeck / Runbook Automation job definitions
(for example `threadcount: 100`) **before** they are promoted through the SCM plugin.

**Status:** interim control. The permanent fix is a product-level, system-wide cap on per-job
node threads (enhancement request to be raised via the CSM / RUN project).

## Why

One job with 100 node-dispatch threads can exhaust the shared JVM heap and take down every team
on the cluster. Project-level settings can be changed by users, so the control must sit outside
their reach. All job definitions already flow through GitLab (SCM export -> MR -> SCM import), so
GitLab is the natural enforcement point.

```
Developer ──► Dev cluster ──SCM export──► GitLab MR ──► [validate-jobs gate] ──► merge
                                                              │ fail: MR blocked
                                                              ▼
                                             protected branch ──SCM import──► PreProd ──► Prod
                                                                               (import-only ACL)
```

## What it checks (policy/job-policy.yml)

| Check | Result |
|-------|--------|
| `nodefilters.dispatch.threadcount` > `max_threadcount` (default **20**) | Fail |
| Same key inside **job-reference steps**, error handlers, nested structures | Fail |
| Dynamic `${option.x}` unless the option is `enforced` with allowed values all within 1..cap | Fail |
| Non-integer / < 1 values | Fail |
| Unparsable file (fail closed - cannot prove it is safe) | Fail |
| Approved **exception** (by job uuid or path) with its own cap | Pass up to that cap |
| **Expired** exception | Fail |

Why dynamic values matter: Rundeck resolves `${option.NAME}` at run time
(`ExecutionService.groovy`), so a validator that only checks integers is trivially bypassed.

## GitHub or GitLab?

The validator, policy and Rundeck configuration are Git-host agnostic. Only the CI wrapper differs:
GitLab uses `.gitlab-ci.yml` + `CODEOWNERS`; GitHub uses `.github/workflows/*` + `.github/CODEOWNERS`.
For a step-by-step GitHub build (repo, ruleset, demo flow, Rundeck SCM wiring) see
[GITHUB-SETUP.md](./GITHUB-SETUP.md).

## Contents

```
.github/workflows/validate-jobs.yml  GitHub PR gate (inline annotations + summary)
.github/workflows/audit-all-jobs.yml GitHub weekly/manual audit (CSV/JSON artifact)
.github/CODEOWNERS                   GitHub code owners for policy/scripts/workflows
GITHUB-SETUP.md                      Step-by-step GitHub + Rundeck end-to-end guide
.gitlab-ci.yml                     MR gate + scheduled audit
CODEOWNERS                         Only the platform team can change policy/validator/pipeline
policy/job-policy.yml              Cap, dynamic-value mode, scope, exceptions (owner/reason/expiry)
scripts/validate_jobs.py           Validator (YAML/JSON/XML; text, json, csv, junit output)
acl/prod-scm-import-only.aclpolicy Prod ACL: jobs change only via SCM import
jobs/ok-*.yaml, jobs/bad-*         Sample jobs: ok-* must pass, bad-* must fail
tests/run_tests.sh                 Self-test over the sample jobs
```

## Run it locally (2 minutes)

```bash
pip install pyyaml
cd demo/gitlab-job-validation-demo

./tests/run_tests.sh                       # expect: 10 passed, 0 failed

python3 scripts/validate_jobs.py --all     # full scan, human-readable
python3 scripts/validate_jobs.py --files jobs/bad-threads-100.yaml
python3 scripts/validate_jobs.py --all --format csv --output reports/audit.csv   # remediation list
```

Expected for `bad-threads-100.yaml`:

```
FAIL  jobs/bad-threads-100.yaml  [demo/validation/bad-threads-100]  job.nodefilters.dispatch.threadcount: threadcount 100 exceeds the maximum allowed (20)
Job policy check FAILED: 1 job(s) in 1 file(s), 1 violation(s), cap=20 threads.
```

## Demo script for the customer meeting

1. **Show the incident pattern:** open `jobs/bad-threads-100.yaml` (threadcount 100).
2. **Open a MR** that adds it to the job repo -> pipeline `validate-jobs` fails, MR shows the
   failure under *Tests* (JUnit) and merge is blocked.
3. **Fix to 20** (`ok-boundary-20-threads.yaml`) -> pipeline passes, merge allowed.
4. **Try the bypasses:** `bad-dynamic-unbounded-option.yaml` and `bad-jobref-override.yaml` are
   both caught.
5. **Exceptions:** show `ok-approved-exception.yaml` vs `bad-expired-exception.yaml`; exceptions
   need an owner, reason and expiry, and only `@platform-team` can approve (CODEOWNERS).
6. **Legacy jobs:** run the scheduled `audit-all-jobs` (or `--all --format csv`) to produce the
   hit list of already non-compliant jobs, grouped by owner, for the remediation campaign.

## Adopting at the customer

1. Copy `scripts/`, `policy/`, `.gitlab-ci.yml`, `CODEOWNERS` into the job-definition repo.
2. Match `job_paths` to the SCM export **File Path Template** (e.g. `projects/${project}/...`).
3. Set the cap to the agreed value (**20** here; sized for 2000-node jobs at 20 threads).
4. In GitLab: protect target branches, enable *Pipelines must succeed* and *Code owner approval*.
5. Create a weekly pipeline schedule (CI/CD > Schedules) for `audit-all-jobs`.
6. Run the audit first in report-only mode, publish the hit list, set a remediation deadline,
   then add expiring exceptions only where justified.

## Prod hardening (closing the bypass)

The Git gate only works if Prod cannot be edited directly. Grant everyday users no `create` /
`update` / `delete` on jobs and let only the SCM import path change them
(`acl/prod-scm-import-only.aclpolicy`, using `scm_create`, `scm_update`, `scm_delete`,
`scm_import`). **Sandbox-validate before rollout** - this demo's ACL has not yet been tested
against a live cluster. Also ensure that broad admin groups do not retain edit rights unless
they are trusted to bypass the gate.

## Known limits

- Does not fix existing jobs - it flags them (audit) so owners can be persuaded to update.
- Only covers the **thread count**. Add further rules (max nodes in filter, timeouts, retry
  counts, local execution) in `check_*` functions as policy matures.
- Does not cover jobs changed through the API/UI on clusters where users can still edit
  (see Prod hardening) or the global `quartz.threadPool.threadCount`.
- A repo admin who can disable the pipeline can bypass it; protect settings and audit changes.
