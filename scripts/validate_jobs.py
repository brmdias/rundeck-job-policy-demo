#!/usr/bin/env python3
"""
validate_jobs.py - Policy gate for Rundeck / Runbook Automation job definitions.

Fails (exit 1) when a job definition violates the guardrails in policy.yml,
most importantly the maximum node-dispatch thread count.

Modes
  --changed-since <git-ref>   Validate only job files changed since <ref> (MR pipelines)
  --all                       Validate every job file under job_paths (scheduled audit)
  --files f1 f2 ...           Validate explicit files (local testing)

Output
  --format text|json|csv|junit|github   (default: text; junit for GitLab, github for GitHub annotations)
  --output <file>                Write the report to a file instead of stdout

Exit codes: 0 = compliant, 1 = violations found, 2 = usage / policy error

Supported job formats: YAML (job-yaml-v12), JSON, XML (job-v20).
Requires: Python 3.8+, PyYAML.
"""
import argparse
import csv
import datetime as dt
import fnmatch
import io
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required: pip install pyyaml\n")
    sys.exit(2)

OPTION_REF = re.compile(r"^\$\{option\.([A-Za-z0-9_.\-]+)\}$")
ANY_REF = re.compile(r"\$\{[^}]+\}|@[A-Za-z0-9_.\-]+@")


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class Policy:
    def __init__(self, data):
        self.max_threadcount = int(data.get("max_threadcount", 20))
        self.dynamic_threadcount = data.get("dynamic_threadcount", "bounded_option")
        if self.dynamic_threadcount not in ("reject", "bounded_option"):
            raise ValueError("dynamic_threadcount must be 'reject' or 'bounded_option'")
        self.job_paths = data.get("job_paths", ["jobs/"])
        self.extensions = [e.lower() for e in data.get("extensions", [".yaml", ".yml", ".json", ".xml"])]
        self.exclude = data.get("exclude", [])
        self.today = dt.date.fromisoformat(data["today"]) if data.get("today") else dt.date.today()
        self.exceptions = data.get("exceptions", []) or []
        for ex in self.exceptions:
            if not (ex.get("uuid") or ex.get("path")):
                raise ValueError(f"exception needs 'uuid' or 'path': {ex}")
            for req in ("max_threadcount", "reason", "approved_by", "expires"):
                if req not in ex:
                    raise ValueError(f"exception missing '{req}': {ex}")

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh) or {})

    def in_scope(self, rel):
        rel = rel.replace("\\", "/")
        if Path(rel).suffix.lower() not in self.extensions:
            return False
        if not any(rel.startswith(p) for p in self.job_paths):
            return False
        return not any(fnmatch.fnmatch(rel, pat) for pat in self.exclude)

    def find_exception(self, rel, uuid):
        for ex in self.exceptions:
            if (ex.get("uuid") and uuid and ex["uuid"] == uuid) or (
                ex.get("path") and ex["path"].replace("\\", "/") == rel.replace("\\", "/")
            ):
                return ex
        return None


# --------------------------------------------------------------------------- #
# Parsing - normalise YAML/JSON/XML to a list of job dicts, and find threadcounts
# --------------------------------------------------------------------------- #
def _xml_to_obj(el):
    """Generic XML -> dict/list/str conversion, sufficient for threadcount/option lookup."""
    children = list(el)
    if not children:
        return (el.text or "").strip()
    out = {}
    for ch in children:
        out.setdefault(ch.tag, []).append(_xml_to_obj(ch))
    return {k: (v[0] if len(v) == 1 else v) for k, v in out.items()}


def load_jobs(path):
    """Return a list of job dicts from a file; raises ValueError if unparsable."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    elif suffix == ".xml":
        root = ET.fromstring(text)
        jobs = root.findall(".//job") if root.tag != "job" else [root]
        data = []
        for j in jobs:
            obj = _xml_to_obj(j)
            # keep the raw element for option lookups with attributes
            obj["__xml__"] = j
            data.append(obj)
    else:
        raise ValueError(f"unsupported extension {suffix}")
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    return [j for j in data if isinstance(j, dict)]


def walk_threadcounts(node, trail="job"):
    """Yield (location, raw_value) for every threadcount in a job structure.

    YAML/JSON: any mapping with a 'threadcount' key (nodefilters.dispatch.threadcount,
    also job-reference steps, error handlers, nested strategies).
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "__xml__":
                continue
            loc = f"{trail}.{k}"
            if k.lower() == "threadcount" and not isinstance(v, (dict, list)):
                yield loc, v
            else:
                yield from walk_threadcounts(v, loc)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk_threadcounts(item, f"{trail}[{i}]")


