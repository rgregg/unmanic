#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_failures_are_not_swallowed.py

    Issue #82. Three places absorbed a failure and carried on, each of them
    turning an unanswered question into a favourable answer:

      1. a file-test plugin that raised had its vote silently discarded, so a
         GUARD plugin - the one whose job is "do not touch this file" - could
         fail and the file would be queued and modified;
      2. sanity.evaluate() returned a PASS when it could not probe the output,
         so the single most damaged file a task can produce was the one case
         that always got through;
      3. PostProcessor.run() logged a stage exception and carried on to the
         stages that record success, so a task whose delivery blew up wrote
         itself into history as a success.

    The policy applied to all three: NOTHING MAY RECORD AN OUTCOME AN EARLIER
    STEP DID NOT ESTABLISH. Where "no answer" has to be resolved to one, it is
    resolved to the answer that does not take an irreversible action.

    The tests below are grouped by the three sites, and each group contains at
    least one test in the false-positive direction - a healthy pipeline that
    must be entirely unaffected - because every one of these changes makes the
    system stop where it used to continue.
"""
import ast
import inspect
import logging
import os
import sys
import textwrap
import threading
from unittest import mock

import pytest

from trawlarr.libs import convergence, sanity, taskfailure
from trawlarr.libs.filetest import (FileTest, FileTestPluginError, ISSUE_PLUGIN_FAILED)
from trawlarr.libs.postprocessor import PostProcessor
from trawlarr.libs.unplugins.executor import PLUGIN_RUNNER_EXCEPTION_KEY, PluginExecutor


# ---------------------------------------------------------------------------
# 0. The executor contract the rest of this rests on
#
# `execute_plugin_runner()` returns False for "no module", "no runner of this
# type" and "the runner raised" alike. The file test read that single bool and
# could not tell a plugin with no opinion from a plugin that broke. The
# exception is now also reported on the caller's `data` dict.
#
# These tests drive the REAL executor against a REAL plugin file on disk,
# because the bug the issue calls out is precisely that the file-test tests
# replaced the executor with a fake that could never fail.
# ---------------------------------------------------------------------------

RAISING_PLUGIN = '''
def on_library_management_file_test(data):
    raise RuntimeError("this guard cannot read its config")
'''

VOTING_PLUGIN = '''
def on_library_management_file_test(data):
    data['add_file_to_pending_tasks'] = False
'''

NO_RUNNER_PLUGIN = '''
def on_worker_process(data):
    pass
'''


@pytest.fixture
def plugins_dir(tmp_path):
    """A plugins directory the real PluginExecutor can load from.

    Plugin modules land in sys.modules under '<plugin_id>.plugin' and the
    directory lands on sys.path; both are undone afterwards so tests cannot
    leak into each other.
    """
    directory = tmp_path / 'plugins'
    directory.mkdir()
    modules_before = set(sys.modules)
    path_before = list(sys.path)
    yield directory
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]
    sys.path[:] = path_before


def _write_plugin(plugins_dir, plugin_id, source):
    plugin_path = plugins_dir / plugin_id
    plugin_path.mkdir()
    (plugin_path / 'plugin.py').write_text(source)
    return plugin_id


@pytest.mark.unittest
class TestTheExecutorReportsWhatThePluginThrew:

    def test_a_raising_runner_tells_its_caller_it_raised(self, plugins_dir):
        """The root of the whole issue. Before this, the ONLY trace of a
        plugin blowing up was a log line; no caller could act on it."""
        _write_plugin(plugins_dir, 'raising_guard', RAISING_PLUGIN)
        executor = PluginExecutor(plugins_directory=str(plugins_dir))
        data = {'path': '/library/video.mkv'}

        result = executor.execute_plugin_runner(data, 'raising_guard', 'library_management.file_test')

        assert result is False
        report = data.get(PLUGIN_RUNNER_EXCEPTION_KEY)
        assert report is not None, (
            "The plugin raised and the caller was told nothing. A guard plugin's "
            "veto disappears and the file gets queued."
        )
        assert report.get('plugin_id') == 'raising_guard'
        assert report.get('exception_type') == 'RuntimeError'
        assert 'cannot read its config' in report.get('exception')

    def test_a_runner_that_completes_reports_nothing(self, plugins_dir):
        """The false-positive direction: a healthy plugin must not look
        broken."""
        _write_plugin(plugins_dir, 'healthy_plugin', VOTING_PLUGIN)
        executor = PluginExecutor(plugins_directory=str(plugins_dir))
        data = {'path': '/library/video.mkv'}

        assert executor.execute_plugin_runner(data, 'healthy_plugin', 'library_management.file_test') is True
        assert PLUGIN_RUNNER_EXCEPTION_KEY not in data
        assert data['add_file_to_pending_tasks'] is False

    def test_a_plugin_without_this_runner_is_not_reported_as_broken(self, plugins_dir):
        """A plugin that simply has no runner of this type returns False too -
        and that must stay distinguishable from a failure, or every library
        would halt on its first unrelated plugin."""
        _write_plugin(plugins_dir, 'worker_only_plugin', NO_RUNNER_PLUGIN)
        executor = PluginExecutor(plugins_directory=str(plugins_dir))
        data = {'path': '/library/video.mkv'}

        assert executor.execute_plugin_runner(data, 'worker_only_plugin', 'library_management.file_test') is False
        assert PLUGIN_RUNNER_EXCEPTION_KEY not in data

    def test_a_previous_plugins_exception_is_not_attributed_to_the_next(self, plugins_dir):
        """The data dict is reused across a plugin flow. A stale report would
        make a healthy plugin look like the broken one."""
        _write_plugin(plugins_dir, 'raising_guard', RAISING_PLUGIN)
        _write_plugin(plugins_dir, 'healthy_plugin', VOTING_PLUGIN)
        executor = PluginExecutor(plugins_directory=str(plugins_dir))
        data = {'path': '/library/video.mkv'}

        executor.execute_plugin_runner(data, 'raising_guard', 'library_management.file_test')
        assert PLUGIN_RUNNER_EXCEPTION_KEY in data

        executor.execute_plugin_runner(data, 'healthy_plugin', 'library_management.file_test')
        assert PLUGIN_RUNNER_EXCEPTION_KEY not in data


# ---------------------------------------------------------------------------
# 1. A file-test plugin that raises no longer loses its vote
# ---------------------------------------------------------------------------

RAISE = object()  #: sentinel: this plugin's runner raises


def _file_test(plugin_votes, handler_raises=()):
    """A FileTest whose plugin flow is `plugin_votes`.

    Deliberately NOT the fake used by test_filetest_vote_precedence.py, whose
    exec_plugin_runner always returned True - that is the blind spot the issue
    names. This one honours the executor contract pinned above: a plugin whose
    vote is RAISE returns False AND leaves an exception report on `data`.

    `handler_raises` names plugins for which exec_plugin_runner itself throws,
    covering the failure modes outside the executor's own try block (a plugin
    module that will not import, a broken plugin type).
    """
    ft = FileTest.__new__(FileTest)
    ft.logger = logging.getLogger('test_failures_are_not_swallowed')
    ft.library_id = 1
    ft.failed_paths = ['/some/other/file/that/failed.mkv']
    ft.plugin_modules = [
        {'plugin_id': plugin_id, 'name': plugin_id.replace('_', ' ')}
        for plugin_id, _vote, _role in plugin_votes
    ]

    votes = {plugin_id: (vote, role) for plugin_id, vote, role in plugin_votes}
    executed = []

    def exec_plugin_runner(data, plugin_id, plugin_type):
        executed.append(plugin_id)
        if plugin_id in handler_raises:
            raise ImportError("cannot import plugin module '{}'".format(plugin_id))
        vote, role = votes[plugin_id]
        if role is not None:
            data['file_test_role'] = role
        if vote is RAISE:
            data[PLUGIN_RUNNER_EXCEPTION_KEY] = {
                'plugin_id':      plugin_id,
                'runner':         'on_library_management_file_test',
                'exception_type': 'RuntimeError',
                'exception':      'the plugin exploded',
            }
            return False
        data['add_file_to_pending_tasks'] = vote
        return True

    ft.plugin_handler = type('FakeHandler', (), {'exec_plugin_runner': staticmethod(exec_plugin_runner)})()
    ft.executed_plugins = executed
    return ft


@pytest.fixture
def path(tmp_path):
    """A path with no sibling .unmanicignore lockfile."""
    return str(tmp_path / "video.mkv")


@pytest.mark.unittest
class TestABrokenFileTestPluginFailsClosed:

    def test_a_raising_guard_does_not_queue_the_file(self, path):
        """The headline. A plugin that says "do not touch this file" broke;
        the file must not be queued on the strength of the plugins that did
        manage to run."""
        ft = _file_test([
            ('some_third_party_guard', RAISE, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False, (
            "A file-test plugin raised and the file was queued anyway. The "
            "plugin that failed may be the guard protecting it."
        )
        assert decision_plugin.get('plugin_id') == 'some_third_party_guard'
        assert [i.get('id') for i in issues] == [ISSUE_PLUGIN_FAILED]
        assert 'some third party guard' in issues[0].get('message')

    def test_the_veto_holds_whatever_the_plugin_order(self, path):
        """A requester that already voted True does not get to win because it
        happened to be configured first."""
        ft = _file_test([
            ('ensure_2ch_aac_audio', True, None),
            ('some_third_party_guard', RAISE, None),
        ])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False

    def test_nothing_after_the_broken_plugin_is_run(self, path):
        """The outcome is decided, and the plugins that follow are the
        expensive probing ones."""
        ft = _file_test([
            ('some_third_party_guard', RAISE, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        ft.should_file_be_added_to_task_list(path)

        assert ft.executed_plugins == ['some_third_party_guard']

    def test_a_handler_that_raises_is_treated_the_same(self, path):
        """The executor catches what the plugin throws, but not what the
        loading machinery around it throws."""
        ft = _file_test([
            ('unimportable_plugin', True, None),
            ('ensure_2ch_aac_audio', True, None),
        ], handler_raises={'unimportable_plugin'})

        result, issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is False
        assert decision_plugin.get('plugin_id') == 'unimportable_plugin'
        assert [i.get('id') for i in issues] == [ISSUE_PLUGIN_FAILED]

    def test_run_file_test_plugins_raises_rather_than_guessing(self, path):
        """The verdict-less outcome is an exception, not a False, because what
        "no verdict" means depends on what the caller does next - and the two
        callers resolve it in OPPOSITE directions."""
        ft = _file_test([('some_third_party_guard', RAISE, None)])

        with pytest.raises(FileTestPluginError) as excinfo:
            ft.run_file_test_plugins(path)

        assert excinfo.value.plugin_id == 'some_third_party_guard'


@pytest.mark.unittest
class TestAHealthyLibraryIsUnaffected:
    """The false-positive direction. This change stops files being queued, so
    the cases that must keep working are worth more than the ones that must
    stop."""

    def test_a_requester_still_queues_the_file(self, path):
        ft = _file_test([
            ('skip_files_matching_ffprobe_data', False, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'ensure_2ch_aac_audio'
        assert issues == []

    def test_a_plugin_with_no_runner_of_this_type_is_still_just_skipped(self, path):
        """exec_plugin_runner returns False for a plugin that has no file-test
        runner. That is not a failure and must not veto anything, or a library
        with one such plugin would queue nothing at all."""
        ft = _file_test([
            ('plugin_with_no_file_test', None, None),
            ('ensure_2ch_aac_audio', True, None),
        ])
        # A None vote returns True from the fake handler; make this one return
        # False with no exception report, exactly like the real executor does.
        ft.plugin_modules[0]['plugin_id'] = 'worker_only_plugin'
        original = ft.plugin_handler.exec_plugin_runner

        def handler(data, plugin_id, plugin_type):
            if plugin_id == 'worker_only_plugin':
                ft.executed_plugins.append(plugin_id)
                return False
            return original(data, plugin_id, plugin_type)

        ft.plugin_handler = type('H', (), {'exec_plugin_runner': staticmethod(handler)})()

        result, issues, _score, _plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert issues == []

    def test_a_broken_content_filter_does_not_halt_the_library(self, path):
        """The one carve-out, and it is provable rather than a judgement call.

        A filter can only ever vote False, and a filter's False is overridable
        by any True. So losing its vote cannot change whether the file is
        queued: with the vote the answer is False, without it and with no
        other vote the answer is None - and neither queues. Nothing
        irreversible follows, so a broken filter is reported and stepped over
        instead of stopping every scan in the library.
        """
        ft = _file_test([
            ('skip_files_matching_ffprobe_data', RAISE, None),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, issues, _score, decision_plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True
        assert decision_plugin.get('plugin_id') == 'ensure_2ch_aac_audio'
        # Reported, not silent.
        assert [i.get('id') for i in issues] == [ISSUE_PLUGIN_FAILED]
        assert ft.executed_plugins == ['skip_files_matching_ffprobe_data', 'ensure_2ch_aac_audio']

    def test_a_broken_filter_alone_still_does_not_queue_the_file(self, path):
        """The other half of the carve-out's proof: with no plugin asking for
        the file, losing the filter's vote leaves it unqueued either way."""
        ft = _file_test([('skip_files_matching_ffprobe_data', RAISE, None)])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(path)

        assert result is not True

    def test_a_self_declared_filter_gets_the_same_treatment(self, path):
        """A plugin that declared itself a filter before it broke is taken at
        its word - the role key is the forward-compatible seam (#16)."""
        ft = _file_test([
            ('some_third_party_filter', RAISE, 'filter'),
            ('ensure_2ch_aac_audio', True, None),
        ])

        result, _issues, _score, _plugin = ft.should_file_be_added_to_task_list(path)

        assert result is True


