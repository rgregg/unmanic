#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_worker_stall_detector.py

    The worker stall detector kills a subprocess that has stopped showing any
    sign of life and records the task as stalled (#15).

    The dangerous failure mode for this feature is not "missed a stall", it is
    "killed a healthy four hour 4K encode". So the bulk of these tests are
    false-positive tests: a command that produces no output for far longer than
    the stall timeout must survive as long as it is still burning CPU, still
    writing to disk, still emitting the occasional line, or paused.

    Simulated time is used throughout (workers.time.time is patched) so the
    tests exercise the real threshold arithmetic without sleeping.
"""
import logging
import threading
import time
from unittest import mock

import psutil
import pytest

from trawlarr.config import (
    DEFAULT_WORKER_STALL_TIMEOUT,
    MINIMUM_WORKER_STALL_TIMEOUT,
    Config,
)
from trawlarr.libs.task import TaskDataStore
from trawlarr.libs.workers import (
    TASK_FAILURE_STATE_KEY,
    Worker,
    WorkerSubprocessMonitor,
)

STALL_TIMEOUT = 300


class FakeClock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds


class FakeProc:
    """Stands in for a psutil.Process in the tracked process tree."""

    def __init__(self, pid=4242, cpu_available=True, io_available=True):
        self.pid = pid
        self.cpu_seconds = 0.0
        self.io_bytes = 0
        self.cpu_available = cpu_available
        self.io_available = io_available

    def cpu_times(self):
        if not self.cpu_available:
            raise psutil.AccessDenied(self.pid)
        return mock.Mock(user=self.cpu_seconds, system=0.0)

    def io_counters(self):
        if not self.io_available:
            raise NotImplementedError('io_counters is not available on this platform')
        return mock.Mock(read_bytes=self.io_bytes, write_bytes=0)


def _bare_monitor(clock, enabled=True, timeout=STALL_TIMEOUT):
    """Construct a WorkerSubprocessMonitor without running __init__ (which
    needs a real parent Worker). Only the attributes the stall detector
    touches are set up."""
    monitor = WorkerSubprocessMonitor.__new__(WorkerSubprocessMonitor)
    monitor.logger = logging.getLogger('test_worker_stall_detector')
    monitor.parent_worker = mock.Mock()
    monitor.event = threading.Event()
    monitor.redundant_flag = threading.Event()
    monitor.paused_flag = threading.Event()
    monitor.paused = False
    monitor.subprocess_pid = 4242
    monitor.subprocess = mock.Mock()
    monitor.subprocess_percent = 0
    monitor._terminate_lock = threading.Lock()
    monitor.terminate_proc = mock.Mock()
    monitor.stall_detection_enabled = enabled
    monitor.stall_timeout = timeout
    monitor.last_activity_at = clock()
    monitor._last_cpu_seconds = None
    monitor._last_io_bytes = None
    monitor._stall_warning_logged = False
    return monitor


def _check(monitor, procs):
    return monitor._WorkerSubprocessMonitor__check_for_stall(procs)


def _run_for(monitor, procs, clock, duration, step=10, on_tick=None):
    """Advance the simulated clock in `step` second increments, running the
    detector on each tick as the real monitor loop does. Returns True if the
    detector killed the subprocess at any point."""
    killed = False
    elapsed = 0
    while elapsed < duration:
        clock.tick(step)
        elapsed += step
        if on_tick is not None:
            on_tick()
        if _check(monitor, procs):
            killed = True
    return killed


class TestHealthyCommandsAreNeverKilled:
    """The expensive mistake. Each of these commands is silent for well over
    the stall timeout but is plainly still working."""

    def test_silent_encode_burning_cpu_survives(self):
        """A 4K HEVC encode whose plugin reports no progress at all, but which
        is pinning a core. One hour of simulated silence."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()

            def burn_cpu():
                proc.cpu_seconds += 9.5

            killed = _run_for(monitor, [proc], clock, duration=3600, on_tick=burn_cpu)

        assert not killed, "A CPU-busy subprocess was killed as stalled"
        monitor.terminate_proc.assert_not_called()
        monitor.parent_worker.report_subprocess_stalled.assert_not_called()

    def test_silent_command_writing_to_disk_survives(self):
        """An I/O bound command (a slow remux onto a network share) that uses
        almost no CPU and prints nothing, but is moving bytes."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()

            def write_bytes():
                proc.io_bytes += 4096

            killed = _run_for(monitor, [proc], clock, duration=3600, on_tick=write_bytes)

        assert not killed, "A subprocess still performing disk I/O was killed as stalled"
        monitor.terminate_proc.assert_not_called()

    def test_command_emitting_output_survives(self):
        """No CPU or I/O counters move (they are unreadable here), but the
        command keeps emitting log lines, which the worker reports through
        note_activity()."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()

            killed = _run_for(monitor, [proc], clock, duration=3600,
                              on_tick=monitor.note_activity)

        assert not killed, "A subprocess still producing output was killed as stalled"

    def test_paused_worker_subprocess_survives(self):
        """A paused worker SIGSTOPs its subprocesses. Of course they stop
        making progress - that must not be read as a stall, and the stall
        clock must not have quietly run down while paused either."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            monitor.paused_flag.set()
            monitor.paused = True
            proc = FakeProc()

            killed = _run_for(monitor, [proc], clock, duration=3600)

            assert not killed, "A paused worker's subprocess was killed as stalled"

            # And on resume the detector starts counting from the resume, not
            # from the last activity before the pause.
            monitor.paused_flag.clear()
            monitor.paused = False
            resumed_killed = _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT - 20)

        assert not resumed_killed, "The stall clock kept running while the worker was paused"

    def test_process_with_no_readable_counters_survives(self):
        """If neither CPU time nor I/O counters can be read, the detector has
        no evidence at all. It must not kill on a guess."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc(cpu_available=False, io_available=False)

            killed = _run_for(monitor, [proc], clock, duration=3600)

        assert not killed, "A subprocess with unreadable counters was killed as stalled"

    def test_detector_disabled_never_kills(self):
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock, enabled=False)
            proc = FakeProc()

            killed = _run_for(monitor, [proc], clock, duration=3600)

        assert not killed, "The stall detector acted while disabled"

    def test_worker_shutting_down_is_not_reported_as_a_stall(self):
        """When the worker is being torn down the subprocess is killed by the
        redundant-flag path. Reporting that as a task stall would be a lie."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            monitor.redundant_flag.set()
            proc = FakeProc()

            killed = _run_for(monitor, [proc], clock, duration=3600)

        assert not killed
        monitor.parent_worker.report_subprocess_stalled.assert_not_called()


class TestWedgedCommandIsKilled:

    def test_idle_subprocess_is_terminated_after_the_threshold(self):
        """The NFS hang: no output, no CPU time, no I/O."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()

            # Just under the threshold: still alive.
            killed_early = _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT - 20)
            assert not killed_early, "The subprocess was killed before the stall timeout elapsed"
            monitor.terminate_proc.assert_not_called()

            # Cross the threshold.
            killed = _run_for(monitor, [proc], clock, duration=60)

        assert killed, "A wedged subprocess was never killed"
        monitor.terminate_proc.assert_called_once()

    def test_stall_is_reported_to_the_parent_worker_before_the_kill(self):
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()
            _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT + 60)

        monitor.parent_worker.report_subprocess_stalled.assert_called_once()
        reason = monitor.parent_worker.report_subprocess_stalled.call_args.args[0]
        # The reason has to say what was observed, not just "stalled".
        assert 'stall' in reason.lower()
        assert str(monitor.subprocess_pid) in reason
        assert 'CPU' in reason
        assert 'I/O' in reason

    def test_a_kill_still_happens_if_reporting_the_stall_raises(self):
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            monitor.parent_worker.report_subprocess_stalled.side_effect = RuntimeError('boom')
            proc = FakeProc()
            killed = _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT + 60)

        assert killed
        monitor.terminate_proc.assert_called_once()

    def test_a_warning_is_logged_before_the_kill(self, caplog):
        """Loud beats tidy: an operator should be able to see the stall
        building in the logs before the detector acts."""
        clock = FakeClock()
        with caplog.at_level(logging.WARNING, logger='test_worker_stall_detector'):
            with mock.patch('trawlarr.libs.workers.time.time', clock):
                monitor = _bare_monitor(clock)
                proc = FakeProc()
                killed = _run_for(monitor, [proc], clock, duration=(STALL_TIMEOUT // 2) + 20)

        assert not killed, "The detector killed at half the threshold"
        assert any('no output' in record.message.lower() or 'no output' in record.getMessage().lower()
                   for record in caplog.records), \
            "No early warning was logged as the subprocess went quiet"

    def test_activity_after_a_long_silence_resets_the_clock(self):
        """A command that goes quiet for almost the whole timeout and then
        speaks up must get a full fresh timeout, not be killed moments later."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            proc = FakeProc()
            _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT - 20)
            monitor.note_activity()
            killed = _run_for(monitor, [proc], clock, duration=STALL_TIMEOUT - 20)

        assert not killed, "The stall clock was not reset by fresh activity"


class TestTreeActivitySampling:

    def test_child_process_activity_counts_as_the_tree_being_alive(self):
        """The tracked PID is often a lightweight wrapper. Work done by a
        descendant must count."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            parent = FakeProc(pid=1)
            child = FakeProc(pid=2)

            def child_works():
                child.cpu_seconds += 9.5

            killed = _run_for(monitor, [parent, child], clock, duration=3600, on_tick=child_works)

        assert not killed, "Work done by a child process was not counted"

    def test_sampling_reports_none_when_a_counter_is_unavailable(self):
        monitor = _bare_monitor(FakeClock())
        cpu, io_bytes = monitor._sample_tree_activity([FakeProc(io_available=False)])
        assert cpu == 0.0
        assert io_bytes is None

        cpu, io_bytes = monitor._sample_tree_activity([FakeProc(cpu_available=False)])
        assert cpu is None
        assert io_bytes == 0


class TestProgressParserRecordsActivity:

    def test_default_progress_parser_notes_activity(self):
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            monitor.last_activity_at = clock() - 1000
            monitor.default_progress_parser('55')
            assert monitor.last_activity_at == clock()

    def test_unparseable_progress_line_still_counts_as_activity(self):
        """Plenty of ffmpeg output does not parse into a percentage. It is
        still proof the command is alive."""
        clock = FakeClock()
        with mock.patch('trawlarr.libs.workers.time.time', clock):
            monitor = _bare_monitor(clock)
            monitor.last_activity_at = clock() - 1000
            monitor.default_progress_parser('frame= 1234 fps=30 q=28.0 size=  102400kB')
            assert monitor.last_activity_at == clock()


def _bare_worker(task_id=4242):
    worker = Worker.__new__(Worker)
    # Worker.name is a threading.Thread property that needs a real
    # Thread.__init__; the stall path only reads it for log messages.
    object.__setattr__(worker, '_name', 'Worker-Test')
    worker._initialized = True
    worker.logger = logging.getLogger('test_worker_stall_detector')
    worker.worker_log = []
    worker.current_task = mock.Mock()
    worker.current_task.get_task_id.return_value = task_id
    return worker


class TestWorkerRecordsTheStall:

    def test_stall_is_written_into_the_worker_log(self):
        worker = _bare_worker()
        worker.report_subprocess_stalled('subprocess PID 1 produced no output for 300 seconds')
        joined = ''.join(worker.worker_log)
        assert 'TASK FAILED [STALLED]' in joined, \
            "A stalled task left no visible marker in the log the user reads"
        assert 'no output' in joined

    def test_stall_is_recorded_in_the_task_data_store(self):
        task_id = 987654
        TaskDataStore.clear_task(task_id)
        worker = _bare_worker(task_id=task_id)
        worker.report_subprocess_stalled('wedged')
        record = TaskDataStore.get_task_state(TASK_FAILURE_STATE_KEY, task_id=task_id)
        TaskDataStore.clear_task(task_id)
        assert record is not None, "No durable failure record was left for the stalled task"
        # 'stalled' is distinct from a plain plugin failure.
        assert record['category'] == 'stalled'
        assert record['message'] == 'wedged'
        assert record['timestamp'] > 0

    def test_reporting_a_stall_never_raises_into_the_monitor_thread(self):
        """report_subprocess_stalled runs on the monitor thread. An exception
        there would take the monitor down with it."""
        worker = _bare_worker()
        worker.current_task.get_task_id.side_effect = RuntimeError('task went away')
        worker.report_subprocess_stalled('wedged')
        assert 'TASK FAILED [STALLED]' in ''.join(worker.worker_log)


class TestStallTimeoutConfig:

    @staticmethod
    def _bare_config(value):
        cfg = Config.__new__(Config)
        cfg.worker_stall_timeout = value
        return cfg

    def test_configured_value_is_used(self):
        assert self._bare_config(900).get_worker_stall_timeout() == 900

    @pytest.mark.parametrize('value', [0, 1, 5, -30])
    def test_hair_trigger_values_are_clamped(self, value):
        """A two second threshold would kill every healthy transcode on the
        box. Clamp rather than trust."""
        assert self._bare_config(value).get_worker_stall_timeout() == MINIMUM_WORKER_STALL_TIMEOUT

    @pytest.mark.parametrize('value', [None, 'soon', ''])
    def test_nonsense_values_fall_back_to_the_default(self, value):
        assert self._bare_config(value).get_worker_stall_timeout() == DEFAULT_WORKER_STALL_TIMEOUT

    def test_string_numbers_from_the_environment_are_accepted(self):
        assert self._bare_config('600').get_worker_stall_timeout() == 600

    @pytest.mark.parametrize('value,expected', [
        (True, True), (False, False),
        ('true', True), ('True', True), ('1', True), ('on', True),
        ('false', False), ('no', False), ('', False),
    ])
    def test_enabled_flag_accepts_environment_style_strings(self, value, expected):
        cfg = Config.__new__(Config)
        cfg.set_worker_stall_detection_enabled(value)
        assert cfg.get_worker_stall_detection_enabled() is expected


class TestDetectorIsArmedByTheRealMonitor:
    """The unit tests above hand-build the monitor state. This one checks the
    real set_proc/unset_proc lifecycle arms and disarms the detector, so the
    tests above are testing a state the application actually produces."""

    def test_set_proc_arms_the_detector_and_unset_disarms_it(self):
        monitor = _bare_monitor(FakeClock())
        monitor.stall_detection_enabled = False
        monitor.stall_timeout = None
        monitor.subprocess_pid = None
        monitor.last_activity_at = None
        monitor._tracked_processes_by_pid = {}
        monitor.subprocess_start_time = 0
        monitor.subprocess_pause_time = 0
        monitor.subprocess_elapsed = 0

        settings = mock.Mock()
        settings.get_worker_stall_detection_enabled.return_value = True
        settings.get_worker_stall_timeout.return_value = 450

        with mock.patch('trawlarr.libs.workers.psutil.Process', return_value=mock.Mock()), \
                mock.patch('trawlarr.libs.workers.config.Config', return_value=settings):
            monitor.set_proc(9999)

        assert monitor.stall_detection_enabled is True
        assert monitor.stall_timeout == 450
        assert monitor.last_activity_at is not None

        monitor.unset_proc()
        assert monitor.stall_detection_enabled is False, \
            "The detector stayed armed with no subprocess to watch"

    def test_unreadable_settings_leave_the_detector_disabled(self):
        """Fail safe: if the settings cannot be read, do not start killing
        subprocesses on a default guess."""
        monitor = _bare_monitor(FakeClock())
        monitor.stall_detection_enabled = False
        monitor.subprocess_pid = None
        monitor._tracked_processes_by_pid = {}
        monitor.subprocess_start_time = 0
        monitor.subprocess_pause_time = 0
        monitor.subprocess_elapsed = 0

        with mock.patch('trawlarr.libs.workers.psutil.Process', return_value=mock.Mock()), \
                mock.patch('trawlarr.libs.workers.config.Config', side_effect=RuntimeError('no config')):
            monitor.set_proc(9999)

        assert monitor.stall_detection_enabled is False
        assert monitor.stall_timeout is None


class TestWorkerReportsOutputAsActivity:
    """The per-line heartbeat has to actually be wired into the exec loop."""

    def test_exec_loop_notes_activity_for_each_line_of_output(self):
        worker = Worker.__new__(Worker)
        worker.logger = logging.getLogger('test_worker_stall_detector')
        worker.worker_log = []
        worker.event = threading.Event()
        worker.redundant_flag = threading.Event()
        worker.paused_flag = threading.Event()
        worker.worker_subprocess_monitor = mock.Mock()

        proc = mock.Mock()
        proc.pid = 12345
        proc.stdout.readline.side_effect = ['frame=1\n', 'frame=2\n', '']
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.communicate.return_value = ('', '')

        with mock.patch('trawlarr.libs.workers.subprocess.Popen', return_value=proc), \
                mock.patch('trawlarr.libs.workers.psutil'), \
                mock.patch('trawlarr.libs.workers.os') as fake_os:
            fake_os.name = 'posix'
            fake_os.getpid.return_value = 1
            worker._Worker__exec_command_subprocess({'exec_command': ['ffmpeg', '-i', 'in.mkv']})

        assert worker.worker_subprocess_monitor.note_activity.call_count == 2, (
            "The worker did not report command output to the stall detector; a chatty "
            "command would eventually be killed as stalled")


def test_time_is_not_actually_slept_by_these_tests():
    """Guard against someone converting the simulated clock back into real
    sleeps and making the suite take an hour."""
    started = time.time()
    clock = FakeClock()
    with mock.patch('trawlarr.libs.workers.time.time', clock):
        monitor = _bare_monitor(clock)
        _run_for(monitor, [FakeProc()], clock, duration=3600)
    assert (time.time() - started) < 5