def xml_threadcounts(job_el):
    for tc in job_el.iter("threadcount"):
        yield "xml:threadcount", (tc.text or "").strip()


def job_options(job):
    """Return {option_name: option_dict} for YAML/JSON jobs and XML."""
    opts = {}
    raw = job.get("options")
    if isinstance(raw, list):  # YAML v12 / JSON
        for o in raw:
            if isinstance(o, dict) and o.get("name"):
                opts[o["name"]] = o
    el = job.get("__xml__")
    if el is not None:
        for o in el.iter("option"):
            d = dict(o.attrib)
            if "enforcedvalues" in d:
                d["enforced"] = d["enforcedvalues"].lower() == "true"
            if "values" in d:
                d["values"] = d["values"]
            opts[d.get("name")] = d
    return opts


def option_values(opt):
    vals = opt.get("values")
    if isinstance(vals, str):
        return [v.strip() for v in vals.split(",") if v.strip()]
    if isinstance(vals, list):
        return [str(v).strip() for v in vals]
    return []


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def check_threadcount(raw, loc, cap, policy, options):
    """Return a list of violation messages for one threadcount value."""
    msgs = []
    if isinstance(raw, bool):
        return [f"{loc}: invalid threadcount {raw!r}"]
    s = str(raw).strip()
    if s == "":
        return []  # empty means default (1)

    m = OPTION_REF.match(s)
    if m or ANY_REF.search(s):
        if policy.dynamic_threadcount == "reject":
            return [f"{loc}: dynamic threadcount '{s}' is not allowed (policy: reject)"]
        if not m:
            return [f"{loc}: threadcount '{s}' mixes text and references; use a single bounded ${{option.NAME}}"]
        name = m.group(1)
        opt = options.get(name)
        if not opt:
            return [f"{loc}: threadcount references option '{name}' which is not defined in the job"]
        vals = option_values(opt)
        if not (opt.get("enforced") and vals):
            return [f"{loc}: option '{name}' must be 'enforced' with an explicit allowed-values list so the thread count is bounded (cap {cap})"]
        bad = []
        for v in vals:
            if not re.fullmatch(r"\d+", v) or int(v) < 1 or int(v) > cap:
                bad.append(v)
        if bad:
            return [f"{loc}: option '{name}' allows values {bad} outside 1..{cap}"]
        return []

    if not re.fullmatch(r"-?\d+", s):
        return [f"{loc}: threadcount '{s}' is not an integer"]
    n = int(s)
    if n < 1:
        msgs.append(f"{loc}: threadcount {n} must be >= 1")
    elif n > cap:
        msgs.append(f"{loc}: threadcount {n} exceeds the maximum allowed ({cap})")
    return msgs


