#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.extensions.py

    The per-library file extension allow-list (issue #33).

    WHY THIS IS LIBRARY CONFIGURATION AND NOT A PLUGIN
    --------------------------------------------------
    "Which files in this directory are even candidates for processing" is a
    property of the library, not an opinion a plugin holds about a file. It
    was previously supplied by `limit_library_search_by_file_extension`, whose
    False vote lived in the same overridable pool as every other plugin's -
    so a requester plugin could outvote it and drag a subtitle file or a
    poster image into the task queue. Moving it into library config takes it
    out of the vote entirely.

    THE DEFAULT IS EMPTY, AND EMPTY MEANS "NO RESTRICTION"
    ------------------------------------------------------
    This is the load-bearing decision for upgrades.

    An empty allow-list could reasonably mean either "allow nothing" or
    "allow everything". "Allow nothing" is not survivable as an upgrade
    default: every existing library would silently stop queueing work, and
    the symptom - an idle installation - looks identical to a healthy
    installation with nothing to do. Nobody would find that for weeks.

    So empty means unrestricted, and the column defaults to empty. An existing
    installation therefore behaves on the release after this change exactly as
    it did on the release before it: every file is still offered to the
    file-test plugins, and anyone relying on
    `limit_library_search_by_file_extension` keeps the behaviour they had. The
    allow-list is opt-in, and turning it on is a deliberate, visible act.

    The same default applies to newly created libraries, for consistency and
    because guessing a video extension list on the user's behalf would quietly
    exclude whatever we failed to think of.
"""

import os

#: Storage form is a comma-separated string in a single TEXT column. The
#: separator characters accepted on input are deliberately generous, because
#: this is a field humans type into.
_INPUT_SEPARATORS = (',', ';', '\n', '\r', '\t', ' ')


def normalise_extension(value):
    """
    Normalise one extension to its stored form: lower case, no leading dot.

    :param value:
    :return: normalised extension, or '' when there is nothing usable
    """
    if value is None:
        return ''
    extension = str(value).strip().lower()
    while extension.startswith('.'):
        extension = extension[1:]
    return extension.strip()


def parse_allowlist(value):
    """
    Parse a stored (or user-supplied) allow-list into a tuple of extensions.

    Accepts either the stored string form or a list/tuple, so that the API can
    hand this whatever the client sent. Duplicates are removed, order is kept.

    :param value:
    :return: tuple of normalised extensions
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        candidates = list(value)
    else:
        text = str(value)
        for separator in _INPUT_SEPARATORS:
            text = text.replace(separator, ',')
        candidates = text.split(',')

    extensions = []
    for candidate in candidates:
        extension = normalise_extension(candidate)
        if extension and extension not in extensions:
            extensions.append(extension)
    return tuple(extensions)


def format_allowlist(value):
    """
    Render an allow-list into the form stored in the libraries table.

    :param value:
    :return: comma-separated string ('' when the list is empty)
    """
    return ','.join(parse_allowlist(value))


def path_extension(path):
    """
    The normalised extension of a path ('' when it has none).

    :param path:
    :return:
    """
    if not path:
        return ''
    return normalise_extension(os.path.splitext(str(path))[1])


def extension_is_allowed(path, allowlist):
    """
    Is `path` within the scope of `allowlist`?

    An empty allow-list allows everything. See the module docstring: that is
    the deliberate upgrade default, not an oversight.

    :param path:
    :param allowlist:
    :return:
    """
    allowed = parse_allowlist(allowlist)
    if not allowed:
        return True
    return path_extension(path) in allowed
