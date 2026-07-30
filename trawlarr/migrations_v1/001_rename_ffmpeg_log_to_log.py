"""Peewee migrations -- 001_rename_ffmpeg_log_to_log.py.

Some examples (model - class or model name)::

    > Model = migrator.orm['model_name']            # Return model in current state by name

    > migrator.sql(sql)                             # Run custom SQL
    > migrator.python(func, *args, **kwargs)        # Run python code
    > migrator.create_model(Model)                  # Create a model (could be used as decorator)
    > migrator.remove_model(model, cascade=True)    # Remove a model
    > migrator.add_fields(model, **fields)          # Add fields to a model
    > migrator.change_fields(model, **fields)       # Change fields
    > migrator.remove_fields(model, *field_names, cascade=True)
    > migrator.rename_field(model, old_field_name, new_field_name)
    > migrator.rename_table(model, new_table_name)
    > migrator.add_index(model, *col_names, unique=False)
    > migrator.drop_index(model, *col_names)
    > migrator.add_not_null(model, *field_names)
    > migrator.drop_not_null(model, *field_names)
    > migrator.add_default(model, field_name, default)

"""

import peewee as pw
from decimal import ROUND_HALF_EVEN

try:
    import playhouse.postgres_ext as pw_pext
except ImportError:
    pass

SQL = pw.SQL

"""NOTES:
The migrator function 'rename_field' needs the model to already be in the migrator's ORM snapshot,
which it is not here - nothing in this migration set creates the 'tasks' model. Upstream worked
around that by reaching into `migrator.ops` / `migrator.migrator`, which are not the attribute names
peewee_migrate has used since 1.5 (they are `__ops__` / `__migrator__`), so that call raised
AttributeError on exactly the legacy databases this migration exists to repair. The rename is issued
as SQL through the public `migrator.sql()` instead: it is queued on the migrator like any other
operation and runs inside the migration transaction. SQLite has supported
ALTER TABLE ... RENAME COLUMN since 3.25.
"""


"""Note:
This migration is required for legacy installations.
The old Unmanic had a ffmpeg_log column which had a NOT NULL contraint on it.
If this column is left as is, no items will be able to be added to the task queue.
"""


def migrate(migrator, database, fake=False, **kwargs):
    """Write your migrations here."""
    if fake:
        # Fake replay. peewee_migrate re-runs every already-applied migration with
        # peewee.Database.execute_sql mocked out, purely to rebuild the migrator's ORM
        # snapshot before a NEW migration runs (Router.migrator). Introspection returns a
        # Mock under that patch, so touching the database here breaks every future migration
        # for every existing installation. This migration adds nothing to the ORM snapshot -
        # it issues raw SQL against a table no migration declares - so there is nothing to
        # replay and skipping is exact.
        return
    # Rename 'ffmpeg_log' field to 'log'' in Tasks model
    if any(cm for cm in database.get_columns('tasks') if cm.name == 'ffmpeg_log'):
        migrator.sql('ALTER TABLE "tasks" RENAME COLUMN "ffmpeg_log" TO "log"')


def rollback(migrator, database, fake=False, **kwargs):
    """Write your rollback migrations here."""
    if fake:
        return
    # Reverse rename 'ffmpeg_log' field to 'log'' in Tasks model
    if any(cm for cm in database.get_columns('tasks') if cm.name == 'log'):
        migrator.sql('ALTER TABLE "tasks" RENAME COLUMN "log" TO "ffmpeg_log"')
