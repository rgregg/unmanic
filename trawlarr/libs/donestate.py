#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.donestate.py

    A native concept of "this file is done" (issue #33).

    THE PROBLEM
    -----------
    Upstream's FileTest natively knows only one terminal state: a file that
    FAILED in history is never queued again. There is no native counterpart
    for a file that SUCCEEDED. "Already processed, leave it alone" was left to
    an ordinary file-test plugin (`ignore_completed_tasks`) whose vote sits in
    the same overridable pool as every other plugin's. When some other plugin
    outvoted it, the file was queued, processed, completed, scanned, queued
    again - an infinite reprocess loop that looks exactly like a working
    installation from the outside. That is the failure mode this module exists
    to remove.

    WHAT IS RECORDED, AND WHEN
    --------------------------
    The post-processor calls record_completion() for each destination file of
    every task that finished successfully. The row stores a signature of the
    file as that task left it.

    WHAT THE SIGNATURE IS, AND WHY
    ------------------------------
    st_size + st_mtime_ns, from a single stat() call.

    The issue floated an ffprobe fingerprint or a content hash instead. Both
    were rejected for the scan path: this check runs against every file in
    every library on every scan, and reading (or probing) every file to decide
    whether to skip it costs more than the work being skipped. A stat is what
    the scanner already pays for.

    What size+mtime buys is the distinction the issue actually asked for:

      - a rescan of an untouched file      -> identical signature -> done
      - a re-download to the same path     -> new mtime           -> not done
      - an external edit / restore         -> new mtime or size   -> not done

    What it does NOT buy, stated plainly: a file rewritten in place, in the
    same second-of-nanosecond resolution the filesystem reports, to exactly
    the same byte count, with the mtime deliberately restored (`touch -r`,
    some rsync and backup-restore modes, `cp --preserve=timestamps` over the
    top of a file of identical size) is indistinguishable from the file we
    completed, and will be treated as done. Anyone in that position can clear
    the record - see forget_path() - or queue the file manually, which does
    not consult this module at all.

    The signature algorithm is stored alongside the signature. A row written
    by a different algorithm cannot be compared, so it is treated as "not
    done" rather than assumed either way. There is exactly one algorithm
    today; the column exists so that a future content-hash mode can coexist
    with rows written before it.

    THE SEAM FOR #41 (deliberate reprocessing)
    ------------------------------------------
    This check is deliberately stronger than a plugin vote: nothing in the
    plugin layer can override it. That would be a trap if there were no way
    to ask for a completed file to be processed again, so there are three:

      1. forget_path() / forget_paths() drop the record for a path. The next
         scan sees an unknown file and queues it normally. This is the hook
         #41 should build its "reprocess" action on.
      2. FileTest(library_id, ignore_completed_files=True) skips the check for
         one caller without touching any stored state.
      3. Queueing a file by hand (webserver/helpers/pending_tasks.create_task,
         used by the UI's "add to queue" and by file upload) does not run
         FileTest at all, so it already bypasses this - and always did.

    NO EXPIRY
    ---------
    Unlike the sanity-check state, rows here are never aged out. "Done" does
    not stop being true after a year, and expiring rows would silently restart
    the very loop this module was written to stop. The table therefore holds
    at most one small row per library file that has ever been processed.
"""

import datetime
import os

from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.unmodels import FileCompletionState

#: Identifier for the signature scheme below. Stored on every row so that a
#: future scheme can tell "written by an algorithm I do not know" apart from
#: "written by me and does not match".
SIGNATURE_ALGO = 'size_mtime_ns_v1'

logger = TrawlarrLogging.get_logger(name='DoneState')


def build_signature(abspath):
    """
    Build the content signature for a file on disk.

    :param abspath:
    :return: signature string, or None when the file cannot be stat'd
    """
    if not abspath:
        return None
    try:
        stat_result = os.stat(abspath)
    except OSError:
        # Missing, unreadable, or a dangling symlink. Not something this
        # module should have an opinion about - say "I do not know".
        return None
    mtime_ns = getattr(stat_result, 'st_mtime_ns', None)
    if mtime_ns is None:
        mtime_ns = int(stat_result.st_mtime * 1000000000)
    return '{}:{}'.format(stat_result.st_size, mtime_ns)


def record_completion(abspath, library_id=0, task_id=None):
    """
    Record that `abspath` has been completed successfully.

    Where a task renamed the file, the caller is responsible for dropping the
    record held against the path it came from - see forget_path(). That is
    deliberately not folded in here: a single task can deliver several
    destination files, and a per-destination "forget the source" would have
    each delivery undo the last one's record when the source is also one of
    the destinations.

    Never raises: a database problem must not stop a completed task being
    delivered. It only means the file may be looked at again on the next scan.

    :param abspath:
    :param library_id:
    :param task_id:
    :return: True when a row was written
    """
    if not abspath:
        return False
    signature = build_signature(abspath)
    if signature is None:
        # We were told this file was completed but it is not there to be
        # measured. Recording an unverifiable row would mean skipping whatever
        # later appears at this path, which is the silent-failure direction.
        logger.warning("Completed file '%s' could not be read to record its done state", abspath)
        return False
    now = datetime.datetime.now()
    try:
        row = FileCompletionState.get_or_none(FileCompletionState.abspath == abspath)
        if row is None:
            FileCompletionState.create(
                abspath=abspath,
                library_id=library_id or 0,
                signature=signature,
                signature_algo=SIGNATURE_ALGO,
                task_id=task_id,
                completed_at=now,
            )
        else:
            row.library_id = library_id or 0
            row.signature = signature
            row.signature_algo = SIGNATURE_ALGO
            row.task_id = task_id
            row.completed_at = now
            row.save()
    except Exception:
        logger.exception("Unable to record completed state for '%s'", abspath)
        return False
    return True


def file_is_already_completed(abspath):
    """
    Has this exact file already been completed successfully?

    Never raises. On any doubt - no row, an unreadable file, an unknown
    signature algorithm, a database error - the answer is False, because the
    cost of wrongly answering False is one extra pass over a file, while the
    cost of wrongly answering True is a file that silently never gets
    processed.

    :param abspath:
    :return: (bool, message) - message is non-empty only when the answer is
             True, and is written for the /pending/test issue list and the log
    """
    signature = build_signature(abspath)
    if signature is None:
        # Nothing on disk to compare against. Let the rest of the pipeline
        # deal with a path that cannot be stat'd.
        return False, ''
    try:
        row = FileCompletionState.get_or_none(FileCompletionState.abspath == abspath)
    except Exception:
        logger.exception("Unable to read completed state for '%s'", abspath)
        return False, ''
    if row is None:
        return False, ''

    if row.signature_algo != SIGNATURE_ALGO:
        logger.warning(
            "Completed state for '%s' was written by signature algorithm '%s' and cannot be checked against '%s'. "
            "Treating the file as not yet completed.", abspath, row.signature_algo, SIGNATURE_ALGO)
        return False, ''

    if row.signature != signature:
        # Same path, different file. A re-download, a restore, an edit. The
        # completion we recorded does not apply to what is there now, so drop
        # it and let the file be queued as new.
        logger.info("File '%s' has changed since it was completed by task %s. Re-queueing.", abspath, row.task_id)
        forget_path(abspath)
        return False, ''

    return True, "File was already completed successfully by task {} on {} and has not changed since - '{}'".format(
        row.task_id, row.completed_at, abspath)


def forget_path(abspath):
    """
    Drop the completion record for a path, making the file eligible again.

    This is the supported way to ask for a completed file to be reprocessed
    (issue #41). Never raises.

    :param abspath:
    :return: number of rows removed
    """
    return forget_paths([abspath])


def forget_paths(abspaths):
    """
    forget_path() for several paths at once.

    :param abspaths:
    :return: number of rows removed
    """
    paths = [p for p in (abspaths or []) if p]
    if not paths:
        return 0
    try:
        return FileCompletionState.delete().where(FileCompletionState.abspath.in_(paths)).execute()
    except Exception:
        logger.exception("Unable to clear completed state for %s path(s)", len(paths))
        return 0
