#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.runtimepaths.py

    The names Trawlarr uses on disk and on the wire, in one place.

    Trawlarr keeps its configuration in ``~/.trawlarr/`` and serves itself
    under ``/trawlarr/``. Upstream Unmanic used ``~/.unmanic/`` and
    ``/unmanic/``. That rename is a clean break: there is no migration, no
    symlinking and no legacy alias for either the directory or the URL
    prefix.

    A clean break on a URL is loud — the old path 404s and whoever called it
    finds out immediately. A clean break on a config directory is silent:
    the application would simply not find an existing install and would
    create a new, empty one beside it. Every library, plugin, setting and
    completed task would appear to be gone, when in fact nothing had been
    touched.

    So the directory rename gets a guard. `check_for_legacy_config_directory`
    looks for exactly that situation — data in the legacy directory, nothing
    in the new one — and returns a message. The caller refuses to start and
    prints it. Nothing is moved, nothing is deleted; the operator moves their
    own data, which is the decision recorded in issue #49.

    Keep this module free of intra-package imports. It is read during startup
    before the configuration and the logger exist.
"""

import os

#: Name of the application directory inside the user's home directory.
APP_DIR_NAME = '.trawlarr'

#: The directory upstream Unmanic used. Read only to detect it, never written.
LEGACY_APP_DIR_NAME = '.unmanic'

#: Filename of the SQLite database inside <APP_DIR_NAME>/config/.
DATABASE_FILE_NAME = 'trawlarr.db'

#: Upstream's database filename. Only ever used in operator-facing messages.
LEGACY_DATABASE_FILE_NAME = 'unmanic.db'

#: Root path prefix for everything the web server serves.
URL_PREFIX = '/trawlarr'

#: Root path prefix for the HTTP API, e.g. /trawlarr/api/v2/version/read.
API_URL_PREFIX = '{}/api'.format(URL_PREFIX)

#: Setting this to a truthy value suppresses the legacy-directory guard for
#: operators who know the legacy directory is stale and want a fresh start.
IGNORE_LEGACY_CONFIG_ENV_VAR = 'TRAWLARR_IGNORE_LEGACY_CONFIG'

_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


def app_dir(home_directory):
    """
    Return the Trawlarr application directory for the given home directory.

    :param home_directory:
    :return:
    """
    return os.path.join(home_directory, APP_DIR_NAME)


def legacy_app_dir(home_directory):
    """
    Return the legacy Unmanic application directory for the given home directory.

    :param home_directory:
    :return:
    """
    return os.path.join(home_directory, LEGACY_APP_DIR_NAME)


def _directory_holds_data(path):
    """
    True when the path is a directory that contains anything at all.

    Emptiness rather than existence is the test that matters here. The
    Docker entrypoint creates the application directory before the
    application runs, so "the new directory exists" would be true on the
    first start of an upgraded container and would defeat the guard
    entirely.

    :param path:
    :return:
    """
    if not os.path.isdir(path):
        return False
    try:
        return any(os.scandir(path))
    except OSError:
        # Unreadable is not the same as absent. Assume it holds data so
        # the guard errs towards refusing to start rather than towards
        # silently starting fresh.
        return True


def legacy_config_directory_conflict(home_directory):
    """
    True when a legacy install is present and the new location is unused.

    :param home_directory:
    :return:
    """
    if os.environ.get(IGNORE_LEGACY_CONFIG_ENV_VAR, '').strip().lower() in _TRUTHY:
        return False
    return (
        _directory_holds_data(legacy_app_dir(home_directory))
        and not _directory_holds_data(app_dir(home_directory))
    )


def legacy_config_migration_command_lines(home_directory):
    """
    The migration, as shell lines an operator can paste verbatim.

    Three things about this sequence are load-bearing, and all three were
    got wrong the obvious way first:

    1. **The database is renamed in place, under the legacy path, before
       anything moves.** If a later step fails, everything is still where
       it was and the guard fires again on the next start. Relocating
       first and renaming second leaves a half-migrated install that the
       guard can no longer see, because the new directory now holds data.

    2. **The destination is removed with `rmdir` before the move.** The
       Docker entrypoint runs `mkdir -p /config/.trawlarr` before the
       application starts — including on the run that refuses — so the
       destination almost always exists. `mv src existing_dir` does not
       fail: it moves src *inside* it, producing
       `/config/.trawlarr/.unmanic/`, and the next start comes up as a
       brand new install with the real data one level down. `mkdir -p`
       then `rmdir` succeeds whether or not the destination existed, and
       refuses (loudly) if it turns out to hold anything.

    3. **Every step is chained with `&&`.** A failure stops the sequence
       instead of scrolling one line past an operator who is already
       looking at the next command.

    The `-wal` and `-shm` sidecars are renamed with the database. The
    schema sets `journal_mode=wal`, so a database whose write-ahead log
    was left behind under the old name silently loses whatever had not
    been checkpointed.

    :param home_directory:
    :return:
    """
    legacy_dir = legacy_app_dir(home_directory)
    new_dir = app_dir(home_directory)
    legacy_config_dir = os.path.join(legacy_dir, 'config')

    def _legacy_db(suffix=''):
        return os.path.join(legacy_config_dir, LEGACY_DATABASE_FILE_NAME + suffix)

    def _new_db(suffix=''):
        return os.path.join(legacy_config_dir, DATABASE_FILE_NAME + suffix)

    lines = ['mv {} {} &&'.format(_legacy_db(), _new_db())]
    for suffix in ('-wal', '-shm'):
        lines.append('{{ [ ! -e {0} ] || mv {0} {1}; }} &&'.format(_legacy_db(suffix), _new_db(suffix)))
    lines.append('mkdir -p {0} && rmdir {0} &&'.format(new_dir))
    lines.append('mv {} {}'.format(legacy_dir, new_dir))
    return lines


def legacy_config_migration_command(home_directory):
    """
    The migration as a single shell command string.

    :param home_directory:
    :return:
    """
    return '\n'.join(legacy_config_migration_command_lines(home_directory))


def legacy_config_directory_message(home_directory):
    """
    Build the operator-facing refuse-to-start message.

    Names both directories in full, says where the migration has to be run,
    and gives a command sequence that is correct whether or not the
    destination directory already exists.

    :param home_directory:
    :return:
    """
    legacy_dir = legacy_app_dir(home_directory)
    new_dir = app_dir(home_directory)
    return '\n'.join([
        'Trawlarr refused to start: an Unmanic configuration directory is present, but',
        'Trawlarr has not been given one.',
        '',
        '    legacy configuration:  {}'.format(legacy_dir),
        '    expected by Trawlarr:  {}'.format(new_dir),
        '',
        'Trawlarr no longer reads {}. Starting anyway would look like a brand new'.format(LEGACY_APP_DIR_NAME),
        'installation - no libraries, no plugins, no settings, no task history - while your',
        'existing data sat untouched in the old directory. Refusing to start instead.',
        '',
        'Nothing has been moved or deleted. To carry the existing installation across, run',
        'the following on the HOST. Trawlarr is refusing to start, so there is no running',
        'container to "docker exec" into; if these paths are inside a container, apply the',
        'same sequence to the host directory you bind-mount at /config.',
        '',
    ] + [
        '    {}'.format(line) for line in legacy_config_migration_command_lines(home_directory)
    ] + [
        '',
        'Paste it whole. It is one "&&"-chained command on purpose: if any step fails the',
        'rest do not run, and everything is still where it was. The mkdir/rmdir pair is not',
        'redundant - the destination directory usually already exists and is empty, and',
        '"mv old new" would then move the old directory INSIDE the new one rather than',
        'becoming it. rmdir removes it only while it is empty, so if it turns out to hold',
        'anything the sequence stops rather than burying your installation one level down.',
        '',
        'The API also moved, from {}/api/v2/ to {}/api/v2/. Update any'.format(
            '/{}'.format(LEGACY_APP_DIR_NAME.lstrip('.')), URL_PREFIX),
        'bookmarks, scripts or reverse-proxy rules that name the old path.',
        '',
        'To start fresh and keep the old directory where it is, set {}=1.'.format(
            IGNORE_LEGACY_CONFIG_ENV_VAR),
    ])


def check_for_legacy_config_directory(home_directory):
    """
    Return the refuse-to-start message, or None when it is safe to start.

    :param home_directory:
    :return:
    """
    if legacy_config_directory_conflict(home_directory):
        return legacy_config_directory_message(home_directory)
    return None
