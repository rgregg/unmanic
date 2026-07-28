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
    manage_completed_tasks (which has its own file). Most are thin
    delegations to a singleton; the worker-count balancing method has
    real branching logic worth pinning.

    Methods covered:
    - register_unmanic: delegates to Session().register_unmanic(force=True)
    - plugin_repo_update: delegates to PluginsHandler().update_plugin_repos()
    - update_remote_installation_links: delegates to Links().update_all_...
    - set_worker_count_based_on_remote_installation_links: branching logic
"""
import logging
import time
from unittest import mock

import pytest

from unmanic.libs.scheduler import ScheduledTasksManager


def _bare_manager():
    m = ScheduledTasksManager.__new__(ScheduledTasksManager)
    m.logger = logging.getLogger("test_scheduler_other")
    m.force_local_worker_timer = 0
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

    def test_update_remote_installation_links_delegates(self):
        m = _bare_manager()
        with mock.patch("unmanic.libs.scheduler.Links") as LinksCls:
            m.update_remote_installation_links()
        LinksCls.assert_called_once_with()
        LinksCls.return_value.update_all_remote_installation_links.assert_called_once_with()


class TestSetWorkerCountBalancing:
    """The interesting one: balances worker count across this
    installation and any linked remotes that have
    enable_distributed_worker_count=True."""

    def _setup(self, m, *, local_task_count, target_count, linked_installations, force_local_worker_timer=0):
        """Patch in the dependencies the method touches."""
        settings = mock.Mock()
        settings.get_distributed_worker_count_target.return_value = target_count
        settings.get_remote_installations.return_value = linked_installations
        settings.set_config_item = mock.Mock()
        m.force_local_worker_timer = force_local_worker_timer

        task_handler = mock.Mock()
        task_handler.get_total_task_list_count.return_value = local_task_count

        cfg_patch = mock.patch("unmanic.libs.scheduler.config.Config",
                                return_value=settings)
        task_patch = mock.patch("unmanic.libs.scheduler.task.Task",
                                return_value=task_handler)
        return cfg_patch, task_patch, settings

    def test_no_distributed_links_returns_without_setting_workers(self):
        """If no linked installation enables distributed worker count, the
        method must not modify number_of_workers."""
        m = _bare_manager()
        cfg, tsk, settings = self._setup(
            m, local_task_count=10, target_count=4, linked_installations=[
                {"enable_distributed_worker_count": False, "task_count": 5},
            ])
        with cfg, tsk:
            m.set_worker_count_based_on_remote_installation_links()
        settings.set_config_item.assert_not_called()

    def test_no_links_at_all_returns_without_setting_workers(self):
        m = _bare_manager()
        cfg, tsk, settings = self._setup(
            m, local_task_count=10, target_count=4, linked_installations=[])
        with cfg, tsk:
            m.set_worker_count_based_on_remote_installation_links()
        settings.set_config_item.assert_not_called()

    def test_balances_workers_proportionally(self):
        """One linked remote with 8 pending tasks, this installation has 4
        pending tasks, target=3. Local share = round(4/12 * 3) = 1."""
        m = _bare_manager()
        cfg, tsk, settings = self._setup(
            m, local_task_count=4, target_count=3, linked_installations=[
                {"enable_distributed_worker_count": True, "task_count": 8},
            ])
        with cfg, tsk:
            m.set_worker_count_based_on_remote_installation_links()
        settings.set_config_item.assert_called_once_with(
            "number_of_workers", 1, save_settings=True)

    def test_zero_local_tasks_gets_zero_workers(self):
        """No local pending tasks → zero workers, even if remote has work."""
        m = _bare_manager()
        cfg, tsk, settings = self._setup(
            m, local_task_count=0, target_count=4, linked_installations=[
                {"enable_distributed_worker_count": True, "task_count": 10},
            ])
        with cfg, tsk:
            m.set_worker_count_based_on_remote_installation_links()
        settings.set_config_item.assert_called_once_with(
            "number_of_workers", 0, save_settings=True)

    def test_force_local_worker_when_timer_expired_and_local_has_tasks(self):
        """Every ~10-12 min, if this installation has >1 pending tasks
        but the proportional allocation gave it 0 workers, force at least
        1 worker so the local queue doesn't sit idle."""
        m = _bare_manager()
        # Heavy remote workload makes local share round to 0.
        cfg, tsk, settings = self._setup(
            m, local_task_count=2, target_count=2, linked_installations=[
                {"enable_distributed_worker_count": True, "task_count": 100},
            ],
            # Set timer way in the past so the rate-limit gate is open.
            force_local_worker_timer=time.time() - 10000,
        )
        with cfg, tsk:
            m.set_worker_count_based_on_remote_installation_links()
        # Without the force, local would be 0. Force bumps to 1.
        settings.set_config_item.assert_called_once()
        args = settings.set_config_item.call_args.args
        assert args[0] == "number_of_workers"
        assert args[1] >= 1