@pytest.mark.unittest
class TestConvergenceResolvesTheSameFailureTheOtherWay:
    """Same rule, opposite direction, and that is the point.

    The scan path resolves "no verdict" to "do not queue". Here the answer
    that would file the case closed is "converged", so "no verdict" must
    resolve to NOT EVALUATED. Both refuse to record what was not established.
    """

    def test_a_raising_file_test_plugin_never_reports_convergence(self):
        ft = _file_test([('some_third_party_guard', RAISE, None)])

        result = convergence.evaluate_path('/library/video.mkv', 1, file_test=ft)

        assert result.state == convergence.STATE_NOT_EVALUATED, (
            "A broken file-test plugin reported the file as converged. The "
            "task would be filed as fully successful on the strength of a "
            "question that was never answered."
        )
        assert result.converged is False
        assert result.plugin_id == 'some_third_party_guard'

    def test_a_working_file_test_still_reports_convergence(self):
        ft = _file_test([('ensure_2ch_aac_audio', False, None)])

        result = convergence.evaluate_path('/library/video.mkv', 1, file_test=ft)

        assert result.converged is True


# ---------------------------------------------------------------------------
# 2. An output that cannot be probed fails the sanity check
# ---------------------------------------------------------------------------

def _stream(codec_type='video', **kwargs):
    stream = {'codec_type': codec_type}
    stream.update(kwargs)
    return stream


