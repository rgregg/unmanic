#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    filecompletionstate.py

    The platform's native record of "this file is done" (see issue #33 and
    trawlarr/libs/donestate.py).

    One row per library file that a task has completed successfully. The row
    stores a cheap content signature of the file *as the completing task left
    it*, so that a later library scan can tell the difference between:

      - the same file, untouched since it was processed  -> still done
      - a different file that happens to occupy the same path (a re-download,
        a restore from backup, an external edit)         -> not done, re-queue

    Keyed on the absolute path with the signature as a qualifier, rather than
    on the signature alone: the question being asked at scan time is always
    "what do I know about *this path*", and a path lookup is an index hit
    instead of a hash of every file in the library on every scan.
"""

import datetime

from peewee import *
from trawlarr.libs.unmodels.lib import BaseModel


class FileCompletionState(BaseModel):
    """
    FileCompletionState

    One row per library file completed successfully by this installation.
    """
    abspath = TextField(null=False, index=True, unique=True)
    # The library the completing task belonged to. Informational: the done
    # state is a property of the file, not of the library that produced it,
    # so a file moved between libraries stays done.
    library_id = IntegerField(null=False, default=0)
    # Content signature of the file at the moment it was completed, and the
    # algorithm that produced it. See donestate.build_signature().
    signature = TextField(null=False, default='')
    signature_algo = TextField(null=False, default='')
    # The task that completed it, for the log and for support questions.
    task_id = IntegerField(null=True)
    completed_at = DateTimeField(null=False, default=datetime.datetime.now)

    class Meta:
        table_name = 'file_completion_state'
