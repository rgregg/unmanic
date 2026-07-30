# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
"""Peewee migrations -- 002_libraries_allowlist_sql_default.py.

Give `libraries.file_extension_allowlist` a SQL-level DEFAULT.

Issue #33 added the column as `TEXT NOT NULL` with only a peewee-side
`default=''`. peewee applies that default in Python, so the generated DDL
carried no DEFAULT at all and any INSERT that omitted the column failed
with "NOT NULL constraint failed".

That is not a hypothetical. It is what happens when an operator rolls back
to an image from before the allowlist: the old code has never heard of the
column, so the first library it writes - and, on a first boot, the default
library it creates for you - dies on the constraint. Rolling a bad image
back is a normal operator action and it must not leave the installation
unable to start.

The model now declares `constraints=[SQL("DEFAULT ''")]`, which fixes fresh
installs and any database that has yet to gain the column. This migration
exists for the databases in between: those already upgraded onto a build
that added the column without the default. SQLite cannot alter a column in
place, so the value is carried across to a correctly-declared column.
"""

COLUMN = 'file_extension_allowlist'
TEMP_COLUMN = 'file_extension_allowlist_tmp'


def _libraries_ddl(database):
    row = database.execute_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'libraries'").fetchone()
    return row[0] if row and row[0] else ''


def _column_names(database):
    return [column.name for column in database.get_columns('libraries')]


def migrate(migrator, database, fake=False, **kwargs):
    """Write your migrations here."""
    if fake:
        return

    columns = _column_names(database)
    if COLUMN not in columns:
        # Pre-#33 database. The column has not been added yet; the auto-sync
        # step in Migrations.update_schema() adds it after migrations run, and
        # it now carries the DEFAULT from the model. Nothing to do here.
        return

    ddl = _libraries_ddl(database)
    if 'DEFAULT' in ddl.upper():
        # Already declared with a default - a fresh install, or an
        # installation this migration has already been through.
        return

    # SQLite has no ALTER COLUMN. Add a correctly-declared column, carry the
    # configured values across, then swap the names.
    if TEMP_COLUMN in columns:
        database.execute_sql('ALTER TABLE "libraries" DROP COLUMN "{}"'.format(TEMP_COLUMN))
    database.execute_sql(
        'ALTER TABLE "libraries" ADD COLUMN "{}" TEXT NOT NULL DEFAULT \'\''.format(TEMP_COLUMN))
    database.execute_sql(
        'UPDATE "libraries" SET "{}" = COALESCE("{}", \'\')'.format(TEMP_COLUMN, COLUMN))
    database.execute_sql('ALTER TABLE "libraries" DROP COLUMN "{}"'.format(COLUMN))
    database.execute_sql(
        'ALTER TABLE "libraries" RENAME COLUMN "{}" TO "{}"'.format(TEMP_COLUMN, COLUMN))


def rollback(migrator, database, fake=False, **kwargs):
    """Write your rollback migrations here."""
    # Deliberately a no-op. This migration only ever widens what the schema
    # accepts, and the whole reason it exists is that older code cannot insert
    # a row without the default. Taking the default away again on rollback
    # would re-create the failure the operator is rolling back to escape.
    return
