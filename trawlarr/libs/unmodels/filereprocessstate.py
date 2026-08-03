#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    filereprocessstate.py

    The audit trail for deliberate reprocessing (see issue #41 and
    trawlarr/libs/reprocess.py).

    One row per file whose completed state has ever been invalidated on
    purpose. It exists for two reasons, and both of them are about the thing
    reprocessing deliberately switches off:

      1. VISIBILITY. Issue #33's done-state is what stops a file being
         processed twice. Removing that record is a destructive act with no
         other trace - the row simply disappears and the next scan queues the
         file as though it had never been seen. Without this table there is
         nothing afterwards that can answer "why is this file being encoded
         again?".

      2. LOOP DETECTION. A reprocess loop is, by definition, the same file
         being invalidated over and over. `occurrences` counts exactly that,
         and `requested_at` is what the cooldown in reprocess.py compares
         against before it will invalidate the same path a second time.

    Note that this is NOT the same signal as the non-convergence count in
    FileConvergenceState. That one counts completions that did not achieve
    what the library asked for. This one counts deliberate invalidations,
    including of files that converge perfectly every time - a library being
    re-encoded nightly by a script is invisible to convergence and obvious
    here.

    Rows are keyed on the path, not on the request, so the table holds at
    most one small row per file that has ever been reprocessed rather than
    growing with every request.
"""

import datetime

from peewee import *
from trawlarr.libs.unmodels.lib import BaseModel


class FileReprocessState(BaseModel):
    """
    FileReprocessState

    One row per library file whose completed state has been deliberately
    invalidated, holding the most recent request and a running count.
    """
    abspath = TextField(null=False, index=True, unique=True)
    # The library the invalidated completion record belonged to, where one
    # was recorded. 0 when unknown.
    library_id = IntegerField(null=False, default=0)
    # When the most recent invalidation was requested. The cooldown compares
    # against this.
    requested_at = DateTimeField(null=False, default=datetime.datetime.now)
    # How many times this path has been invalidated in total. A number that
    # keeps climbing is the loop signal this table exists to provide.
    occurrences = IntegerField(null=False, default=1)
    # A human-readable rendering of the filter that selected the file, so the
    # audit trail says which request this file came from and not only when.
    requested_by = TextField(null=False, default='')

    class Meta:
        table_name = 'file_reprocess_state'
