# GitLab Job Definition Validation

A merge-request gate that blocks dangerous Rundeck / Runbook Automation job definitions
(for example `threadcount: 100`) **before** they are promoted through the SCM plugin.

This example provides a repository-level control. Where available, complement it with a
platform-level per-job thread limit.

## Why

A job configured with 100 node-dispatch threads can exhaust shared JVM resources and disrupt
concurrent workloads. Because project-level settings may be changed by project users, applying
the control in the source repository helps enforce the policy consistently. When job definitions
flow through GitLab (SCM export -> MR -> SCM import), GitLab provides an enforcement point.

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

Dynamic values matter because Rundeck resolves `${option.NAME}` at run time; validating only
literal integers would not enforce the configured limit.

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

## Run it locally

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

## Example validation walkthrough

1. Open `jobs/bad-threads-100.yaml` (threadcount 100) as an example of a non-compliant job.
2. **Open a MR** that adds it to the job repo -> pipeline `validate-jobs` fails, MR shows the
   failure under *Tests* (JUnit) and merge is blocked.
3. **Fix to 20** (`ok-boundary-20-threads.yaml`) -> pipeline passes, merge allowed.
4. **Validate related cases:** `bad-dynamic-unbounded-option.yaml` and `bad-jobref-override.yaml`
   are both rejected.
5. **Exceptions:** compare `ok-approved-exception.yaml` and `bad-expired-exception.yaml`; exceptions
   need an owner, reason and expiry, and approval is restricted to designated code owners (CODEOWNERS).
6. **Legacy jobs:** run the scheduled `audit-all-jobs` (or `--all --format csv`) to produce the
   report of existing non-compliant jobs, grouped by owner, to support remediation.

## Adoption guidance

1. Copy `scripts/`, `policy/`, `.gitlab-ci.yml`, `CODEOWNERS` into the job-definition repo.
2. Match `job_paths` to the SCM export **File Path Template** (e.g. `projects/${project}/...`).
3. Set the cap to a value established by capacity testing and operational requirements (**20** in this example).
4. In GitLab: protect target branches, enable *Pipelines must succeed* and *Code owner approval*.
5. Create a weekly pipeline schedule (CI/CD > Schedules) for `audit-all-jobs`.
6. Run the audit first in report-only mode, publish the hit list, set a remediation deadline,
   then add expiring exceptions only where justified.

## Production access controls

The Git gate is most effective when Production cannot be edited directly. Grant users no `create` /
`update` / `delete` on jobs and let only the SCM import path change them
(`acl/prod-scm-import-only.aclpolicy`, using `scm_create`, `scm_update`, `scm_delete`,
`scm_import`). Validate the sample ACL in a non-production environment and adapt it to the target
environment before rollout. Limit administrative permissions and monitor changes to repository
and pipeline settings.

## Known limits

- Does not fix existing jobs - it flags them (audit) so owners can remediate them.
- Only covers the **thread count**. Add further rules (max nodes in filter, timeouts, retry
  counts, local execution) in `check_*` functions as policy matures.
- Does not cover jobs changed through the API/UI on clusters where users can still edit
  (see Prod hardening) or the global `quartz.threadPool.threadCount`.
- Repository administrators may be able to disable or alter the pipeline; protect settings and
  audit changes.
