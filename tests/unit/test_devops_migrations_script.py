#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_devops_migrations_script.py

    devops/migrations.sh is the only supported way to hand-write a migration,
    and it has to talk to the same place the application reads from.

    It pointed at `trawlarr/migrations`, a directory that does not exist. The
    application loads migrations from `trawlarr/migrations_v1`
    (service.py:init_db -> MIGRATIONS_DIR). So `pw_migrate create` wrote a new
    migration into a directory the application never opens: the migration
    would simply never run, and the developer would have no signal at all -
    the script exits 0.

    It also let pw_migrate default to the `migratehistory` table, while the
    application records applied migrations in `migratehistory_v1`
    (Migrations.__init__, MIGRATIONS_HISTORY_VERSION). `list` therefore
    reported against a table nothing else writes, and `migrate`/`rollback`
    operated on a private history - so a manual `migrate` would happily
    re-apply migrations the application had already run.

    The script is kept rather than deleted: creating and rolling back
    migrations by hand is the workflow that produced migrations_v1/002, and
    there is no other entry point for it.

    These assertions are on the script text because the alternative is
    shelling out to pw_migrate against a real database in a unit test. What
    matters is that the two directory/table values cannot drift from the
    application's without a test failing.
"""
import os
import re

import pytest

import trawlarr.libs.db_migrate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_PATH = os.path.join(REPO_ROOT, 'devops', 'migrations.sh')
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(
    trawlarr.libs.db_migrate.__file__)))


@pytest.fixture(scope='module')
def script_text():
    with open(SCRIPT_PATH, 'r') as handle:
        return handle.read()


def _application_migrations_dir():
    """The directory the application actually loads migrations from, read out
    of service.py rather than restated here."""
    with open(os.path.join(PACKAGE_DIR, 'service.py'), 'r') as handle:
        source = handle.read()
    match = re.search(r'"MIGRATIONS_DIR":\s*os\.path\.join\(app_dir,\s*"([^"]+)"\)', source)
    assert match, "service.py no longer declares MIGRATIONS_DIR in the expected form"
    return match.group(1)


def _application_history_version():
    with open(os.path.join(PACKAGE_DIR, 'service.py'), 'r') as handle:
        source = handle.read()
    match = re.search(r'"MIGRATIONS_HISTORY_VERSION":\s*"([^"]+)"', source)
    assert match, "service.py no longer declares MIGRATIONS_HISTORY_VERSION in the expected form"
    return match.group(1)


@pytest.mark.unittest
class TestMigrationsScriptMatchesTheApplication:

    def test_the_directory_the_application_reads_actually_exists(self):
        """Guards the premise of everything below."""
        assert os.path.isdir(os.path.join(PACKAGE_DIR, _application_migrations_dir()))

    def test_the_script_writes_where_the_application_reads(self, script_text):
        expected = _application_migrations_dir()
        match = re.search(r'MIGRATIONS_PATH=\$\(realpath[^)]*trawlarr/([A-Za-z0-9_]+)"\)', script_text)
        assert match, "could not find the MIGRATIONS_PATH assignment in devops/migrations.sh"
        assert match.group(1) == expected, (
            "devops/migrations.sh writes migrations to trawlarr/{}, but the application "
            "loads them from trawlarr/{}. Migrations created with this script would "
            "never run.".format(match.group(1), expected))

    def test_the_script_uses_the_application_history_table(self, script_text):
        expected = 'migratehistory_{}'.format(_application_history_version())
        assert '--migratetable=' in script_text, (
            "devops/migrations.sh does not pass --migratetable, so pw_migrate defaults "
            "to 'migratehistory' while the application records applied migrations in "
            "'{}'.".format(expected))
        assert expected in script_text, (
            "devops/migrations.sh does not use the application's migration history "
            "table '{}'".format(expected))

    def test_the_database_path_survives_the_file_not_existing_yet(self, script_text):
        """Plain `realpath` fails on a missing path and prints nothing, which
        silently produced `--database=sqlite:///`."""
        for line in script_text.splitlines():
            if line.startswith('DATABASE_FILE=') or line.startswith('TEST_DATABASE_FILE='):
                assert 'realpath -m' in line, (
                    "{} uses plain realpath; it resolves to an empty string when the "
                    "database file does not exist yet".format(line.split('=')[0]))
