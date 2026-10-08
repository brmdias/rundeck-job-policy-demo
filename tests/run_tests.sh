#!/usr/bin/env bash
# Self-test: every jobs/ok-* file must PASS and every jobs/bad-* file must FAIL.
# Usage: ./tests/run_tests.sh   (run from the demo root)
set -u
cd "$(dirname "$0")/.."
TODAY="2026-10-07"   # fixed so the "expired exception" case is deterministic
pass=0; fail=0

for f in jobs/*; do
  base=$(basename "$f")
  python3 scripts/validate_jobs.py --today "$TODAY" --files "$f" >/tmp/validate.out 2>&1
  rc=$?
  case "$base" in
    ok-*)  expected=0 ;;
    bad-*) expected=1 ;;
    *) continue ;;
  esac
  if [ "$rc" -eq "$expected" ]; then
    printf 'PASS  %-40s (exit %s as expected)\n' "$base" "$rc"; pass=$((pass+1))
  else
    printf 'FAIL  %-40s (exit %s, expected %s)\n' "$base" "$rc" "$expected"; cat /tmp/validate.out
    fail=$((fail+1))
  fi
done

echo "----"
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