def validate_file(path, rel, policy):
    """Return (job_count, [violation dicts])."""
    results = []
    try:
        jobs = load_jobs(path)
    except Exception as exc:  # unparsable file => cannot be proven safe => fail
        return 0, [dict(file=rel, job="<file>", uuid="", rule="parse-error",
                        message=f"cannot parse file: {str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__}")]
    for job in jobs:
        name = job.get("name", "<unnamed>")
        uuid = job.get("uuid") or job.get("id") or ""
        grp = job.get("group")
        label = f"{grp}/{name}" if grp else str(name)
        options = job_options(job)

        cap = policy.max_threadcount
        ex = policy.find_exception(rel, str(uuid))
        if ex:
            try:
                expires = dt.date.fromisoformat(str(ex["expires"]))
            except ValueError:
                expires = dt.date.min
            if expires < policy.today:
                results.append(dict(file=rel, job=label, uuid=str(uuid), rule="exception-expired",
                                    message=f"exception approved by {ex['approved_by']} expired on {ex['expires']} "
                                            f"(reason: {ex['reason']}); renew or fix the job"))
            else:
                cap = int(ex["max_threadcount"])

        found = list(walk_threadcounts({k: v for k, v in job.items() if k != "__xml__"}))
        if job.get("__xml__") is not None:
            found = list(xml_threadcounts(job["__xml__"]))
        for loc, raw in found:
            for msg in check_threadcount(raw, loc, cap, policy, options):
                results.append(dict(file=rel, job=label, uuid=str(uuid), rule="max-threadcount", message=msg))
    return len(jobs), results


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #
def git_changed_files(ref, repo):
    cmd = ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD"]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError:
        # shallow clones may lack a merge base; fall back to a two-dot diff
        cmd[-1] = f"{ref}..HEAD"
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def all_files(repo, policy):
    files = []
    for p in policy.job_paths:
        base = repo / p
        if base.exists():
            files += [str(f.relative_to(repo)) for f in base.rglob("*") if f.is_file()]
    return sorted(files)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render(fmt, violations, scanned_files, scanned_jobs, policy):
    if fmt == "json":
        return json.dumps(dict(max_threadcount=policy.max_threadcount, files_scanned=scanned_files,
                               jobs_scanned=scanned_jobs, violations=violations), indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["file", "job", "uuid", "rule", "message"])
        w.writeheader()
        w.writerows(violations)
        return buf.getvalue()
    if fmt == "junit":  # GitLab shows this under the MR "Tests" tab
        suite = ET.Element("testsuite", name="rundeck-job-policy",
                           tests=str(max(scanned_files, 1)), failures=str(len({v['file'] for v in violations})))
        by_file = {}
        for v in violations:
            by_file.setdefault(v["file"], []).append(v)
        for f, vs in by_file.items():
            tc = ET.SubElement(suite, "testcase", classname="job-policy", name=f)
            fail = ET.SubElement(tc, "failure", message=f"{len(vs)} violation(s)")
            fail.text = "\n".join(f"[{v['job']}] {v['message']}" for v in vs)
        if not by_file:
            ET.SubElement(suite, "testcase", classname="job-policy", name="all-jobs-compliant")
        return ET.tostring(suite, encoding="unicode")
    if fmt == "github":  # GitHub Actions workflow commands -> inline annotations on the PR diff
        def esc(s):
            return str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        def esc_prop(s):
            return esc(s).replace(":", "%3A").replace(",", "%2C")
        lines = [f"::error file={esc_prop(v['file'])},title={esc_prop('Job policy: ' + v['rule'])}::"
                 f"[{esc(v['job'])}] {esc(v['message'])}" for v in violations]
        return "\n".join(lines)
    # text
    lines = []
    for v in violations:
        lines.append(f"FAIL  {v['file']}  [{v['job']}]  {v['message']}")
    status = "FAILED" if violations else "PASSED"
    lines.append(f"\nJob policy check {status}: {scanned_jobs} job(s) in {scanned_files} file(s), "
                 f"{len(violations)} violation(s), cap={policy.max_threadcount} threads.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", default="policy/job-policy.yml")
    ap.add_argument("--repo", default=".", help="repo root (default: cwd)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--changed-since", metavar="REF")
    g.add_argument("--all", action="store_true")
    g.add_argument("--files", nargs="+")
    ap.add_argument("--format", choices=["text", "json", "csv", "junit", "github"], default="text")
    ap.add_argument("--output")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for deterministic tests")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    try:
        policy = Policy.load(repo / args.policy if not Path(args.policy).is_absolute() else args.policy)
        if args.today:
            policy.today = dt.date.fromisoformat(args.today)
    except Exception as exc:
        sys.stderr.write(f"policy error: {exc}\n")
        return 2

    if args.files:
        candidates = args.files
    elif args.all:
        candidates = all_files(repo, policy)
    else:
        candidates = git_changed_files(args.changed_since, repo)

    violations, nfiles, njobs = [], 0, 0
    for rel in candidates:
        rel = str(Path(rel)).replace("\\", "/")
        if not policy.in_scope(rel):
            continue
        p = repo / rel
        if not p.exists():
            continue
        nfiles += 1
        n, v = validate_file(p, rel, policy)
        njobs += n
        violations += v

    report = render(args.format, violations, nfiles, njobs, policy)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report, encoding="utf-8")
        if args.format != "text":
            # always show a human summary in the CI log too
            sys.stdout.write(render("text", violations, nfiles, njobs, policy) + "\n")
    else:
        sys.stdout.write(report + "\n")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
