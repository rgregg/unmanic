#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    filesanitystate.py

    Per-file memory for the output sanity checks (see trawlarr/libs/sanity.py
    and issue #35).

    A single task that grows a file is normal - adding a stereo downmix, for
    instance. What is not normal is the same file growing again on the next
    task, and again on the one after that. Detecting that pattern requires
    remembering what the previous task did to this path, which is what this
    table is for.

    Keyed on the file's absolute path rather than a content fingerprint: the
    whole point is to follow a file across rewrites, and every rewrite gives
    it a new fingerprint.
"""

import datetime

from peewee import *
from trawlarr.libs.unmodels.lib import BaseModel


class FileSanityState(BaseModel):
    """
    FileSanityState

    One row per library file that has completed at least one task.
    """
    abspath = TextField(null=False, index=True, unique=True)
    # Consecutive completed tasks on this path whose output had more streams
    # than its input. Reset to 0 by any task that does not grow the file.
    stream_growth_streak = IntegerField(null=False, default=0)
    # As above, for file size.
    size_growth_streak = IntegerField(null=False, default=0)
    # The structure the last task left behind, for the log/report only.
    last_stream_count = IntegerField(null=True)
    last_size = IntegerField(null=True)
    updated_at = DateTimeField(null=False, default=datetime.datetime.now)

    class Meta:
        table_name = 'file_sanity_state'
