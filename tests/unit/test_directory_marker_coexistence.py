#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.
#
# Trawlarr and Unmanic must be able to run over the same library.
#
# Both applications record what they have already done to each file in a
# per-directory marker file. Upstream writes `.unmanic`. If Trawlarr wrote
# the same file, the two would corrupt each other: `save()` serialises the
# whole document, so whichever application saved last would silently drop
# every entry the other had added. Nothing would error, and the damage
# would surface later as work being redone or skipped for no visible
# reason.
#
# So Trawlarr writes `.trawlarr` and NEVER writes `.unmanic`.
#
# The legacy file is still READ, once, when a directory has no `.trawlarr`
# marker: an installation migrating away from Unmanic keeps its processing
# history instead of reprocessing the entire library. After the first
# save the two files are independent, and the legacy one is left untouched
# for whoever else may still be using it.

import json
import os

import pytest

from trawlarr.libs import directoryinfo
from trawlarr.libs.directoryinfo import TrawlarrDirectoryInfo


def _write(path, data):
    with open(path, 'w') as handle:
        json.dump(data, handle)


@pytest.mark.unittest
class TestTrawlarrWritesItsOwnMarker:

    def test_the_marker_written_is_dot_trawlarr(self, tmp_path):
        info = TrawlarrDirectoryInfo(str(tmp_path))
        info.set('episode.mkv', 'example_plugin', 'done')
        info.save()

        assert (tmp_path / '.trawlarr').exists()
        assert not (tmp_path / '.unmanic').exists()

    def test_saving_never_writes_the_legacy_marker(self, tmp_path):
        """The corruption case: an Unmanic install is using this directory."""
        legacy = tmp_path / '.unmanic'
        _write(str(legacy), {'episode.mkv': {'unmanic_plugin': 'done'}})
        legacy_before = legacy.read_text()

        info = TrawlarrDirectoryInfo(str(tmp_path))
        info.set('episode.mkv', 'trawlarr_plugin', 'done')
        info.save()

        assert legacy.read_text() == legacy_before, (
            "Trawlarr rewrote Unmanic's marker file. save() serialises the "
            "whole document, so this silently destroys the other "
            "application's record of what it has processed."
        )
        assert (tmp_path / '.trawlarr').exists()


@pytest.mark.unittest
class TestLegacyMarkersAreInheritedOnce:

    def test_history_is_read_from_a_legacy_marker_when_ours_is_absent(self, tmp_path):
        """Migration: an Unmanic-era library must not be reprocessed wholesale."""
        _write(str(tmp_path / '.unmanic'), {'episode.mkv': {'example_plugin': 'done'}})

        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert info.get('episode.mkv', 'example_plugin') == 'done'

    def test_our_marker_wins_outright_when_both_exist(self, tmp_path):
        """No merging. After the first save the two files are independent and
        cannot drift into one another."""
        _write(str(tmp_path / '.unmanic'), {'episode.mkv': {'example_plugin': 'stale'}})
        _write(str(tmp_path / '.trawlarr'), {'episode.mkv': {'example_plugin': 'current'}})

        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert info.get('episode.mkv', 'example_plugin') == 'current'

    def test_inherited_history_is_persisted_to_our_own_marker_on_save(self, tmp_path):
        """The inheritance is one-time. Once saved, later changes to the
        legacy file must not affect us."""
        _write(str(tmp_path / '.unmanic'), {'episode.mkv': {'example_plugin': 'done'}})

        info = TrawlarrDirectoryInfo(str(tmp_path))
        info.set('episode.mkv', 'another_plugin', 'done')
        info.save()

        reopened = TrawlarrDirectoryInfo(str(tmp_path))
        assert reopened.source_path == str(tmp_path / '.trawlarr')
        assert reopened.get('episode.mkv', 'example_plugin') == 'done'
        assert reopened.get('episode.mkv', 'another_plugin') == 'done'

    def test_a_legacy_ini_marker_is_still_migrated(self, tmp_path):
        """Upstream's oldest format, reached through the legacy path."""
        (tmp_path / '.unmanic').write_text('[episode.mkv]\nexample_plugin = done\n')

        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert info.get('episode.mkv', 'example_plugin') == 'done'


@pytest.mark.unittest
class TestMarkerNamesAreDistinct:

    def test_the_two_marker_names_are_not_the_same_file(self):
        assert directoryinfo.MARKER_FILE_NAME != directoryinfo.LEGACY_MARKER_FILE_NAME

    def test_a_fresh_directory_reads_as_empty_without_creating_anything(self, tmp_path):
        info = TrawlarrDirectoryInfo(str(tmp_path))
        assert info.get('episode.mkv', 'example_plugin') is None
        assert os.listdir(str(tmp_path)) == []
