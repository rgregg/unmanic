#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.completedtasks.py
 
    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     30 Sep 2019, (6:46 PM)
 
    Copyright:
           Copyright (C) Josh Sunnex - All Rights Reserved
 
           Permission is hereby granted, free of charge, to any person obtaining a copy
           of this software and associated documentation files (the "Software"), to deal
           in the Software without restriction, including without limitation the rights
           to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
           copies of the Software, and to permit persons to whom the Software is
           furnished to do so, subject to the following conditions:
  
           The above copyright notice and this permission notice shall be included in all
           copies or substantial portions of the Software.
  
           THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
           EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
           MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
           IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
           DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
           OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
           OR OTHER DEALINGS IN THE SOFTWARE.

"""

import datetime

from peewee import *
from trawlarr.libs.unmodels.lib import BaseModel


class CompletedTasks(BaseModel):
    """
    CompletedTasks
    """
    task_label = TextField(null=False)
    abspath = TextField(null=False, default='', index=True)
    task_success = BooleanField(null=False, index=True)
    start_time = DateTimeField(null=False, default=datetime.datetime.now)
    finish_time = DateTimeField(null=False, default=datetime.datetime.now, index=True)
    processed_by_worker = TextField(null=False)

    # Durable failure state (fork addition; see trawlarr/libs/taskfailure.py
    # and issue #25).
    #
    # All nullable, so that the schema auto-sync in trawlarr/libs/db_migrate.py
    # can add them to an existing database with a plain ALTER TABLE, and so
    # that rows written before the upgrade read back as "failed, reason not
    # recorded" rather than as a fabricated reason.
    #
    # NOTE: none of these carry `index=True`. The auto-sync adds a missing
    # column with peewee_migrate's add_columns, which emits the field's own
    # CREATE INDEX unconditionally - and the index already exists by then, so
    # an indexed new column makes an existing install fail to start. The
    # indexes below are declared on Meta instead, which the auto-sync's
    # separate index pass adds with the existence check. See
    # tests/unit/test_task_failure_state.py::TestExistingInstallUpgrade.
    failure_category = TextField(null=True)
    failure_message = TextField(null=True)
    failure_time = DateTimeField(null=True)
    # Which consecutive failure of this path this was: 1 for the first, 2 for
    # the next one in a row, reset by any success. Drives the retry guard.
    failure_attempt = IntegerField(null=True)
    # Acknowledged by a user. Takes the failure out of the health view without
    # deleting any of the diagnostics.
    failure_dismissed = BooleanField(null=True, default=False)

    class Meta:
        indexes = (
            (('failure_category',), False),
            (('failure_dismissed',), False),
        )
