#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_migration_replay.py

    Adding a migration must not break the installations that already ran the
    earlier ones.

    peewee-migrate does not keep a schema snapshot. Before it runs a NEW
    migration it rebuilds the migrator's ORM state by re-running every
    already-applied migration with `peewee.Database.execute_sql` and
    `peewee.Model.select` mocked out - "fake replay" (Router.migrator,
    router.py:93). A migration that introspects the database during that
    replay gets a Mock back and explodes:

        TypeError: 'Mock' object is not iterable

    Nothing about that failure mentions the new migration, and it cannot
    happen on the machine that authored it: a developer's database has the new
    migration recorded, so there is no diff and no replay. It only fires on
    the installations that are upgrading - i.e. all of them. Migration 001 had
    this shape, so PR #86's 002 made every existing install fail to start.

    The invariant these tests pin is deliberately general, because the next
    migration is the one nobody will remember this for: applying migration N
    to a database where 1..N-1 are already recorded must succeed.
"""
import os
import shutil
import sqlite3
import sys
import inspect as _inspect

import pytest

from peewee import Model, SqliteDatabase

from trawlarr.libs.db_migrate import Migrations

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(sys.modules['trawlarr.libs.db_migrate'].__file__))),
    'migrations_v1')


def migration_names():
    """Every migration module in migrations_v1, in the order peewee-migrate runs them."""
    return sorted(
        name[:-3] for name in os.listdir(MIGRATIONS_DIR)
        if name.endswith('.py') and not name.startswith('_')
    )


@pytest.fixture
def run_schema_update(tmp_path):
    """Run the real startup schema path against a database, with a chosen set of migrations.

    peewee-migrate rebinds every model's Meta.database as a side effect of
    creating tables, so each run happens inside a bind_ctx that puts the
    original bindings back and keeps this test from leaking into the rest of
    the suite.
    """
    discovered = _inspect.getmembers(sys.modules["trawlarr.libs.unmodels"], _inspect.isclass)
    all_models = [model for _name, model in discovered if issubclass(model, Model)]
    counter = {'n': 0}

    def _settings(db_file, only=None):
        if only is None:
            migrations_dir = MIGRATIONS_DIR
        else:
            counter['n'] += 1
            migrations_dir = str(tmp_path / 'migrations_subset_{}'.format(counter['n']))
            os.makedirs(migrations_dir)
            for name in only:
                shutil.copy(os.path.join(MIGRATIONS_DIR, name + '.py'), migrations_dir)
        return {
            "TYPE":                       "SQLITE",
            "FILE":                       db_file,
            "MIGRATIONS_DIR":             migrations_dir,
            "MIGRATIONS_HISTORY_VERSION": "v1",
        }

    def _run(db_file, only=None, replay_only=False):
        """Bring `db_file` up to date using only the named migrations (default: all of them).

        With replay_only, stop at the point peewee-migrate fake-replays the
        already-applied migrations to rebuild its ORM state - which is what
        happens on every installation that has a new migration waiting.
        """
        settings = _settings(db_file, only)
        scratch_db = SqliteDatabase(db_file)
        try:
            with scratch_db.bind_ctx(all_models):
                migrations = Migrations(settings)
                if replay_only:
                    migrations.router.migrator  # noqa: B018 - the cached_property IS the replay
                else:
                    migrations.update_schema()
        finally:
            scratch_db.close()

    return _run


def _applied(db_file):
    connection = sqlite3.connect(db_file)
    try:
        return [row[0] for row in connection.execute('SELECT name FROM migratehistory_v1 ORDER BY id')]
    finally:
        connection.close()


def _table_ddl(db_file, table):
    connection = sqlite3.connect(db_file)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        return row[0] if row else ''
    finally:
        connection.close()


def _columns(db_file, table):
    connection = sqlite3.connect(db_file)
    try:
        return [row[1] for row in connection.execute('PRAGMA table_info("{}")'.format(table))]
    finally:
        connection.close()


class TestAnExistingInstallCanTakeANewMigration:
    """The exact shape that broke: 001 already applied, 002 pending.

    `test_the_repair_is_idempotent_across_restarts` does not cover this. Its
    second call has no diff, so Router.run() returns at its "nothing to
    migrate" early return and never builds the migrator - which is where the
    replay happens.
    """

    def test_a_database_with_001_applied_accepts_the_next_migration(self, tmp_path, run_schema_update):
        db_file = str(tmp_path / 'trawlarr.db')

        # Build the database the way an existing installation was built: the
        # real startup chain, with only the migrations that shipped before.
        run_schema_update(db_file, only=['001_rename_ffmpeg_log_to_log'])

        assert _applied(db_file) == ['001_rename_ffmpeg_log_to_log'], (
            "The fixture did not produce the database shape under test. This test is only "
            "meaningful if 001 is recorded as applied before the new migration is offered."
        )

        # Now start the version that adds 002. This is the upgrade.
        run_schema_update(db_file)

        assert _applied(db_file) == migration_names(), (
            "Starting a build with a new migration did not apply it to an existing install."
        )
        ddl = _table_ddl(db_file, 'libraries')
        assert 'file_extension_allowlist" TEXT NOT NULL DEFAULT' in ddl, (
            "Migration 002 ran but the allowlist column still has no SQL default:\n{}".format(ddl))


class TestEveryMigrationSurvivesReplayOfTheEarlierOnes:
    """The general guard. A new migration file makes this test cover it automatically."""

    @pytest.mark.parametrize('index', range(len(migration_names())))
    def test_applying_one_migration_over_all_the_earlier_ones(self, index, tmp_path, run_schema_update):
        names = migration_names()
        earlier, target = names[:index], names[index]
        db_file = str(tmp_path / 'trawlarr.db')

        run_schema_update(db_file, only=earlier)
        assert _applied(db_file) == earlier

        run_schema_update(db_file, only=earlier + [target])

        assert _applied(db_file) == earlier + [target], (
            "Applying {} to a database that already had {} did not record it. Every migration "
            "before the new one is fake-replayed first, and a migration that introspects the "
            "database during that replay breaks every installation that is upgrading.".format(
                target, earlier or 'nothing')
        )

    def test_the_whole_applied_set_can_be_replayed(self, tmp_path, run_schema_update):
        """The newest migration has no successor yet, so nothing above replays it.

        This closes that gap directly: apply everything, then do what
        Router.migrator does on the day migration N+1 lands. A migration that
        introspects the database under the mocked execute_sql fails here, on
        the branch that adds it, rather than on somebody's installation months
        later when the next migration is written.
        """
        db_file = str(tmp_path / 'trawlarr.db')
        run_schema_update(db_file)
        assert _applied(db_file) == migration_names()

        run_schema_update(db_file, replay_only=True)


class TestMigration001StillRepairsALegacyDatabase:
    """Replay safety must not have been bought by making 001 stop working.

    001 exists for legacy Unmanic databases, whose `tasks` table carries a NOT
    NULL `ffmpeg_log` column. Left alone, nothing can be added to the task
    queue at all.
    """

    @pytest.fixture
    def legacy_db(self, tmp_path, run_schema_update):
        """A database in the legacy shape, built by the real chain and then walked back."""
        db_file = str(tmp_path / 'trawlarr.db')
        run_schema_update(db_file, only=[])

        connection = sqlite3.connect(db_file)
        connection.execute('ALTER TABLE "tasks" RENAME COLUMN "log" TO "ffmpeg_log"')
        connection.execute('DELETE FROM migratehistory_v1')
        connection.commit()
        connection.close()

        assert 'ffmpeg_log' in _columns(db_file, 'tasks')
        return db_file

    def test_the_legacy_column_is_renamed(self, legacy_db, run_schema_update):
        run_schema_update(legacy_db)

        columns = _columns(legacy_db, 'tasks')
        assert 'ffmpeg_log' not in columns, (
            "A legacy database still has its ffmpeg_log column after migration 001 ran. "
            "The NOT NULL constraint on it blocks every insert into the task queue.")
        assert 'log' in columns
        assert '001_rename_ffmpeg_log_to_log' in _applied(legacy_db)

    def test_a_modern_database_is_left_alone(self, tmp_path, run_schema_update):
        """001 must not fire on a healthy database - it has nothing to repair there."""
        db_file = str(tmp_path / 'trawlarr.db')
        run_schema_update(db_file)

        columns = _columns(db_file, 'tasks')
        assert 'log' in columns and 'ffmpeg_log' not in columns
