#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Names that still say "unmanic" ON PURPOSE, pinned so a future sweep
# cannot quietly "finish the rename" and destroy user data.
#
# The rename (#49) left a handful of legacy names in place deliberately,
# and FORK.md's "What deliberately still says unmanic" section explains
# why. The problem with a prose list is that it does not fail a build.
# Someone greps for `unmanic`, sees a leftover, renames it, all the tests
# pass, and the damage only shows up in a user's library weeks later.
#
# The per-directory marker file is the dangerous one. DirectoryInfo
# writes `.unmanic` into every processed directory and reads it back to
# decide what has already been handled. Renaming it does not lose the
# files -- it loses the *knowledge*, silently: every previously processed
# directory looks untouched, and an entire library gets reprocessed. That
# is the exact pathology this milestone exists to end, and it would be
# introduced by a change that looks like tidying.
#
# So: if you are here because this test failed, you are probably about to
# do something destructive. Read FORK.md first.

import inspect
import os

import pytest

from trawlarr.libs import runtimepaths
from trawlarr.libs.directoryinfo import TrawlarrDirectoryInfo


@pytest.mark.unittest
class TestTheDirectoryMarkerFileNameIsFrozen:
    """`.unmanic` per-directory markers exist in users' libraries already."""

    def test_the_marker_file_is_still_called_dot_unmanic(self, tmp_path):
        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert os.path.basename(info.path) == '.unmanic', (
            "The per-directory marker file name is load-bearing: it exists in "
            "users' libraries today. Renaming it makes every previously "
            "processed directory look untouched and silently reprocesses the "
            "whole library. See FORK.md."
        )

    def test_the_marker_is_not_derived_from_the_app_directory_name(self, tmp_path):
        """Guards against the plausible-looking 'fix' of routing this through
        runtimepaths, which is what would couple it to the next rename."""
        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert os.path.basename(info.path) != runtimepaths.APP_DIR_NAME

    def test_an_existing_marker_written_before_the_rename_is_still_read(self, tmp_path):
        """The compatibility that actually matters: a marker on disk from an
        Unmanic-era install must still be found."""
        marker = tmp_path / '.unmanic'
        marker.write_text('{"episode.mkv": {"example_plugin": "done"}}')
        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert info.get('episode.mkv', 'example_plugin') == 'done'


@pytest.mark.unittest
class TestPathDefaultsFollowTheAppDirectory:
    """The opposite invariant: anything that IS a runtime path must track
    runtimepaths, not a literal. Two separate bugs of this shape shipped --
    PluginExecutor and the plugin CLI both defaulted to ~/.unmanic/plugins
    while the application installed to ~/.trawlarr/plugins."""

    @pytest.mark.parametrize('module_name', [
        'trawlarr.libs.unplugins.executor',
        'trawlarr.libs.unplugins.pluginscli',
    ])
    def test_no_module_hardcodes_the_legacy_app_directory(self, module_name):
        import importlib
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        offenders = [
            line.strip() for line in source.splitlines()
            if "'{}'".format(runtimepaths.LEGACY_APP_DIR_NAME) in line
            and not line.strip().startswith('#')
        ]
        assert offenders == [], (
            "{} hardcodes the legacy app directory in code: {}. Use "
            "runtimepaths.APP_DIR_NAME so the next rename cannot "
            "desynchronise them.".format(module_name, offenders)
        )