def _probe(streams, filename='/library/episode.mkv'):
    return {'streams': list(streams), 'format': {'filename': filename}}


@pytest.mark.unittest
class TestAnUnprobeableOutputFails:

    def test_an_output_ffprobe_cannot_read_is_a_failure(self):
        """The case the checks exist to catch was the case that passed."""
        result = sanity.evaluate(_probe([_stream(), _stream('audio')]), None, 1000, 900)

        assert result.checked is True
        assert result.failed is True
        assert [f['id'] for f in result.failures] == [sanity.CHECK_UNPROBEABLE_OUTPUT]

    def test_an_output_with_no_streams_at_all_is_a_failure(self):
        """A probe that came back empty is no better than no probe."""
        result = sanity.evaluate(_probe([_stream()]), _probe([]), 1000, 900)

        assert result.failed is True
        assert result.failures[0]['id'] == sanity.CHECK_UNPROBEABLE_OUTPUT

    def test_a_missing_output_file_does_not_pass_silently(self):
        """No probe and no size - previously reported as 'could not check'
        and waved through, which is how a task that produced nothing at all
        was recorded as a success."""
        result = sanity.evaluate(_probe([_stream()]), None, 1000, None)

        assert result.checked is True
        assert result.failed is True
        assert result.failures[0]['id'] == sanity.CHECK_UNPROBEABLE_OUTPUT

    def test_no_ffprobe_at_all_is_still_not_a_failure(self):
        """The false-positive direction, and the reason the check is phrased
        as 'the INPUT probed and the OUTPUT did not'. An install with no
        ffprobe, or an unreadable mount, fails both files identically and
        must keep degrading to the size comparison instead of failing every
        task in the library."""
        result = sanity.evaluate(None, None, 1000, 900)

        assert result.failed is False
        assert result.checked is True

    def test_no_probes_and_no_sizes_is_still_merely_unchecked(self):
        result = sanity.evaluate(None, None, None, None)

        assert result.checked is False
        assert result.failed is False

    def test_an_unprobeable_input_alone_is_not_the_outputs_fault(self):
        """A source that cannot be probed but an output that can is odd, but
        it is not evidence that the output is damaged."""
        result = sanity.evaluate(None, _probe([_stream()]), 1000, 900)

        assert result.failed is False

    def test_a_healthy_pair_of_probes_still_passes(self):
        result = sanity.evaluate(_probe([_stream(), _stream('audio')]),
                                 _probe([_stream(), _stream('audio')]), 1000, 800)

        assert result.checked is True
        assert result.failed is False


