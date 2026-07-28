#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.envvars.py

    The environment variables Trawlarr reads, and what happens to the names
    it used to read.

    Upstream Unmanic prefixed its environment variables ``UNMANIC_``.
    Trawlarr prefixes them ``TRAWLARR_``. Like the config directory and the
    API path, that rename is a clean break: nothing here ever falls back to
    an ``UNMANIC_`` name, and setting one has no effect on behaviour.

    A clean break on an environment variable is silent in the worst way. The
    application starts, works, and quietly uses a default -- so an operator
    who pinned ``UNMANIC_DEFAULT_PLUGIN_REPO_URL`` at an internal mirror
    starts fetching from GitHub again and finds out when the firewall logs
    it, if ever. Nothing errors, so nothing prompts anyone to look.

    So the break gets a warning. `check_for_legacy_env_vars` reports every
    ``UNMANIC_``-prefixed variable present in the environment, names the
    ``TRAWLARR_`` variable that replaced it, and says plainly that the old
    one is being ignored. Startup continues -- an ignored environment
    variable is not a reason to refuse to boot, the way an unmigrated config
    directory is (see `trawlarr.libs.runtimepaths`) -- but nobody has to
    guess why their setting stopped applying.

    The scan is by prefix, not by lookup table, so variables this module has
    never heard of are still reported. `RENAMED_ENV_VARS` exists to give the
    known ones a better message than the mechanical prefix swap, and to
    document the full set in one place, including the ones consumed by the
    Docker entrypoint and the frontend dev server rather than by Python.

    Keep this module free of intra-package imports. It is read during
    startup before the configuration and the logger exist.
"""

import os

#: Prefix for every environment variable Trawlarr reads.
ENV_VAR_PREFIX = 'TRAWLARR_'

#: The prefix upstream Unmanic used. Detected, never read.
LEGACY_ENV_VAR_PREFIX = 'UNMANIC_'

#: Override the public plugin catalog URL (self-hosted mirrors).
DEFAULT_PLUGIN_REPO_URL_ENV_VAR = ENV_VAR_PREFIX + 'DEFAULT_PLUGIN_REPO_URL'

#: Override the pinned local session/support level.
LOCAL_SESSION_LEVEL_ENV_VAR = ENV_VAR_PREFIX + 'LOCAL_SESSION_LEVEL'

#: Opt in to forwarding logs to a self-hosted sink.
REMOTE_LOGGING_ENDPOINT_ENV_VAR = ENV_VAR_PREFIX + 'REMOTE_LOGGING_ENDPOINT'

#: Opt in to letting a plugin's declared dependencies drive pip (issue #39).
#: New in Trawlarr, so it has no legacy counterpart below. Off by default:
#: see docs/PLUGIN-DEPENDENCIES.md for what enabling it asks you to trust.
ALLOW_PLUGIN_DEPENDENCY_INSTALL_ENV_VAR = ENV_VAR_PREFIX + 'ALLOW_PLUGIN_DEPENDENCY_INSTALL'

#: Legacy name -> current name, for every variable renamed by issue #49.
#:
#: The last four are not read by Python at all -- ``DB_PATH``,
#: ``SQLITE_MAINTENANCE`` and ``RUN_COMMAND`` belong to
#: ``docker/root/entrypoint.sh`` and ``BACKEND_URL`` to the frontend dev
#: server -- but they are inherited by this process, so listing them here
#: means an operator who sets the old name still gets told. ``PROFILE_UNMANIC``
#: carries no ``UNMANIC_`` prefix and would be missed by the prefix scan
#: entirely; it is only ever found because it is named here.
RENAMED_ENV_VARS = {
    'UNMANIC_DEFAULT_PLUGIN_REPO_URL': DEFAULT_PLUGIN_REPO_URL_ENV_VAR,
    'UNMANIC_LOCAL_SESSION_LEVEL':     LOCAL_SESSION_LEVEL_ENV_VAR,
    'UNMANIC_REMOTE_LOGGING_ENDPOINT': REMOTE_LOGGING_ENDPOINT_ENV_VAR,
    'UNMANIC_DB_PATH':                 ENV_VAR_PREFIX + 'DB_PATH',
    'UNMANIC_SQLITE_MAINTENANCE':      ENV_VAR_PREFIX + 'SQLITE_MAINTENANCE',
    'UNMANIC_RUN_COMMAND':             ENV_VAR_PREFIX + 'RUN_COMMAND',
    'UNMANIC_BACKEND_URL':             ENV_VAR_PREFIX + 'BACKEND_URL',
    'PROFILE_UNMANIC':                 'PROFILE_TRAWLARR',
}


def replacement_env_var_name(legacy_name):
    """
    Return the Trawlarr name that replaced a legacy variable.

    Known renames come from `RENAMED_ENV_VARS`. Anything else that carries
    the legacy prefix gets the mechanical swap, which is the rule the whole
    rename followed and is very likely right.

    :param legacy_name:
    :return:
    """
    if legacy_name in RENAMED_ENV_VARS:
        return RENAMED_ENV_VARS[legacy_name]
    if legacy_name.startswith(LEGACY_ENV_VAR_PREFIX):
        return ENV_VAR_PREFIX + legacy_name[len(LEGACY_ENV_VAR_PREFIX):]
    return None


def legacy_env_vars_present(environ=None):
    """
    Return sorted (legacy name, replacement name) pairs found in the environment.

    :param environ:
    :return:
    """
    if environ is None:
        environ = os.environ
    found = {}
    for key in environ:
        if key.startswith(LEGACY_ENV_VAR_PREFIX) or key in RENAMED_ENV_VARS:
            replacement = replacement_env_var_name(key)
            if replacement is not None:
                found[key] = replacement
    return sorted(found.items())


def legacy_env_var_message(environ=None):
    """
    Build the operator-facing warning, or None when there is nothing to say.

    :param environ:
    :return:
    """
    present = legacy_env_vars_present(environ)
    if not present:
        return None
    width = max(len(legacy) for legacy, _ in present)
    lines = [
        'Trawlarr warning: ignoring {} environment variable(s) that Unmanic used to read.'.format(len(present)),
        '',
        'Trawlarr reads {}* names only. There is no fallback to the old names, so'.format(ENV_VAR_PREFIX),
        'each setting below is currently having NO effect and the built-in default is in',
        'use instead. Rename them:',
        '',
    ]
    for legacy, replacement in present:
        lines.append('    {}  ->  {}'.format(legacy.ljust(width), replacement))
    lines += [
        '',
        'Startup continues. This is a warning, not an error - but a setting you believed',
        'was applied is not applied.',
    ]
    return '\n'.join(lines)


def check_for_legacy_env_vars(environ=None):
    """
    Return the warning message, or None when the environment is clean.

    :param environ:
    :return:
    """
    return legacy_env_var_message(environ)
