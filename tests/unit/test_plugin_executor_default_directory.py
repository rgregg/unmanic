#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# PluginExecutor's default plugins directory must be the one the
# application actually installs plugins into.
#
# The rename (#49) moved the application directory from ~/.unmanic to
# ~/.trawlarr, but PluginExecutor.__init__ built its fallback from a
# hardcoded '.unmanic' string literal. Ten call sites construct
# PluginExecutor() with no argument, so every one of them resolved to a
# directory the application never writes to. Nothing failed loudly: the
# executor simply found no plugins there.
#
# It survived the rename audits because the literal is inside a default
# argument rather than in the config module, and it survived the test
# suite because the tests either pass an explicit directory or run with
# no plugins installed, where both paths are equally empty.
#
# The invariant is not "the string says trawlarr" -- that would go stale
# the same way. It is that the executor's default and Config agree.

import os

import pytest

from trawlarr import config as config_module
from trawlarr.config import Config
from trawlarr.libs import runtimepaths
from trawlarr.libs.unplugins.executor import PluginExecutor


@pytest.mark.unittest
class TestPluginExecutorDefaultDirectory:

    def test_the_default_matches_where_the_application_installs_plugins(self, tmp_path, monkeypatch):
        """The bug this file exists for: these two disagreed.

        Config is a singleton, so an earlier test in the suite may already
        have built one against a different HOME. Drop it from the registry
        so this assertion measures the patched environment rather than
        whatever ran first, and drop it again afterwards so the instance
        built here does not leak into later tests.
        """
        monkeypatch.setenv('HOME', str(tmp_path))
        registry = type(config_module.Config)._instances
        registry.pop(config_module.Config, None)
        try:
            assert PluginExecutor().plugins_directory == Config().get_plugins_path()
        finally:
            registry.pop(config_module.Config, None)

    def test_the_default_is_under_the_current_app_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path))
        directory = PluginExecutor().plugins_directory
        assert runtimepaths.APP_DIR_NAME in directory.split(os.sep)
        assert runtimepaths.LEGACY_APP_DIR_NAME not in directory.split(os.sep)

    def test_an_explicit_directory_is_still_honoured(self, tmp_path):
        explicit = str(tmp_path / 'somewhere-else')
        assert PluginExecutor(plugins_directory=explicit).plugins_directory == explicit
