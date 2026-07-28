#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_phone_home_misc_stubs.py

    Pin the smaller phone-home stubs we added on top of session.py:
    - PluginsHandler.notify_site_of_plugin_install (plugin install telemetry)
    - ScheduledTasksManager.run no longer schedules register_unmanic
    - Session.__configure_log_forwarding never falls back to the central API
"""
import logging
from unittest import mock

import pytest

from trawlarr.libs.plugins import PluginsHandler
from trawlarr.libs.scheduler import ScheduledTasksManager
from trawlarr.libs.session import Session


class TestPluginInstallTelemetryStubbed:

    def test_notify_site_of_plugin_install_is_noop(self):
        # Construct a bare PluginsHandler — __init__ touches config.Config.
        h = PluginsHandler.__new__(PluginsHandler)
        h.logger = mock.Mock()
        # Patch the Session class globally to catch any accidental usage.
        with mock.patch("trawlarr.libs.plugins.Session") as session_cls:
            assert h.notify_site_of_plugin_install({"plugin_id": "x"}) is None
        # The upstream version constructed a Session and called api_post.
        session_cls.assert_not_called()


class TestSchedulerHeartbeatRemoved:

    def test_run_does_not_schedule_register_unmanic(self):
        """The 60-min `register_unmanic` heartbeat is removed. The fork
        ScheduledTasksManager.run() should set up plugin_repo_update and
        manage_completed_tasks — but no register_unmanic schedule."""
        m = ScheduledTasksManager.__new__(ScheduledTasksManager)
        m.logger = mock.Mock()
        m.event = mock.MagicMock()
        # Make .wait return immediately so we don't loop.
        m.event.wait = mock.Mock(return_value=None)
        m.abort_flag = mock.MagicMock()
        # Set is_set to return True after the first check so the loop exits.
        m.abort_flag.is_set.side_effect = [False, True]
        m.scheduler = mock.MagicMock()
        # Capture every .every(...).<unit>.do(...) chain. Each .do call is
        # what schedules a task.
        scheduled = []
        m.scheduler.every.return_value.minutes.do.side_effect = lambda fn: scheduled.append(("min", fn))
        m.scheduler.every.return_value.hours.do.side_effect = lambda fn: scheduled.append(("hr", fn))
        m.scheduler.every.return_value.seconds.do.side_effect = lambda fn: scheduled.append(("sec", fn))

        with mock.patch.object(m, "manage_completed_tasks"):
            m.run()

        scheduled_names = [fn.__name__ if hasattr(fn, "__name__") else str(fn) for _, fn in scheduled]
        # register_unmanic must not appear.
        assert not any("register_unmanic" in n for n in scheduled_names), (
            f"register_unmanic was scheduled: {scheduled_names}")
        # Sanity: at least one of the kept schedules is present.
        assert any("plugin_repo_update" in n
                   or "manage_completed_tasks" in n
                   for n in scheduled_names), (
            f"expected at least one of the kept schedules; got: {scheduled_names}")


class TestLogForwardingDoesNotCallCentralApi:

    def test_configure_log_forwarding_skips_central_lookup(self, monkeypatch):
        monkeypatch.delenv("TRAWLARR_REMOTE_LOGGING_ENDPOINT", raising=False)
        s = Session.__new__(Session)
        s.logger = logging.getLogger("test")
        s.uuid = "test-uuid"
        s.requests_session = mock.Mock()  # Catch any HTTP calls
        # Settings is touched for log_buffer_retention.
        with mock.patch("trawlarr.libs.session.config.Config") as cfg, \
                mock.patch("trawlarr.libs.session.TrawlarrLogging") as ul:
            cfg.return_value.get_log_buffer_retention.return_value = 50
            # Run with session_valid=True (the path that previously did the
            # central API lookup). With our stub it must skip straight to
            # disable_remote_logging unless the env var is set.
            s._Session__configure_log_forwarding(session_valid=True)
            ul.disable_remote_logging.assert_called_once_with(50)
            ul.enable_remote_logging.assert_not_called()
        # Crucially: no api_get / requests call.
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()

    def test_configure_log_forwarding_honours_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "TRAWLARR_REMOTE_LOGGING_ENDPOINT", "http://my-loki.lan/loki/api/v1/push")
        s = Session.__new__(Session)
        s.logger = logging.getLogger("test")
        s.uuid = "test-uuid"
        s.requests_session = mock.Mock()
        with mock.patch("trawlarr.libs.session.config.Config") as cfg, \
                mock.patch("trawlarr.libs.session.TrawlarrLogging") as ul:
            cfg.return_value.get_log_buffer_retention.return_value = 100
            s._Session__configure_log_forwarding(session_valid=True)
            ul.enable_remote_logging.assert_called_once_with(
                "http://my-loki.lan/loki/api/v1/push", "test-uuid", 100)
            ul.disable_remote_logging.assert_not_called()
        s.requests_session.get.assert_not_called()
        s.requests_session.post.assert_not_called()
