"""Add an idempotency key for task history persistence."""


def migrate(migrator, database, fake=False, **kwargs):
    columns = {column.name for column in database.get_columns('completedtasks')}
    if 'source_task_id' not in columns:
        migrator.sql('ALTER TABLE completedtasks ADD COLUMN source_task_id INTEGER')
    migrator.sql(
        'CREATE UNIQUE INDEX IF NOT EXISTS completedtasks_source_task_id_start_time '
        'ON completedtasks (source_task_id, start_time) WHERE source_task_id IS NOT NULL'
    )
    command_log_indexes = database.get_indexes('completedtaskscommandlogs')
    if not any(
            index.unique and tuple(index.columns) == ('completedtask_id',)
            for index in command_log_indexes):
        migrator.sql(
            'DELETE FROM completedtaskscommandlogs WHERE id NOT IN '
            '(SELECT MIN(id) FROM completedtaskscommandlogs GROUP BY completedtask_id)'
        )
        migrator.sql(
            'CREATE UNIQUE INDEX IF NOT EXISTS completedtaskscommandlogs_one_per_task '
            'ON completedtaskscommandlogs (completedtask_id)'
        )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX IF EXISTS completedtaskscommandlogs_one_per_task')
    migrator.sql('DROP INDEX IF EXISTS completedtasks_source_task_id_start_time')
