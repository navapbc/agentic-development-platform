#!/bin/sh
# Usage: cis-scan ROOT EVIDENCE_DIR
#
# Evaluates the root file system at ROOT against the CIS Amazon Linux 2023 Level 2 Server profile
# and writes the ARF results, the HTML report, the per-rule results, and a summary to
# EVIDENCE_DIR. The content marks machine rules not applicable inside a container, so ROOT gets
# the /run/.containerenv marker a container runtime creates. Exits 1 on a rule that neither passes
# nor is not applicable unless cis-accepted.txt names it.
set -eu

root=$1
evidence=$2
content=/usr/local/share/cis
mkdir -p "${evidence}" "${root}/run"
: > "${root}/run/.containerenv"

status=0
OSCAP_PROBE_ROOT="${root}" oscap xccdf eval --progress \
    --profile xccdf_org.ssgproject.content_profile_cis \
    --results-arf "${evidence}/arf.xml" \
    --report "${evidence}/report.html" \
    "${content}/ssg-al2023-ds.xml" > "${evidence}/oscap.log" 2>&1 || status=$?
if [ "${status}" -ne 0 ] && [ "${status}" -ne 2 ]; then
    cat "${evidence}/oscap.log" >&2
    exit 1
fi

sed -n 's/^xccdf_org\.ssgproject\.content_rule_\([^:]*\):\([a-z]*\)$/\1\t\2/p' "${evidence}/oscap.log" \
    | sort > "${evidence}/results.tsv"
if [ ! -s "${evidence}/results.tsv" ]; then
    echo "oscap reported no rule results" >&2
    exit 1
fi
grep -v '^#' "${content}/cis-accepted.txt" | awk 'NF { print $1 }' | sort -u > /tmp/cis-accepted
awk -F '\t' '$2 != "pass" && $2 != "notapplicable" { print $1 "\t" $2 }' "${evidence}/results.tsv" > /tmp/cis-open
awk -F '\t' 'FILENAME == ARGV[1] { accepted[$1] = 1; next } !($1 in accepted)' /tmp/cis-accepted /tmp/cis-open > "${evidence}/blocking.tsv"
cut -f2 "${evidence}/results.tsv" | sort | uniq -c | awk '{ print $2 "\t" $1 }' > "${evidence}/summary.tsv"
rm -f /tmp/cis-accepted /tmp/cis-open

cat "${evidence}/summary.tsv"
if [ -s "${evidence}/blocking.tsv" ]; then
    echo "rules that neither pass nor are accepted:" >&2
    cat "${evidence}/blocking.tsv" >&2
    exit 1
fi
