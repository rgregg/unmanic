#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_library_allowlist_downgrade.py

    Rolling an image back is a normal operator action, and it has to work.

    Issue #33 added `libraries.file_extension_allowlist` as `TEXT NOT NULL`
    with only peewee's Python-side `default=''`. peewee applies that default
    when IT builds the INSERT, so the generated DDL carried no SQL DEFAULT.

    An operator who pulls a bad image, finds it bad, and rolls back to the
    previous one is then running code that has never heard of the column. Every
    INSERT it writes omits it, and SQLite answers "NOT NULL constraint failed:
    libraries.file_extension_allowlist". The operator cannot create a library -
    and on a first start the application creates the default library for you,
    so this is a startup failure, not a cosmetic one. Rolling forward again
    does not repair the schema either, because the column already exists and
    the auto-sync step in update_schema() leaves existing columns alone.

    The tests below build real databases with `Migrations.update_schema()` and
    then do what the old image does: insert a library row naming only the
    columns that existed before #33.
"""
import os
import sqlite3
import sys
import inspect as _inspect

import pytest

from peewee import Model, SqliteDatabase

from trawlarr.libs.db_migrate import Migrations

#: Exactly the columns an image from before #33 knows about.
PRE_33_COLUMNS = ('name', 'path', 'locked', 'enable_remote_only', 'enable_scanner',
                  'enable_inotify', 'priority_score')

#: The `libraries` table as it stood before #33.
PRE_33_SCHEMA = """
CREATE TABLE "libraries" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "path" TEXT NOT NULL,
    "locked" INTEGER NOT NULL,
    "enable_remote_only" INTEGER NOT NULL,
    "enable_scanner" INTEGER NOT NULL,
    "enable_inotify" INTEGER NOT NULL,
    "priority_score" INTEGER NOT NULL
);
"""

#: A database that has already been upgraded onto a build which added the
#: column without a SQL default. These exist in the wild; the model change
#: alone does not reach them, which is what migration 002 is for.
NO_DEFAULT_SCHEMA = """
CREATE TABLE "libraries" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "name" TEXT NOT NULL,
    "path" TEXT NOT NULL,
    "locked" INTEGER NOT NULL,
    "enable_remote_only" INTEGER NOT NULL,
    "enable_scanner" INTEGER NOT NULL,
    "enable_inotify" INTEGER NOT NULL,
    "priority_score" INTEGER NOT NULL,
    "file_extension_allowlist" TEXT NOT NULL
);
"""


def _run_update_schema(db_file):
    """Run the real application startup path against db_file.

    peewee-migrate rebinds every model's Meta.database as a side effect of
    creating tables, so this is done inside a bind_ctx that puts the original
    bindings back afterwards and keeps the rest of the suite clean. Same
    approach as tests/unit/test_library_allowlist_upgrade.py.
    """
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(
        sys.modules['trawlarr.libs.db_migrate'].__file__)))
    settings = {
        "TYPE":                       "SQLITE",
        "FILE":                       db_file,
        "MIGRATIONS_DIR":             os.path.join(app_dir, "migrations_v1"),
        "MIGRATIONS_HISTORY_VERSION": "v1",
    }
    discovered = _inspect.getmembers(sys.modules["trawlarr.libs.unmodels"], _inspect.isclass)
    all_models = [model for _name, model in discovered if issubclass(model, Model)]

    scratch_db = SqliteDatabase(db_file)
    with scratch_db.bind_ctx(all_models):
        Migrations(settings).update_schema()
    scratch_db.close()


def _insert_as_the_old_image_would(db_file, name):
    """Insert a library naming only the pre-#33 columns.

    This is what a rolled-back image's peewee models emit. It raises
    sqlite3.IntegrityError if the new column has no usable default.
    """
    connection = sqlite3.connect(db_file)
    try:
        connection.execute(
            'INSERT INTO libraries ({}) VALUES (?, ?, ?, ?, ?, ?, ?)'.format(
                ', '.join('"{}"'.format(c) for c in PRE_33_COLUMNS)),
            (name, '/mnt/' + name, 0, 0, 1, 1, 0))
        connection.commit()
    finally:
        connection.close()


def _libraries_ddl(db_file):
    connection = sqlite3.connect(db_file)
    try:
        return connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'libraries'").fetchone()[0]
    finally:
        connection.close()


def _allowlists(db_file):
    connection = sqlite3.connect(db_file)
    try:
        return {row[0]: row[1] for row in connection.execute(
            'SELECT name, file_extension_allowlist FROM libraries')}
    finally:
        connection.close()


@pytest.mark.unittest
class TestARolledBackImageCanStillWrite:

    def test_a_fresh_install_survives_a_rollback(self, tmp_path):
        """Install the current build, then roll back. The old code must be
        able to create a library."""
        db_file = str(tmp_path / 'fresh.db')
        _run_update_schema(db_file)

        _insert_as_the_old_image_would(db_file, 'Films')

        assert _allowlists(db_file)['Films'] == '', (
            "the rolled-back image's row did not get the empty allow-list, "
            "which is the value that means 'no restriction'")

    def test_an_upgraded_install_survives_a_rollback(self, tmp_path):
        """The upgrade path: a pre-#33 database gains the column here, and
        that column must be declared with the default too."""
        db_file = str(tmp_path / 'upgraded.db')
        connection = sqlite3.connect(db_file)
        connection.executescript(PRE_33_SCHEMA)
        connection.execute(
            'INSERT INTO libraries VALUES (1, "Default", "/library", 0, 0, 1, 1, 0)')
        connection.commit()
        connection.close()

        _run_update_schema(db_file)

        _insert_as_the_old_image_would(db_file, 'Films')

        assert _allowlists(db_file)['Films'] == ''

    def test_a_database_already_missing_the_default_is_repaired(self, tmp_path):
        """The in-between case the model change cannot reach on its own.

        The column already exists, so update_schema()'s auto-sync step skips
        it. Migration 002 has to rebuild it - and must carry the operator's
        configured allow-lists across while doing so.
        """
        db_file = str(tmp_path / 'nodefault.db')
        connection = sqlite3.connect(db_file)
        connection.executescript(NO_DEFAULT_SCHEMA)
        connection.execute(
            'INSERT INTO libraries VALUES (1, "Default", "/library", 0, 0, 1, 1, 0, "")')
        connection.execute(
            'INSERT INTO libraries VALUES (2, "TV", "/mnt/tv", 0, 0, 1, 0, 100, "mkv,mp4")')
        connection.commit()
        connection.close()

        _run_update_schema(db_file)

        assert _allowlists(db_file)['TV'] == 'mkv,mp4', (
            "the repair discarded a configured allow-list")

        _insert_as_the_old_image_would(db_file, 'Films')

        assert _allowlists(db_file)['Films'] == ''

    def test_the_repair_is_idempotent_across_restarts(self, tmp_path):
        """Containers restart. The second boot must not repeat the rebuild or
        disturb anyone's configuration."""
        db_file = str(tmp_path / 'restart.db')
        connection = sqlite3.connect(db_file)
        connection.executescript(NO_DEFAULT_SCHEMA)
        connection.execute(
            'INSERT INTO libraries VALUES (2, "TV", "/mnt/tv", 0, 0, 1, 0, 100, "mkv,mp4")')
        connection.commit()
        connection.close()

        _run_update_schema(db_file)
        _run_update_schema(db_file)

        assert _allowlists(db_file) == {'TV': 'mkv,mp4'}
        _insert_as_the_old_image_would(db_file, 'Films')
        assert _allowlists(db_file)['Films'] == ''


@pytest.mark.unittest
class TestTheColumnIsDeclaredWithASqlDefault:

    def test_the_generated_ddl_carries_the_default(self, tmp_path):
        """Pins the mechanism, not just the symptom.

        peewee's `default=` is Python-side only; the SQL DEFAULT is what makes
        a writer that does not know the column work.
        """
        db_file = str(tmp_path / 'ddl.db')
        _run_update_schema(db_file)

        ddl = _libraries_ddl(db_file)

        assert 'file_extension_allowlist' in ddl
        assert 'DEFAULT' in ddl.upper(), (
            "libraries.file_extension_allowlist has no SQL DEFAULT:\n{}".format(ddl))
