#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime

from peewee import DateTimeField, ForeignKeyField, TextField

from unmanic.libs.unmodels.lib import BaseModel
from unmanic.libs.unmodels.tasks import Tasks


class TaskLifecycle(BaseModel):
    """Internal durable task state that is never exposed as plugin metadata."""

    task = ForeignKeyField(
        Tasks,
        backref='task_lifecycle',
        on_delete='CASCADE',
        unique=True,
    )
    json_blob = TextField(null=False, default='{}')
    updated_at = DateTimeField(null=False, default=datetime.datetime.now)

    class Meta:
        table_name = 'task_lifecycle'