# ---------------------------------------------------------------------------
# 2b. ... and the post-processor acts on it
# ---------------------------------------------------------------------------

def _sanity_postprocessor(cache_path, source_abspath, dest_path):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger('test_failures_are_not_swallowed')
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False
    pp.event = mock.Mock()

    class _Task:
        success = True

    class _Settings:
        def get_cache_path(self):
            return os.path.dirname(cache_path)

    class _CurrentTask:
        task = _Task()
        command_log = []

        def get_task_id(self_inner):
            return 1

        def get_task_library_id(self_inner):
            return 1

        def get_task_type(self_inner):
            return "local"

        def get_task_success(self_inner):
            return self_inner.task.success

        def get_cache_path(self_inner):
            return cache_path

        def get_source_data(self_inner):
            return {"abspath": source_abspath}

        def get_destination_data(self_inner):
            return {"abspath": dest_path}

        def get_start_time(self_inner):
            return 0

        def get_finish_time(self_inner):
            return 1

        def set_success(self_inner, success):
            self_inner.task.success = bool(success)

        def save_command_log(self_inner, log):
            self_inner.command_log.append(log)

    pp.settings = _Settings()
    pp.current_task = _CurrentTask()
    return pp


@pytest.fixture
def library_task(tmp_path):
    cache = tmp_path / "cache" / "task" / "out.mkv"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"transcoded output")
    source = tmp_path / "library" / "episode.mkv"
    source.parent.mkdir()
    source.write_bytes(b"original")
    return cache, source, tmp_path / "library" / "episode.mkv"


