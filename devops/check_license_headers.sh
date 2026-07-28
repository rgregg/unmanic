#!/bin/bash
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Verify every tracked Python file carries a license header.
#
# The invariant: a .py file is either upstream's (carrying Unmanic's
# copyright block) or fork-authored (carrying an SPDX identifier). A file
# with neither is a new fork file that was committed without a header.
#
# See docs/CONTRIBUTING.md for the header policy.
#
# Usage: devops/check_license_headers.sh

set -euo pipefail

project_root="$(readlink -e "$(dirname "$(readlink -e "${BASH_SOURCE[0]}")")/../")"
cd "${project_root}"

# Files that predate the policy and are upstream's, but carry no header of
# any kind. Grandfathered deliberately — do not add fork-authored files here.
GRANDFATHERED=(
    "unmanic/migrations_v1/001_rename_ffmpeg_log_to_log.py"
    "unmanic/webserver/proxy.py"
)

is_grandfathered() {
    local candidate="$1"
    for entry in "${GRANDFATHERED[@]}"; do
        [ "${entry}" = "${candidate}" ] && return 0
    done
    return 1
}

missing=()

# The vendored frontend tree is a third-party subtree; its headers are not
# ours to police.
while read -r file; do
    is_grandfathered "${file}" && continue
    grep -q 'SPDX-License-Identifier' "${file}" && continue
    grep -q 'Copyright (C) Josh Sunnex' "${file}" && continue
    missing+=("${file}")
done < <(git ls-files '*.py' | grep -v '^unmanic/webserver/frontend/')

if [ ${#missing[@]} -gt 0 ]; then
    echo "The following Python files carry no license header:"
    printf '  %s\n' "${missing[@]}"
    echo
    echo "Fork-authored files need an SPDX header:"
    echo
    echo "  #!/usr/bin/env python3"
    echo "  # -*- coding: utf-8 -*-"
    echo "  #"
    echo "  # SPDX-License-Identifier: GPL-3.0-or-later"
    echo "  # Copyright (C) <year> <your name>"
    echo "  #"
    echo "  # This file is part of Trawlarr, a fork of Unmanic."
    echo "  # See LICENSE for the full license text."
    echo
    echo "See docs/CONTRIBUTING.md for the full policy."
    exit 1
fi

echo "License headers OK ($(git ls-files '*.py' | grep -cv '^unmanic/webserver/frontend/') Python files checked)"
