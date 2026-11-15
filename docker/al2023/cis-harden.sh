#!/bin/sh
# Applies the ComplianceAsCode remediation of the CIS Amazon Linux 2023 Level 2 Server profile to
# the image being built. The content skips machine rules inside a container, which it detects by
# /.dockerenv, a file a build step does not have, so the step creates it for the run. An image runs
# this as the last step that runs as root, so the rules the packages it installed made applicable
# are remediated too.
#
# The remediation script does not stop on a missing command, which would skip that rule's fix, so
# the tools its file and account fixes call are installed for the run, removed afterwards unless
# the image depends on them, and any "command not found" fails the build.
set -eu

content=/usr/local/share/cis
log=/tmp/cis-harden.log
release=$(cat "${content}/releasever")

rpm -qa --qf '%{NAME}\n' | sort > /tmp/cis-harden-before
dnf -y --releasever="${release}" install \
    findutils shadow-utils passwd util-linux-core crypto-policies-scripts > /dev/null
rpm -qa --qf '%{NAME}\n' | sort > /tmp/cis-harden-after
added=$(comm -13 /tmp/cis-harden-before /tmp/cis-harden-after | tr '\n' ' ')
rm -f /tmp/cis-harden-before /tmp/cis-harden-after

: > /.dockerenv
status=0
bash "${content}/al2023-script-cis.sh" > "${log}" 2>&1 || status=$?
rm -f /.dockerenv

if grep -q "command not found" "${log}"; then
    grep "command not found" "${log}" | sort -u >&2
    status=1
fi
if [ "${status}" -ne 0 ]; then
    tail -n 40 "${log}" >&2
fi
rm -f "${log}"

if [ -n "${added}" ]; then
    rpm -e ${added} > /dev/null
fi
dnf clean all > /dev/null
rm -rf /var/cache/dnf /var/log/dnf*
exit "${status}"
