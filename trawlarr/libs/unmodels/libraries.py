#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.libraries.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     06 Feb 2022, (9:44 AM)

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

from peewee import *

from trawlarr.libs.unmodels.lib import BaseModel
from trawlarr.libs.unmodels.tags import Tags


class Libraries(BaseModel):
    """
    Libraries
    """
    name = TextField(null=False, unique=True)
    path = TextField(null=False)
    locked = BooleanField(null=False, default=False)
    # Retained for schema compatibility only. The "receive remote files only" option was
    # retired with the Link feature (see #52); nothing reads this column any more. Dropping
    # it would require a migration for no benefit, so it stays at its default.
    enable_remote_only = BooleanField(null=False, default=False)
    enable_scanner = BooleanField(null=False, default=False)
    enable_inotify = BooleanField(null=False, default=False)
    priority_score = BigIntegerField(null=False, default=0)
    # Trawlarr fork addition (see issue #33). Comma-separated list of file
    # extensions that this library will consider for processing, stored
    # without leading dots and lower-cased.
    #
    # The default is the empty string, and empty means NO RESTRICTION - see
    # trawlarr/libs/extensions.py for why. An existing installation upgrading
    # onto this column gets '' for every library and so keeps its current
    # behaviour exactly.
    #
    # The SQL-level DEFAULT is load-bearing, not decoration. peewee's `default`
    # is applied in Python, so without the constraint the column lands as
    # "TEXT NOT NULL" with no default and any INSERT that omits it fails. That
    # is exactly what an operator rolling back to a pre-allowlist image does:
    # the old code has never heard of this column, so every library it writes
    # trips a NOT NULL constraint. With DEFAULT '' the old image keeps working
    # and rolling forward again is a no-op.
    file_extension_allowlist = TextField(null=False, default='',
                                         constraints=[SQL("DEFAULT ''")])
    # ManyToMany Linking field. Does not create a column in the DB. See linking table below
    tags = ManyToManyField(Tags, backref='tags')


# Generate linking table for the 'tags' field above
# https://docs.peewee-orm.com/en/latest/peewee/relationships.html#manytomanyfield
LibraryTags = Libraries.tags.get_through_model()
