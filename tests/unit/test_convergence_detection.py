#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_convergence_detection.py

    Issue #34: a task completes, and nothing ever checks whether the file it
    produced still matches the criteria that queued it. Files "landed in
    completed-task history [...] with no signal from the queue that anything
    was wrong".

    Invariants covered:

    1. The seam is the PLUGIN tier, not the full file test. #33 records a
       completed file as done, so a convergence check built on
       should_file_be_added_to_task_list() would report every file converged
       and would be an always-green check.
    2. A file the plugins no longer want is converged, quietly, and any
       previous record of it is cleared.
    3. A file the plugins still want is recorded, named with the plugin that
       still wants it, and counted.
    4. Nothing is re-queued and the task is not marked failed.
    5. The done-state from #33 still stands for a non-converged file - the
       whole point is that it is reported, not looped.
    6. A library with no file-test plugins has no criteria to converge
       against and produces no finding either way.
    7. The occurrence count advances across tasks and escalates at the repeat
       limit; a converged run removes the row entirely.
    8. The output probe from #35 is reused when it still describes the
       delivered file, and refused when it does not.
    9. The extraction of run_file_test_plugins() left the scan path's
       behaviour byte-for-byte identical (#32 precedence, #33 tier 0).
"""
import logging

import pytest

from trawlarr.libs import convergence
from trawlarr.libs.filetest import FileTest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def convergence_db():
    """Bind FileConvergenceState to a throwaway in-memory database."""
    from peewee import SqliteDatabase
    from trawlarr.libs.unmodels import FileConvergenceState

    database = SqliteDatabase(':memory:')
    with database.bind_ctx([FileConvergenceState]):
        database.create_tables([FileConvergenceState])
        yield database
    database.close()


@pytest.fixture
def both_state_dbs():
    """Both #33's done-state and #34's convergence state, so the interaction
    between them can be asserted."""
    from peewee import SqliteDatabase
    from trawlarr.libs.unmodels import FileCompletionState, FileConvergenceState

    database = SqliteDatabase(':memory:')
    models = [FileCompletionState, FileConvergenceState]
    with database.bind_ctx(models):
        database.create_tables(models)
        yield database
    database.close()


def _bare_file_test(plugin_votes, allowlist=(), ignore_completed_files=False):
    """A FileTest without __init__ - that would build a real config, a real
    PluginsHandler and hit the database. Mirrors the helper in
    tests/unit/test_native_done_state.py.
    """
    ft = FileTest.__new__(FileTest)
    ft.logger = logging.getLogger("test_convergence_detection")
    ft.library_id = 1
    # Non-empty so file_failed_in_history() does not go to the database.
    ft.failed_paths = ['/some/other/file/that/failed.mkv']
    ft.file_extension_allowlist = tuple(allowlist)
    ft.ignore_completed_files = ignore_completed_files
    ft.plugin_modules = [
        {'plugin_id': plugin_id, 'name': plugin_id.replace('_', ' ')}
        for plugin_id, _vote in plugin_votes
    ]

    votes = dict(plugin_votes)
    executed = []
    seen_shared_info = []

    def exec_plugin_runner(data, plugin_id, plugin_type):
        executed.append(plugin_id)
        seen_shared_info.append(dict(data.get('shared_info') or {}))
        data['add_file_to_pending_tasks'] = votes[plugin_id]
        return True

    ft.plugin_handler = type('FakeHandler', (), {'exec_plugin_runner': staticmethod(exec_plugin_runner)})()
    ft.executed_plugins = executed
    ft.seen_shared_info = seen_shared_info
    return ft


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x" * 1024)
    return str(path)


# ---------------------------------------------------------------------------
# 1. The seam
# ---------------------------------------------------------------------------

class TestTheSeamIsThePluginTier:

    def test_the_full_file_test_would_always_report_converged(self, both_state_dbs, video):
        """The trap this design exists to avoid. Once #33 has recorded the
        delivered file as done, the FULL file test answers 'do not queue' no
        matter what the plugins think - so a convergence check built on it
        would be green for every file forever."""
        from trawlarr.libs import donestate
        donestate.record_completion(video, library_id=1, task_id=7)

        ft = _bare_file_test([('video_transcoder', True)])
        should_queue, issues, _priority, _plugin = ft.should_file_be_added_to_task_list(video)

        assert should_queue is False
        assert 'alreadycompleted' in [i.get('id') for i in issues]
        # And the plugins were never even consulted.
        assert ft.executed_plugins == []

    def test_the_plugin_tier_still_answers_honestly_for_a_completed_file(self, both_state_dbs, video):
        """Same file, same recorded done-state, asked at the right seam."""
        from trawlarr.libs import donestate
        donestate.record_completion(video, library_id=1, task_id=7)

        ft = _bare_file_test([('video_transcoder', True)])
        result = convergence.evaluate_path(video, 1, file_test=ft)

        assert result.state == convergence.STATE_NOT_CONVERGED
        assert result.plugin_id == 'video_transcoder'


# ---------------------------------------------------------------------------
# 2/3. Evaluating a delivered file
# ---------------------------------------------------------------------------

class TestEvaluatePath:

    def test_a_file_the_plugins_no_longer_want_has_converged(self, video):
        ft = _bare_file_test([('video_transcoder', False)])
        result = convergence.evaluate_path(video, 1, file_test=ft)
        assert result.state == convergence.STATE_CONVERGED
        assert result.converged is True

    def test_a_file_the_plugins_still_want_has_not_converged(self, video):
        ft = _bare_file_test([('video_transcoder', True)])
        result = convergence.evaluate_path(video, 1, file_test=ft)
        assert result.state == convergence.STATE_NOT_CONVERGED
        assert result.plugin_id == 'video_transcoder'
        assert video in result.message
        assert 'video transcoder' in result.message

    def test_no_plugins_means_no_criteria_and_no_verdict(self, video):
        """A library with nothing to say about its files has nothing to
        converge against. Reporting 'converged' here would be inventing a
        clean bill of health that was never earned."""
        ft = _bare_file_test([])
        result = convergence.evaluate_path(video, 1, file_test=ft)
        assert result.state == convergence.STATE_NOT_EVALUATED
        assert result.converged is False
        assert result.not_converged is False

    def test_a_guard_veto_reads_as_converged(self, video):
        """A guard plugin saying 'do not touch this file' is a legitimate
        reason for the file not to qualify. It is not a finding."""
        ft = _bare_file_test([('some_guard', False), ('video_transcoder', True)])
        result = convergence.evaluate_path(video, 1, file_test=ft)
        assert result.state == convergence.STATE_CONVERGED

    def test_a_raising_file_test_never_reports_convergence(self, video):
        class _Exploding:
            plugin_modules = [{'plugin_id': 'x', 'name': 'x'}]

            def run_file_test_plugins(self, *args, **kwargs):
                raise RuntimeError("probe blew up")

        result = convergence.evaluate_path(video, 1, file_test=_Exploding())
        assert result.state == convergence.STATE_NOT_EVALUATED

    def test_no_path_is_not_evaluated(self):
        assert convergence.evaluate_path('', 1).state == convergence.STATE_NOT_EVALUATED

    def test_shared_info_is_offered_to_the_plugins(self, video):
        ft = _bare_file_test([('video_transcoder', True)])
        convergence.evaluate_path(video, 1, shared_info={'ffprobe': {'streams': []}}, file_test=ft)
        assert ft.seen_shared_info[0] == {'ffprobe': {'streams': []}}


# ---------------------------------------------------------------------------
# 7. The record and its count
# ---------------------------------------------------------------------------

class TestRecording:

    def _not_converged(self):
        return convergence.ConvergenceResult(
            convergence.STATE_NOT_CONVERGED, plugin_id='video_transcoder',
            plugin_name='Video Transcoder', message='still wants it')

    def test_a_first_occurrence_is_recorded(self, convergence_db):
        assert convergence.record('/lib/a.mkv', library_id=2, task_id=9, result=self._not_converged()) == 1
        outstanding = convergence.list_outstanding()
        assert len(outstanding) == 1
        assert outstanding[0]['abspath'] == '/lib/a.mkv'
        assert outstanding[0]['plugin_id'] == 'video_transcoder'
        assert outstanding[0]['occurrences'] == 1
        assert outstanding[0]['repeated'] is False

    def test_the_count_advances_across_tasks(self, convergence_db):
        convergence.record('/lib/a.mkv', library_id=2, task_id=9, result=self._not_converged())
        assert convergence.record('/lib/a.mkv', library_id=2, task_id=10, result=self._not_converged()) == 2
        outstanding = convergence.list_outstanding()
        assert len(outstanding) == 1
        assert outstanding[0]['occurrences'] == 2
        assert outstanding[0]['repeated'] is True

    def test_convergence_clears_the_record(self, convergence_db):
        convergence.record('/lib/a.mkv', result=self._not_converged())
        assert convergence.clear('/lib/a.mkv') == 1
        assert convergence.list_outstanding() == []
        assert convergence.outstanding_summary()['total'] == 0

    def test_clearing_an_unknown_path_is_harmless(self, convergence_db):
        assert convergence.clear('/lib/never-seen.mkv') == 0

    def test_dismissal_hides_but_does_not_delete(self, convergence_db):
        convergence.record('/lib/a.mkv', result=self._not_converged())
        record_id = convergence.list_outstanding()[0]['id']
        assert convergence.set_dismissed([record_id]) == 1
        assert convergence.list_outstanding() == []
        assert len(convergence.list_outstanding(include_dismissed=True)) == 1

    def test_a_new_occurrence_undismisses_and_keeps_counting(self, convergence_db):
        convergence.record('/lib/a.mkv', result=self._not_converged())
        convergence.set_dismissed([convergence.list_outstanding()[0]['id']])
        assert convergence.record('/lib/a.mkv', result=self._not_converged()) == 2
        outstanding = convergence.list_outstanding()
        assert len(outstanding) == 1
        assert outstanding[0]['occurrences'] == 2
        assert outstanding[0]['dismissed'] is False

    def test_the_summary_groups_by_plugin(self, convergence_db):
        convergence.record('/lib/a.mkv', result=self._not_converged())
        convergence.record('/lib/b.mkv', result=self._not_converged())
        convergence.record('/lib/b.mkv', result=self._not_converged())
        summary = convergence.outstanding_summary()
        assert summary['total'] == 2
        assert summary['repeated'] == 1
        assert summary['plugins'] == {'video_transcoder': 2}
        assert summary['repeat_limit'] == convergence.REPEAT_LIMIT

    def test_no_database_bound_degrades_quietly(self):
        assert convergence.record('/lib/a.mkv', result=self._not_converged()) == 0
        assert convergence.list_outstanding() == []
        assert convergence.outstanding_summary()['total'] == 0
        assert convergence.clear('/lib/a.mkv') == 0
        assert convergence.set_dismissed([1]) == 0


# ---------------------------------------------------------------------------
# 4/5/8. The post-processor hook
# ---------------------------------------------------------------------------

def _build_postprocessor(destination_files, success=True, enabled=True,
                         output_probe=None, output_size=None, library_id=3):
    from trawlarr.libs.postprocessor import PostProcessor

    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.convergence")
    pp._last_destination_files = list(destination_files)
    pp._last_file_move_processes_success = True
    pp._last_output_probe = output_probe
    pp._last_output_size = output_size
    pp.settings = type('S', (), {'get_convergence_check_enabled': staticmethod(lambda: enabled)})()

    class _CurrentTask:
        def get_task_id(self_inner):
            return 42

        def get_task_library_id(self_inner):
            return library_id

        def get_task_success(self_inner):
            return success

    pp.current_task = _CurrentTask()
    return pp


@pytest.fixture
def patched_file_test(monkeypatch):
    """Replace the FileTest the post-processor builds with a scripted one."""
    holder = {}

    def install(plugin_votes):
        ft = _bare_file_test(plugin_votes)
        holder['ft'] = ft
        monkeypatch.setattr('trawlarr.libs.filetest.FileTest', lambda library_id: ft)
        return ft

    return install


class TestPostProcessorConvergenceCheck:

    def test_a_non_converged_output_is_recorded(self, convergence_db, patched_file_test, video):
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video])
        evaluated = pp.run_convergence_check()

        assert [r.state for _p, r in evaluated] == [convergence.STATE_NOT_CONVERGED]
        outstanding = convergence.list_outstanding()
        assert [o['abspath'] for o in outstanding] == [video]
        assert outstanding[0]['task_id'] == 42
        assert outstanding[0]['library_id'] == 3

    def test_a_converged_output_records_nothing(self, convergence_db, patched_file_test, video):
        patched_file_test([('video_transcoder', False)])
        pp = _build_postprocessor([video])
        evaluated = pp.run_convergence_check()

        assert [r.state for _p, r in evaluated] == [convergence.STATE_CONVERGED]
        assert convergence.list_outstanding() == []

    def test_a_converged_output_clears_an_earlier_finding(self, convergence_db, patched_file_test, video):
        convergence.record(video, result=convergence.ConvergenceResult(
            convergence.STATE_NOT_CONVERGED, plugin_id='video_transcoder'))
        patched_file_test([('video_transcoder', False)])
        pp = _build_postprocessor([video])
        pp.run_convergence_check()
        assert convergence.list_outstanding() == []

    def test_the_file_is_not_re_queued(self, convergence_db, patched_file_test, video, monkeypatch):
        """The whole point. A non-converged file must be reported, never fed
        back into the queue - that is the reprocess loop."""
        from trawlarr.libs import donestate

        queued = []
        monkeypatch.setattr('trawlarr.libs.donestate.forget_path',
                            lambda path: queued.append(path))
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video])
        pp.run_convergence_check()

        # Nothing asked for the done-state to be dropped, which is the only
        # supported way back into the queue for a completed file (#33/#41).
        assert queued == []
        assert donestate.forget_path is not None

    def test_the_done_state_from_issue_33_still_stands(self, both_state_dbs, patched_file_test, video):
        from trawlarr.libs import donestate
        donestate.record_completion(video, library_id=3, task_id=42)

        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video])
        pp.run_convergence_check()

        already_done, _message = donestate.file_is_already_completed(video)
        assert already_done is True

    def test_a_failed_task_is_not_evaluated(self, convergence_db, patched_file_test, video):
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video], success=False)
        assert pp.run_convergence_check() == []
        assert convergence.list_outstanding() == []

    def test_the_check_can_be_switched_off(self, convergence_db, patched_file_test, video):
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video], enabled=False)
        assert pp.run_convergence_check() == []
        assert convergence.list_outstanding() == []

    def test_a_task_that_delivered_nothing_is_not_evaluated(self, convergence_db, patched_file_test):
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([])
        assert pp.run_convergence_check() == []

    def test_every_delivered_copy_is_evaluated(self, convergence_db, patched_file_test, tmp_path):
        first = tmp_path / "a.mkv"
        second = tmp_path / "b.mkv"
        first.write_bytes(b"x")
        second.write_bytes(b"y")
        patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([str(first), str(second)])
        pp.run_convergence_check()
        assert sorted(o['abspath'] for o in convergence.list_outstanding()) == [str(first), str(second)]


class TestProbeReuse:

    def test_the_sanity_probe_is_handed_on_when_it_still_describes_the_file(
            self, convergence_db, patched_file_test, video):
        probe = {'streams': [{'codec_type': 'video'}], 'format': {'filename': '/cache/x.mkv'}}
        ft = patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video], output_probe=probe, output_size=1024)
        pp.run_convergence_check()

        assert ft.seen_shared_info[0]['ffprobe']['streams'] == probe['streams']
        # ...retargeted at the delivered file, not the cache file that is gone.
        assert ft.seen_shared_info[0]['ffprobe']['format']['filename'] == video
        # ...and the caller's probe was not mutated.
        assert probe['format']['filename'] == '/cache/x.mkv'

    def test_a_probe_of_a_different_size_file_is_refused(self, convergence_db, patched_file_test, video):
        """A postprocessor.file_move plugin may transform the file on its way
        into the library. Handing a plugin the wrong file's probe is how a
        check turns into a rubber stamp."""
        probe = {'streams': [{'codec_type': 'video'}], 'format': {'filename': '/cache/x.mkv'}}
        ft = patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video], output_probe=probe, output_size=999999)
        pp.run_convergence_check()
        assert ft.seen_shared_info[0] == {}

    def test_no_probe_available_is_fine(self, convergence_db, patched_file_test, video):
        ft = patched_file_test([('video_transcoder', True)])
        pp = _build_postprocessor([video])
        pp.run_convergence_check()
        assert ft.seen_shared_info[0] == {}


class TestSanityCarriesItsOutputProbe:

    def test_check_task_output_returns_the_probe_it_paid_for(self, monkeypatch, tmp_path):
        """The reuse in #34 depends on #35 handing its probe back."""
        from trawlarr.libs import sanity

        source = tmp_path / "in.mkv"
        output = tmp_path / "out.mkv"
        source.write_bytes(b"a" * 100)
        output.write_bytes(b"b" * 100)
        output_probe = {'streams': [{'codec_type': 'video'}], 'format': {'filename': str(output)}}

        def fake_probe(path):
            if path == str(output):
                return output_probe
            return {'streams': [{'codec_type': 'video'}], 'format': {'filename': str(source)}}

        monkeypatch.setattr(sanity, 'probe_file', fake_probe)
        monkeypatch.setattr(sanity, 'load_state', lambda path: {})
        result = sanity.check_task_output(str(source), str(output))
        assert result.output_probe is output_probe


# ---------------------------------------------------------------------------
# 9. The extraction did not change the scan path
# ---------------------------------------------------------------------------

class TestScanPathIsUnchanged:

    def test_tier_zero_still_runs_before_the_plugins(self, both_state_dbs, video):
        from trawlarr.libs import donestate
        donestate.record_completion(video, library_id=1, task_id=1)
        ft = _bare_file_test([('video_transcoder', True)])
        should_queue, _issues, _priority, _plugin = ft.should_file_be_added_to_task_list(video)
        assert should_queue is False

    def test_a_guard_veto_still_beats_a_true_vote(self, video):
        ft = _bare_file_test([('a_guard', False), ('video_transcoder', True)])
        should_queue, _issues, _priority, plugin = ft.should_file_be_added_to_task_list(video)
        assert should_queue is False
        assert plugin['plugin_id'] == 'a_guard'
        # And the veto short-circuits the remaining plugins.
        assert ft.executed_plugins == ['a_guard']

    def test_an_advisory_skip_is_still_overridable(self, video):
        ft = _bare_file_test([('skip_files_matching_ffprobe_data', False), ('video_transcoder', True)])
        should_queue, _issues, _priority, plugin = ft.should_file_be_added_to_task_list(video)
        assert should_queue is True
        assert plugin['plugin_id'] == 'video_transcoder'

    def test_no_plugin_votes_leaves_the_answer_undecided(self, video):
        ft = _bare_file_test([])
        should_queue, _issues, priority, plugin = ft.should_file_be_added_to_task_list(video)
        assert should_queue is None
        assert priority == 0
        assert plugin is None
