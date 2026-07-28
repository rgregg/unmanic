#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_scheduler_other_tasks.py

    Coverage for ScheduledTasksManager methods other than
    manage_completed_tasks (which has its own file). Both are thin
    delegations to a singleton.

    Methods covered:
    - register_unmanic: delegates to Session().register_unmanic(force=True)
    - plugin_repo_update: delegates to PluginsHandler().update_plugin_repos()
"""
import logging
from unittest import mock

from unmanic.libs.scheduler import ScheduledTasksManager


def _bare_manager():
    m = ScheduledTasksManager.__new__(ScheduledTasksManager)
    m.logger = logging.getLogger("test_scheduler_other")
    return m


class TestSimpleDelegations:

    def test_register_unmanic_delegates_with_force(self):
        m = _bare_manager()
        with mock.patch("unmanic.libs.scheduler.Session") as SessionCls:
            m.register_unmanic()
        SessionCls.assert_called_once_with()
        SessionCls.return_value.register_unmanic.assert_called_once_with(force=True)

    def test_plugin_repo_update_delegates(self):
        m = _bare_manager()
        with mock.patch("unmanic.libs.scheduler.PluginsHandler") as Handler:
            m.plugin_repo_update()
        Handler.assert_called_once_with()
        Handler.return_value.update_plugin_repos.assert_called_once_with()
