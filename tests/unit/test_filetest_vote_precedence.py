#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_filetest_vote_precedence.py

    Tests for FileTest.should_file_be_added_to_task_list vote precedence.

    Two regressions are pinned here, one in each direction:

    * Upstream broke out of the plugin loop on the first decisive vote, which
      let a content filter (skip_files_matching_ffprobe_data voting False)
      lock out a requester plugin (ensure_2ch_aac_audio voting True) that had
      real follow-up work. Fixed by bec7bd7.

    * bec7bd7's fix made *any* True beat *any* False, which removed the veto
      that the guard plugins at the head of the pipeline depend on
      (limit_library_search_by_file_extension, ignore_completed_tasks). Files
      that completed but never converged were re-queued on every library
      scan. Fixed by issue #32.
"""
import logging

import pytest

from unmanic.libs.filetest import FileTest


def _bare_file_test(plugin_votes):
    """Construct a FileTest without running __init__ — that would build a real
    config, a real PluginsHandler and hit the database.

    `plugin_votes` is an ordered list of (plugin_id, vote, role) tuples, where
    vote is True/False/None and role is None or 'filter'. It stands in for the
    library's configured file-test plugin flow.
    """
    ft = FileTest.__new__(FileTest)
    ft.logger = logging.getLogger("test_filetest")
    ft.library_id = 1
    # Non-empty so file_failed_in_history() short-circuits without touching
    # the history database. The path under test is never in this list.
    ft.failed_paths = ['/some/other/file/that/failed.mkv']
    ft.plugin_modules = [
        {'plugin_id': plugin_id, 'name': plugin_id.replace('_', ' ')}
        for plugin_id, _vote, _role in plugin_votes
    ]

    votes = {plugin_id: (vote, role) for plugin_id, vote, role in plugin_votes}
    executed = []

    def exec_plugin_runner(data, plugin_id, plugin_type):
        executed.append(plugin_id)
        vote, role = votes[plugin_id]
        data['add_file_to_pending_tasks'] = vote
        if role is not None:
            data['file_test_role'] = role
        return True

    ft.plugin_handler = type('FakeHandler', (), {'exec_plugin_runner': staticmethod(exec_plugin_runner)})()
    ft.executed_plugins = executed
    return ft


@pytest.fixture
def path(tmp_path):
    """A path with no sibling .unmanicignore lockfile."""
    return str(tmp_path / "video.mkv")


class TestGuardVeto:
    """Issue #32: a guard plugin's False vote must not be overridable."""

    def test_guard_veto_beats_requester(self, path):
        ft = _bare_file_test([
            ('ignore_completed_tasks', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'ignore_completed_tasks'

    def test_guard_veto_beats_requester_that_voted_first(self, path):
        """Precedence must not depend on the configured plugin order."""
        ft = _bare_file_test([
            ('ensure_2ch_aac_audio', True, None),
            ('limit_library_search_by_file_extension', False, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'limit_library_search_by_file_extension'

    def test_guard_veto_stops_the_pipeline(self, path):
        """Nothing after a veto can change the outcome, so nothing after a
        veto should be run — these are the expensive probing plugins."""
        ft = _bare_file_test([
            ('limit_library_search_by_file_extension', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        ft.should_file_be_added_to_task_list(path)

        assert ft.executed_plugins == ['limit_library_search_by_file_extension']

    def test_unknown_plugin_voting_false_is_a_veto(self, path):
        """Fail safe: a plugin that has not declared itself a filter gets the
        benefit of the doubt, because a wrongly-overridden veto is a silent
        re-queue loop."""
        ft = _bare_file_test([
            ('some_third_party_guard', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'some_third_party_guard'


class TestRequesterOverridesFilter:
    """bec7bd7: a filter plugin must not lock out a requester plugin."""

    def test_requester_beats_known_filter_voting_first(self, path):
        ft = _bare_file_test([
            ('skip_files_matching_ffprobe_data', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'ensure_2ch_aac_audio'

    def test_requester_beats_self_declared_filter(self, path):
        """A plugin can opt into advisory-skip semantics at runtime without
        needing to be in the built-in list."""
        ft = _bare_file_test([
            ('some_third_party_filter', False, 'filter'),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'ensure_2ch_aac_audio'

    def test_declared_role_does_not_leak_to_the_next_plugin(self, path):
        """The role key is per-plugin. A filter declaring itself must not make
        the following guard's veto advisory."""
        ft = _bare_file_test([
            ('some_third_party_filter', False, 'filter'),
            ('ignore_completed_tasks', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'ignore_completed_tasks'

    def test_filter_alone_still_skips_the_file(self, path):
        ft = _bare_file_test([
            ('skip_files_matching_ffprobe_data', False, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'skip_files_matching_ffprobe_data'


class TestNoDecision:

    def test_no_plugin_votes_leaves_the_decision_undecided(self, path):
        ft = _bare_file_test([
            ('some_plugin_with_no_opinion', None, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is None
        assert decision_plugin is None

    def test_first_requester_is_recorded_as_the_decision_plugin(self, path):
        ft = _bare_file_test([
            ('requester_one', True, None),
            ('requester_two', True, None),
        ])

        result, _issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'requester_one'
