#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    fileconvergencestate.py

    Files that completed a task and STILL match their library's plugin
    criteria (see trawlarr/libs/convergence.py and issue #34).

    This is not a failed task, so it deliberately does not live on the
    completed_tasks row alongside the #25 failure columns. The task succeeded;
    what is wrong is the relationship between the library's criteria and what
    the plugin flow is able to produce. That is a property of the FILE and the
    CONFIGURATION, not of one task, which is why it is keyed on the path and
    why the row survives the completed task being deleted from history.

    A row exists only while the file is still non-converged. The next task on
    the file that does converge it removes the row - see convergence.clear().
"""

import datetime

from peewee import *
from trawlarr.libs.unmodels.lib import BaseModel


class FileConvergenceState(BaseModel):
    """
    FileConvergenceState

    One row per library file that finished a task still qualifying for one.
    """
    abspath = TextField(null=False, index=True, unique=True)
    library_id = IntegerField(null=False, default=0)
    # How many completed tasks in a row have left this file still qualifying.
    # Two or more is no longer bad luck; it is a configuration or plugin bug.
    occurrences = IntegerField(null=False, default=1)
    # Which plugin still wants the file, so the report names a culprit rather
    # than just asserting that something is wrong.
    plugin_id = TextField(null=True)
    plugin_name = TextField(null=True)
    message = TextField(null=True)
    # The task that most recently left the file in this state.
    task_id = IntegerField(null=True)
    first_seen = DateTimeField(null=False, default=datetime.datetime.now)
    last_seen = DateTimeField(null=False, default=datetime.datetime.now)
    # Acknowledged by a human. Kept out of the health view, but still counted
    # in `occurrences` so a dismissal cannot hide an escalating problem.
    dismissed = BooleanField(null=False, default=False)

    class Meta:
        table_name = 'file_convergence_state'