def _run_post_process(pp, probes):
    plugin_handler = mock.Mock()
    plugin_handler.get_enabled_plugin_modules_by_type.return_value = []
    with mock.patch("trawlarr.libs.postprocessor.PluginsHandler", return_value=plugin_handler), \
            mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"), \
            mock.patch("trawlarr.libs.sanity.probe_file",
                       side_effect=lambda p: probes.get(os.path.abspath(p))), \
            mock.patch("trawlarr.libs.sanity.probe_from_task_data_store", return_value=None), \
            mock.patch("trawlarr.libs.sanity.load_state", return_value={}), \
            mock.patch("trawlarr.libs.sanity.save_state", return_value=True), \
            mock.patch("trawlarr.libs.taskfailure.record"):
        pp.post_process_file()


@pytest.mark.unittest
class TestTheDamagedOutputIsNeverDelivered:

    def test_an_unreadable_output_does_not_replace_the_library_file(self, library_task):
        cache, source, dest = library_task
        pp = _sanity_postprocessor(str(cache), str(source), str(dest))

        _run_post_process(pp, {os.path.abspath(str(source)): _probe([_stream(), _stream('audio')])})

        assert source.read_bytes() == b"original", (
            "An output ffprobe could not read was written over the library file."
        )
        assert pp.current_task.task.success is False
        assert pp._last_destination_files == []
        assert sanity.CHECK_UNPROBEABLE_OUTPUT in pp.current_task.command_log[0]

    def test_a_readable_output_is_delivered_exactly_as_before(self, library_task):
        cache, source, dest = library_task
        pp = _sanity_postprocessor(str(cache), str(source), str(dest))

        _run_post_process(pp, {
            os.path.abspath(str(source)): _probe([_stream(), _stream('audio')]),
            os.path.abspath(str(cache)):  _probe([_stream(), _stream('audio')]),
        })

        assert dest.read_bytes() == b"transcoded output"
        assert pp.current_task.task.success is True


# ---------------------------------------------------------------------------
# 3. A post-processing stage that raises marks the task failed
# ---------------------------------------------------------------------------

