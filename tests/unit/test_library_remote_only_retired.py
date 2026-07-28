#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_library_remote_only_retired.py

    Regression coverage for the retirement of the per-library
    "receive remote files only" option (enable_remote_only), see #52.

    That option was fed exclusively by the removed Link upload path. While the
    Link feature was still present, three code paths skipped any library with
    the flag set:

      - LibraryScannerManager.scheduled_job
      - EventMonitorManager.run
      - EventMonitorManager.start_event_processor

    With Link gone, such a library could never receive work from anywhere, so
    the option was retired: the skip branches were removed and the accessors
    deleted. The DB column is retained for schema compatibility but is no
    longer read.

    These tests pin the recovery behaviour: a library whose row still carries
    enable_remote_only=True is scanned and monitored like any other. The fake
    library objects below deliberately raise AttributeError from
    get_enable_remote_only, so any code that reads the flag again fails loudly
    rather than silently reintroducing the inert-library bug.
"""
import logging
import threading
from unittest import mock

from unmanic.libs.eventmonitor import EventMonitorManager
from unmanic.libs.libraryscanner import LibraryScannerManager


class _RemoteOnlyLibrary:
    """
    Stands in for a library row that still has enable_remote_only=True set.

    Reading the retired flag is an error: nothing in the application should
    consult it any more.
    """

    def __init__(self, library_id=7, name='Remote Only Library', path='/library/remote-only'):
        self._id = library_id
        self._name = name
        self._path = path

    def get_enable_remote_only(self):
        raise AttributeError(
            "enable_remote_only was retired with the Link feature (#52) and must not be read"
        )

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_path(self):
        return self._path

    def get_enable_scanner(self):
        return True

    def get_enable_inotify(self):
        return True


class TestLibraryScannerScansRemoteOnlyLibraries:

    def test_scheduled_job_scans_a_library_with_enable_remote_only_set(self):
        manager = LibraryScannerManager.__new__(LibraryScannerManager)
        manager.logger = logging.getLogger("test_remote_only_scanner")
        manager.scan_library_path = mock.Mock()
        manager.system_configuration_is_valid = mock.Mock(return_value=True)

        library = _RemoteOnlyLibrary()
        with mock.patch("unmanic.libs.libraryscanner.Library") as LibraryCls:
            LibraryCls.get_all_libraries.return_value = [{'id': 7}]
            LibraryCls.return_value = library
            manager.scheduled_job()

        manager.scan_library_path.assert_called_once_with(
            'Remote Only Library', '/library/remote-only', 7
        )


class TestEventMonitorMonitorsRemoteOnlyLibraries:

    def test_start_event_processor_schedules_a_remote_only_library_path(self):
        manager = EventMonitorManager.__new__(EventMonitorManager)
        manager.logger = logging.getLogger("test_remote_only_monitor")
        manager.event = threading.Event()
        manager.files_to_test = None
        manager.event_observer_thread = None

        observer = mock.Mock()
        library = _RemoteOnlyLibrary()
        with mock.patch("unmanic.libs.eventmonitor.Library") as LibraryCls, \
                mock.patch("unmanic.libs.eventmonitor.Observer", return_value=observer), \
                mock.patch("unmanic.libs.eventmonitor.os.path.exists", return_value=True):
            LibraryCls.get_all_libraries.return_value = [{'id': 7}]
            LibraryCls.return_value = library
            manager.start_event_processor()

        assert observer.schedule.call_count == 1
        scheduled_path = observer.schedule.call_args[0][1]
        assert scheduled_path == '/library/remote-only'
        observer.start.assert_called_once_with()

    def test_run_loop_enables_inotify_for_a_remote_only_library(self):
        manager = EventMonitorManager.__new__(EventMonitorManager)
        manager.logger = logging.getLogger("test_remote_only_monitor_run")
        manager.event = threading.Event()
        manager.files_to_test = mock.Mock()
        manager.files_to_test.empty.return_value = True
        manager.abort_flag = threading.Event()
        manager.event_observer_thread = None
        manager.system_configuration_is_valid = mock.Mock(return_value=True)
        manager.stop_event_processor = mock.Mock()

        # Leave the loop as soon as it has decided to start the monitor.
        manager.start_event_processor = mock.Mock(side_effect=lambda: manager.abort_flag.set())

        library = _RemoteOnlyLibrary()

        with mock.patch("unmanic.libs.eventmonitor.Library") as LibraryCls, \
                mock.patch.object(manager.event, 'wait'):
            LibraryCls.get_all_libraries.return_value = [{'id': 7}]
            LibraryCls.return_value = library
            manager.run()

        # The monitor was started for the remote-only library rather than skipped.
        manager.start_event_processor.assert_called_once_with()
