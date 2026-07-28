#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_supporter_gates_removed.py

    Regression tests for the local fork's "Remove supporter-level feature
    gates" patch. These pin the gate functions to "always allow" so a
    future rebase that accidentally re-introduces upstream's
    `s.level <= 1` / `s.level > 1` checks fails CI immediately.
"""
from unittest import mock

from trawlarr.libs.library import Library


class TestLibraryCountLimits:

    def test_within_library_count_limits_always_returns_true(self):
        # Upstream gated this on `s.level <= 1` and capped at 2 libraries.
        # Local fork: returns True regardless. Patch FrontendPushMessages
        # because it touches a singleton at module scope.
        with mock.patch("trawlarr.libs.library.FrontendPushMessages") as fpm:
            assert Library.within_library_count_limits() is True
            # Must not consult Session at all — register_unmanic / level
            # checks should be entirely gone.
            fpm.return_value.remove_item.assert_called_once_with(
                'libraryEnabledLimits')

    def test_within_library_count_limits_does_not_call_session(self):
        # If a future rebase re-introduces s.register_unmanic() at the top
        # of this function, this test should fail because Session would
        # be touched. Patch the module reference rather than the import,
        # because the upstream version did `from ... import Session`
        # inside the function body.
        with mock.patch("trawlarr.libs.library.FrontendPushMessages"), \
                mock.patch("trawlarr.libs.session.Session") as sess:
            Library.within_library_count_limits()
        sess.assert_not_called()


class TestPluginSettingsReqLevGateRemoved:

    def test_save_plugin_settings_no_longer_consults_req_lev(self):
        # Upstream consulted each setting's `req_lev` metadata against
        # session.level and silently reset values the user wasn't entitled
        # to. Local fork: no req_lev gating. Verify save_plugin_settings
        # writes whatever value the caller supplies, regardless of meta.
        from trawlarr.libs.unplugins.executor import PluginExecutor
        executor = PluginExecutor.__new__(PluginExecutor)
        executor.logger = mock.Mock()

        plugin_module = mock.Mock()
        plugin_settings = mock.Mock()
        plugin_settings.set_setting.return_value = True
        plugin_settings.get_form_settings.return_value = {
            # A setting with req_lev=2 — upstream would have reset to default
            # if session.level was < 2. We're verifying the value passes through.
            "supporter_only_setting": {"req_lev": 2, "label": "Premium"},
            "regular_setting":        {"req_lev": 0, "label": "Free"},
        }
        plugin_module.Settings.return_value = plugin_settings

        with mock.patch.object(executor, "_PluginExecutor__get_plugin_directory",
                               return_value="/tmp/fakepath"), \
                mock.patch.object(executor, "_PluginExecutor__load_plugin_module",
                                  return_value=plugin_module), \
                mock.patch.object(executor, "reload_plugin_module"):
            settings = {"supporter_only_setting": "user_value", "regular_setting": "x"}
            assert executor.save_plugin_settings("any_plugin", settings) is True

        # Critical: the user-supplied value must reach set_setting unchanged.
        # Upstream would have replaced "user_value" with the plugin's default
        # when session.level < 2.
        plugin_settings.set_setting.assert_any_call("supporter_only_setting", "user_value")
        plugin_settings.set_setting.assert_any_call("regular_setting", "x")
        # And get_default_setting must not have been touched.
        plugin_settings.get_default_setting.assert_not_called()