class _BoundedEvent:
    """threading.Event whose wait() bounds the loop under test.

    run() is a `while True`. If the wiring under test is deleted nothing else
    stops it, so the loop is bounded from the outside and a disconnected call
    site fails an assertion instead of hanging.
    """

    def __init__(self, stop_after, stop_flag):
        self.waits = 0
        self.stop_after = stop_after
        self.stop_flag = stop_flag

    def wait(self, _timeout=None):
        self.waits += 1
        if self.waits >= self.stop_after:
            self.stop_flag.set()
        return True

    def is_set(self):
        return False

    def set(self):
        pass

    def clear(self):
        pass


class _RunLoopTask:
    """Minimal stand-in for the task run() pulls off the processed queue."""

    def __init__(self, task_type='local'):
        self.task_type = task_type
        self.success = True
        self.status = 'processed'
        self.deleted = False

    def get_task_id(self):
        return 7

    def get_task_library_id(self):
        return 1

    def get_task_type(self):
        return self.task_type

    def get_task_success(self):
        return self.success

    def set_success(self, success):
        self.success = bool(success)

    def set_status(self, status):
        self.status = status

    def get_source_abspath(self):
        return '/library/episode.mkv'

    def get_source_data(self):
        return {'abspath': '/library/episode.mkv'}

    def get_destination_data(self):
        return {'abspath': '/library/episode.mkv'}

    def get_cache_path(self):
        return '/tmp/cache/episode.mkv'

    def delete(self):
        self.deleted = True


class _OneTaskQueue:
    """Delivers exactly one processed task, then reports itself empty."""

    def __init__(self, task):
        self.task = task
        self.handed_out = False

    def task_list_processed_is_empty(self):
        return self.handed_out

    def get_next_processed_tasks(self):
        self.handed_out = True
        return self.task


def _drive_one_task(task, monkeypatch, stages):
    """Run one full pass of PostProcessor.run() over `task`.

    `stages` maps stage method names to callables, so a test can make exactly
    one of them raise and watch what the rest of the loop then does.
    """
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger('test_failures_are_not_swallowed')
    pp.abort_flag = threading.Event()
    pp.event = _BoundedEvent(stop_after=25, stop_flag=pp.abort_flag)
    pp.task_queue = _OneTaskQueue(task)
    pp.current_task = None
    pp._last_destination_files = []
    pp._last_file_move_processes_success = False
    pp._last_output_probe = None
    pp._last_output_size = None
    pp.system_configuration_is_valid = lambda: True

    called = []

    def _stage(name, default=None):
        def run_stage():
            called.append(name)
            handler = stages.get(name)
            if handler is not None:
                return handler()
            return default

        return run_stage

    for name in ('post_process_file', 'post_process_remote_file', 'write_history_log',
                 'dump_history_log', 'record_completed_file', 'run_convergence_check',
                 'commit_task_metadata'):
        setattr(pp, name, _stage(name))

    monkeypatch.setattr('trawlarr.libs.postprocessor.PluginsHandler', lambda *a, **kw: mock.Mock())

    recorded = []
    monkeypatch.setattr('trawlarr.libs.postprocessor.taskfailure.record',
                        lambda task_id, category, message, **kw: recorded.append((task_id, category, message)))

    pp.run()
    return pp, called, recorded


