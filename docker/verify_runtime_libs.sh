#!/usr/bin/env bash
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Build-time assertion for the runtime stage of docker/Dockerfile.
#
# The runtime stage installs a *smaller* package set than the builder. The
# failure mode that creates is a shared object that resolved during the
# build and does not resolve in the shipped image -- an ffmpeg that exits
# "error while loading shared libraries" the first time an operator runs a
# transcode, or a Python C extension that fails to import at boot.
#
# That is exactly the class of bug the issue behind the multi-stage split
# (#5) called out as the hard part, and exactly the class of bug a hand-
# maintained "runtime sonames" list gets wrong quietly. So instead of
# trusting a list, walk every ELF the image ships in the directories that
# matter and ask the dynamic linker. A missing library fails the build,
# in CI, before the image is pushed.
#
# What this does NOT cover: libraries opened with dlopen() at runtime
# (VAAPI/Vulkan drivers, libcuda from the NVIDIA container runtime). Those
# have no DT_NEEDED entry, so ldd cannot see them. They are covered by
# installing the driver packages, not by this check.
#
# Usage: docker/verify_runtime_libs.sh [dir ...]
#   Defaults to the directories the image is assembled from.

set -uo pipefail

dirs=("$@")
if [[ ${#dirs[@]} -eq 0 ]]; then
    dirs=(/opt/venv /usr/lib/btbn-ffmpeg /usr/lib/jellyfin-ffmpeg)
fi

checked=0
failed=0

for dir in "${dirs[@]}"; do
    if [[ ! -d "${dir}" ]]; then
        echo "**** (verify-runtime-libs) ${dir} not present, skipping"
        continue
    fi
    while IFS= read -r -d '' file; do
        # Only ELF objects have dynamic dependencies. ldd on anything else
        # is noise (and, on a shell script, would be a security footgun).
        magic=$(head -c 4 "${file}" 2>/dev/null | tr -d '\0')
        [[ "${magic}" == $'\x7f'ELF ]] || continue

        checked=$((checked + 1))
        output=$(ldd "${file}" 2>/dev/null)
        missing=$(echo "${output}" | grep 'not found' || true)
        if [[ -n "${missing}" ]]; then
            echo "**** (verify-runtime-libs) MISSING for ${file}:"
            echo "${missing}" | sed 's/^/        /'
            failed=$((failed + 1))
        fi
    done < <(find "${dir}" -type f -print0)
done

if [[ "${failed}" -ne 0 ]]; then
    echo "**** (verify-runtime-libs) ${failed} of ${checked} ELF objects have unresolved libraries."
    echo "**** (verify-runtime-libs) Add the providing package to the runtime stage of docker/Dockerfile."
    exit 1
fi

echo "**** (verify-runtime-libs) ${checked} ELF objects checked, all libraries resolve."
