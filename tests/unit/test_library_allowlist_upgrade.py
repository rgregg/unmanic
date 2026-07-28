#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_library_allowlist_upgrade.py

    Issue #33 adds a column to `libraries` and a whole new table. The question
    that decides whether this is a safe release is not "does a fresh install
    work" - it is "what happens to somebody's populated installation when they
    pull the new image".

    These tests build a database with the PRE-#33 schema, populate it the way
    a real installation would be populated, run the same startup path the
    service runs (trawlarr.libs.db_migrate.Migrations.update_schema), and then
    assert what that installation does next.

    The answer that must hold: every existing library gets an empty
    allow-list, an empty allow-list means no restriction, and so nothing an
    existing library used to queue stops being queued. The opposite reading of
    "empty" - allow nothing - would idle every installation on earth on
    upgrade, and an idle Trawlarr looks exactly like a healthy Trawlarr with
    no work to do.
"""
import os
import sqlite3

import pytest

from trawlarr.libs import extensions

#: The `libraries` table exactly as it stood before this change: upstream's
#: columns plus `enable_remote_only`, which #52 retained for schema
#: compatibility.
PRE_33_LIBRARIES_SCHEMA = """
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

EXISTING_LIBRARIES = [
    (1, 'Default', '/library', 0, 0, 1, 1, 0),
    (2, 'Films', '/mnt/films', 0, 0, 1, 0, 100),
    (3, 'TV', '/mnt/tv', 1, 0, 0, 1, -50),
]


@pytest.fixture
def upgraded_db(tmp_path):
    """A pre-#33 database, brought up to date by the real startup path.

    peewee-migrate rebinds every model's Meta.database as a side effect of
    creating tables, so the whole thing is run inside a bind_ctx that puts the
    original bindings back and keeps this test from leaking into the rest of
    the suite.
    """
    import sys
    import inspect as _inspect
    from peewee import Model, SqliteDatabase
    from trawlarr.libs.db_migrate import Migrations

    db_file = str(tmp_path / 'trawlarr.db')

    connection = sqlite3.connect(db_file)
    connection.executescript(PRE_33_LIBRARIES_SCHEMA)
    connection.executemany(
        'INSERT INTO libraries (id, name, path, locked, enable_remote_only, enable_scanner, enable_inotify, '
        'priority_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', EXISTING_LIBRARIES)
    connection.commit()
    connection.close()

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(
        sys.modules['trawlarr.libs.db_migrate'].__file__)))
    settings = {
        "TYPE":                        "SQLITE",
        "FILE":                        db_file,
        "MIGRATIONS_DIR":              os.path.join(app_dir, "migrations_v1"),
        "MIGRATIONS_HISTORY_VERSION":  "v1",
    }

    discovered = _inspect.getmembers(sys.modules["trawlarr.libs.unmodels"], _inspect.isclass)
    all_models = [model for _name, model in discovered if issubclass(model, Model)]

    scratch_db = SqliteDatabase(db_file)
    with scratch_db.bind_ctx(all_models):
        Migrations(settings).update_schema()
        yield db_file
    scratch_db.close()


def _columns(db_file, table):
    connection = sqlite3.connect(db_file)
    try:
        return {row[1]: row for row in connection.execute('PRAGMA table_info("{}")'.format(table))}
    finally:
        connection.close()


def _tables(db_file):
    connection = sqlite3.connect(db_file)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


class TestExistingInstallUpgrade:

    def test_the_allowlist_column_is_added(self, upgraded_db):
        assert 'file_extension_allowlist' in _columns(upgraded_db, 'libraries')

    def test_the_completion_state_table_is_created(self, upgraded_db):
        assert 'file_completion_state' in _tables(upgraded_db)

    def test_no_existing_library_row_is_lost_or_altered(self, upgraded_db):
        connection = sqlite3.connect(upgraded_db)
        try:
            rows = connection.execute(
                'SELECT id, name, path, locked, enable_scanner, enable_inotify, priority_score '
                'FROM libraries ORDER BY id').fetchall()
        finally:
            connection.close()

        assert rows == [(1, 'Default', '/library', 0, 1, 1, 0),
                        (2, 'Films', '/mnt/films', 0, 1, 0, 100),
                        (3, 'TV', '/mnt/tv', 1, 0, 1, -50)]

    def test_every_existing_library_gets_an_empty_allowlist(self, upgraded_db):
        connection = sqlite3.connect(upgraded_db)
        try:
            values = [row[0] for row in connection.execute(
                'SELECT file_extension_allowlist FROM libraries ORDER BY id')]
        finally:
            connection.close()

        assert values == ['', '', '']

    def test_an_upgraded_library_still_accepts_every_file_it_used_to(self, upgraded_db):
        """The whole point. Read the stored value back out of the upgraded
        database and put it through the code that gates the library scan."""
        connection = sqlite3.connect(upgraded_db)
        try:
            stored = connection.execute(
                'SELECT file_extension_allowlist FROM libraries WHERE id = 2').fetchone()[0]
        finally:
            connection.close()

        for path in ['/mnt/films/movie.mkv', '/mnt/films/movie.avi', '/mnt/films/movie.mp4',
                     '/mnt/films/subs.srt', '/mnt/films/poster.jpg', '/mnt/films/README']:
            assert extensions.extension_is_allowed(path, stored) is True

    def test_running_the_upgrade_twice_is_a_no_op(self, upgraded_db):
        """Containers restart. The second boot must not fail or reset anyone's
        configuration."""
        import sys
        from trawlarr.libs.db_migrate import Migrations

        connection = sqlite3.connect(upgraded_db)
        connection.execute("UPDATE libraries SET file_extension_allowlist = 'mkv,mp4' WHERE id = 2")
        connection.commit()
        connection.close()

        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(
            sys.modules['trawlarr.libs.db_migrate'].__file__)))
        Migrations({
            "TYPE":                       "SQLITE",
            "FILE":                       upgraded_db,
            "MIGRATIONS_DIR":             os.path.join(app_dir, "migrations_v1"),
            "MIGRATIONS_HISTORY_VERSION": "v1",
        }).update_schema()

        connection = sqlite3.connect(upgraded_db)
        try:
            stored = connection.execute(
                'SELECT file_extension_allowlist FROM libraries WHERE id = 2').fetchone()[0]
        finally:
            connection.close()

        assert stored == 'mkv,mp4'

    def test_a_completed_file_recorded_after_the_upgrade_reads_back(self, upgraded_db):
        """The new table is not just present, it is usable by the code that
        was written against it."""
        from peewee import SqliteDatabase
        from trawlarr.libs import donestate
        from trawlarr.libs.unmodels import FileCompletionState

        video = os.path.join(os.path.dirname(upgraded_db), 'episode.mkv')
        with open(video, 'wb') as f:
            f.write(b'x' * 512)

        database = SqliteDatabase(upgraded_db)
        with database.bind_ctx([FileCompletionState]):
            assert donestate.record_completion(video, library_id=2, task_id=1) is True
            assert donestate.file_is_already_completed(video)[0] is True
        database.close()