@pytest.mark.unittest
class TestARaisingDeliveryStageFailsTheTask:

    def test_the_task_is_marked_failed(self, monkeypatch):
        """Before this, a task whose delivery blew up half way through went on
        to write itself into history as a success."""
        task = _RunLoopTask()

        def boom():
            raise OSError("read-only file system")

        pp, called, _recorded = _drive_one_task(task, monkeypatch, {'post_process_file': boom})

        assert 'post_process_file' in called, "The loop never ran"
        assert task.success is False, (
            "Post-processing raised and the task is still recorded as successful. "
            "It will be written to history as a success it did not earn."
        )

    def test_the_reason_is_recorded_under_a_known_category(self, monkeypatch):
        task = _RunLoopTask()

        def boom():
            raise OSError("read-only file system")

        _pp, _called, recorded = _drive_one_task(task, monkeypatch, {'post_process_file': boom})

        assert len(recorded) == 1
        task_id, category, message = recorded[0]
        assert task_id == 7
        assert category == taskfailure.CATEGORY_POSTPROCESSOR_ERROR
        assert category in taskfailure.KNOWN_CATEGORIES
        assert 'read-only file system' in message

    def test_the_task_is_still_recorded_and_removed_from_the_queue(self, monkeypatch):
        """The stages after the failure are not 'more work on a failed task',
        they are how the failure gets recorded and how the task leaves the
        'processed' state. Skipping them would make the post-processor pick
        the same task up again forever."""
        task = _RunLoopTask()

        def boom():
            raise OSError("read-only file system")

        _pp, called, _recorded = _drive_one_task(task, monkeypatch, {'post_process_file': boom})

        assert 'write_history_log' in called
        assert task.deleted is True

    def test_the_failure_is_marked_before_history_is_written(self, monkeypatch):
        """Order is the whole mechanism: write_history_log() reads the success
        flag, and record_completed_file() and run_convergence_check() both
        return early for an unsuccessful task."""
        task = _RunLoopTask()
        success_seen = {}

        def boom():
            raise OSError("read-only file system")

        _pp, _called, _recorded = _drive_one_task(task, monkeypatch, {
            'post_process_file':  boom,
            'write_history_log':  lambda: success_seen.setdefault('history', task.get_task_success()),
        })

        assert success_seen.get('history') is False

    def test_a_raising_remote_delivery_is_treated_the_same(self, monkeypatch):
        """The remote branch hands 'task_success' back to the installation
        that asked for the work. It must not claim a delivery that raised."""
        task = _RunLoopTask(task_type='remote')

        def boom():
            raise OSError("the link went away")

        _pp, called, recorded = _drive_one_task(task, monkeypatch, {'post_process_remote_file': boom})

        assert 'post_process_remote_file' in called
        assert task.success is False
        assert recorded and recorded[0][1] == taskfailure.CATEGORY_POSTPROCESSOR_ERROR
        # Still leaves the processed queue, or it would be picked up forever.
        assert task.status == 'complete'

    def test_a_healthy_task_is_untouched(self, monkeypatch):
        """The false-positive direction: nothing raised, so nothing changes."""
        task = _RunLoopTask()

        pp, called, recorded = _drive_one_task(task, monkeypatch, {})

        assert task.success is True
        assert recorded == []
        assert called[:4] == ['post_process_file', 'write_history_log',
                              'record_completed_file', 'run_convergence_check']
        assert task.deleted is True

    def test_a_bookkeeping_stage_failure_does_not_blacklist_a_delivered_file(self, monkeypatch):
        """The deliberate limit of the policy.

        The stages AFTER delivery are still logged and stepped over. Marking a
        correctly delivered file's task as failed would blacklist a good file
        from every future scan via FileTest.file_failed_in_history(), which is
        a worse outcome than the missing bookkeeping row. The rule is not "any
        exception fails the task", it is "nothing may record an outcome an
        earlier stage did not establish".
        """
        task = _RunLoopTask()

        def boom():
            raise RuntimeError("the done-state table is locked")

        _pp, called, recorded = _drive_one_task(task, monkeypatch, {'record_completed_file': boom})

        assert task.success is True
        assert recorded == []
        assert 'run_convergence_check' in called, "The loop stopped at the failed stage"
        assert task.deleted is True


@pytest.mark.unittest
class TestTheWiringIsPinnedAtItsCallSite:
    """Both delivery handlers must actually call the helper.

    Deleting either call leaves every behavioural test above still passing
    only because they drive run() - which is the point of driving run(). This
    reads the AST as a second, cheaper guard, and specifically checks the call
    is inside an exception handler rather than anywhere in the method.
    """

    def _handlers_calling(self, name):
        tree = ast.parse(textwrap.dedent(inspect.getsource(PostProcessor.run)))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                        and child.func.attr == name:
                    found.append(child)
        return found

    def test_run_marks_the_task_failed_from_its_exception_handlers(self):
        calls = self._handlers_calling('mark_task_failed_in_post_processing')
        assert len(calls) == 2, (
            "PostProcessor.run() must mark the task failed from BOTH delivery "
            "exception handlers (local and remote). Found {}.".format(len(calls))
        )

    def test_the_stages_named_are_the_delivery_stages(self):
        stages = set()
        for call in self._handlers_calling('mark_task_failed_in_post_processing'):
            if call.args and isinstance(call.args[0], ast.Constant):
                stages.add(call.args[0].value)
        assert stages == {'post_process_file', 'post_process_remote_file'}
