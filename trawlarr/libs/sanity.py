#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.sanity.py

    Structural sanity checks on a task's output, compared against its input.
    See issue #35.

    THE INCIDENT
    ------------
    A plugin bug appended an extra AAC stereo track to a file on every library
    scan. Every task "succeeded". One episode accumulated twenty-five identical
    stereo tracks before a human noticed by hand. Nothing in the pipeline ever
    looked at what came out and asked whether it was plausible.

    WHAT IS CHECKED
    ---------------
    Four cheap assertions, run in the post-processor after the worker has
    finished but before the output is allowed anywhere near the library:

      unprobeable_output - the input probed cleanly and the output did not.
                         Added by issue #82: this used to be treated as "the
                         check could not run" and passed, so the most damaged
                         output a task can produce was the one that always got
                         through. See evaluate() for why the check is phrased
                         relative to the input.
      duplicate_audio  - the output has MORE audio streams than the input, and
                         more copies of some comparable audio track (same
                         channel count, language and title) than the input had.
      stream_growth    - the output has more streams than the input, and so did
                         the previous N tasks on this same file.
      size_growth      - the output is materially larger than the input, and so
                         it was for the previous N tasks on this same file.

    WHY THE CHECKS ARE SHAPED THIS WAY
    ----------------------------------
    A single task adding a stream is legitimate and common - adding a stereo
    downmix, burning in a subtitle track, adding chapters. Banning growth
    outright would flag those on their first, correct run, and a check that
    cries wolf gets disabled and then protects nothing.

    What was pathological in the real incident was not growth, it was REPEATED
    growth: the same file getting bigger, with more streams, on every pass,
    forever. So growth is only a failure once it has happened
    `sanity_check_growth_repeats` times in a row on the same path (default 2),
    which catches the runaway on the second pass instead of the twenty-fifth
    while letting the one-off downmix through.

    Multiplying audio needs no repeat count. One task turning one stereo track
    into two is already wrong. But because it fires on the first pass and
    discards the transcode, it is measured entirely as GROWTH relative to the
    input, never as identity within the output:

      - if the output has no more audio streams than the input, it cannot
        have multiplied anything, and is never flagged. Normalising a Blu-ray
        remux that carries the same 5.1 mix as both DTS-HD and AC3 down to one
        codec yields two matching tracks in the output, but two went in and
        two came out. That is conservation, and it is healthy.
      - comparison ignores codec, layout spelling and sample rate, since those
        are exactly what a transcode is supposed to change, and keeps channel
        count, language and title, which are what identify a distinct track.
      - a file that already carried duplicates before the task keeps them
        without flagging, or every pass over an already-damaged library would
        fail forever.

    The dial is set that way on purpose. A false positive here throws away a
    good encode and teaches an operator to switch the checks off; a false
    negative costs one more scan cycle, because a runaway that appends a track
    every pass trips the very next time round. Those costs are not symmetric.

    The other two checks already work this way: both compare a COUNT (or a
    size) against the input rather than inspecting the output on its own, and
    both additionally require the growth to repeat, so neither can be tripped
    by a task that merely rearranged or re-encoded what it was given.

    All the thresholds are configurable, because "materially larger" and "how
    many repeats" genuinely depend on what a given library's plugin flow is
    for. The defaults are chosen to be quiet on correct pipelines.

    WHAT HAPPENS ON A FAILURE
    -------------------------
    The caller (the post-processor) refuses to deliver the output, keeps the
    original file untouched, and marks the task failed. A failed task is
    recorded in history, which both raises a UI notification and - via
    FileTest.file_failed_in_history - stops the file being queued again. The
    damage stops at one pass instead of compounding.

    That is deliberately the loud option. Discarding one transcode and shouting
    about it is recoverable; silently writing a file with a twenty-sixth audio
    track is not.
"""

import datetime
import os

from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.unmodels import FileSanityState

#: Defaults, mirrored by the `sanity_check_*` keys on `config.Config`. Kept
#: here as well so the checks are usable (and testable) with any settings
#: object, including one that predates these keys.
DEFAULT_ENABLED = True
DEFAULT_GROWTH_REPEATS = 2
DEFAULT_SIZE_GROWTH_RATIO = 1.05
DEFAULT_DUPLICATE_AUDIO_ENABLED = True

#: State rows for files nobody has processed in this long are meaningless - a
#: "streak" spanning more than a year is not a streak. Pruned on write so the
#: table cannot grow without bound as files are renamed or deleted.
STATE_MAX_AGE_DAYS = 365

CHECK_DUPLICATE_AUDIO = 'duplicate_audio'
CHECK_STREAM_GROWTH = 'stream_growth'
CHECK_SIZE_GROWTH = 'size_growth'
#: Issue #82. The input probed cleanly and the output did not.
CHECK_UNPROBEABLE_OUTPUT = 'unprobeable_output'

logger = TrawlarrLogging.get_logger(name='OutputSanity')


def _coerce_bool(value, default):
    """
    Config values can arrive as strings - `Config.__import_settings_from_env`
    assigns the raw environment string. bool("false") is True, so a setting
    disabled via the environment would silently stay on.

    :param value:
    :param default:
    :return:
    """
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes', 'on'):
            return True
        if lowered in ('false', '0', 'no', 'off', ''):
            return False
        return default
    return bool(value)


def _coerce_number(value, default, cast):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


class SanityCheckSettings(object):
    """
    The thresholds the checks run with.
    """

    def __init__(self, enabled=DEFAULT_ENABLED, growth_repeats=DEFAULT_GROWTH_REPEATS,
                 size_growth_ratio=DEFAULT_SIZE_GROWTH_RATIO,
                 duplicate_audio_enabled=DEFAULT_DUPLICATE_AUDIO_ENABLED):
        self.enabled = _coerce_bool(enabled, DEFAULT_ENABLED)
        # A repeat count below 1 would fail every single growing task, including
        # the first legitimate one. Clamp rather than honour it.
        self.growth_repeats = max(1, _coerce_number(growth_repeats, DEFAULT_GROWTH_REPEATS, int))
        self.size_growth_ratio = _coerce_number(size_growth_ratio, DEFAULT_SIZE_GROWTH_RATIO, float)
        self.duplicate_audio_enabled = _coerce_bool(duplicate_audio_enabled, DEFAULT_DUPLICATE_AUDIO_ENABLED)

    @classmethod
    def from_settings(cls, settings):
        """
        Read the thresholds off a `config.Config`.

        Uses getattr with defaults rather than direct calls so that any
        settings object without these getters (an older config file, a stub)
        yields the documented defaults instead of an exception. A crash in the
        threshold lookup must not be able to take down post-processing.

        :param settings:
        :return:
        """

        def _read(getter_name, default):
            getter = getattr(settings, getter_name, None)
            if not callable(getter):
                return default
            try:
                value = getter()
            except Exception:
                return default
            return default if value is None else value

        return cls(
            enabled=_read('get_sanity_checks_enabled', DEFAULT_ENABLED),
            growth_repeats=_read('get_sanity_check_growth_repeats', DEFAULT_GROWTH_REPEATS),
            size_growth_ratio=_read('get_sanity_check_size_growth_ratio', DEFAULT_SIZE_GROWTH_RATIO),
            duplicate_audio_enabled=_read('get_sanity_check_duplicate_audio', DEFAULT_DUPLICATE_AUDIO_ENABLED),
        )


class SanityResult(object):
    """
    The outcome of evaluating one task's output.

    :ivar failures:   list of dicts with 'id' and 'message'. Non-empty means
                      the output must not be delivered.
    :ivar state:      the per-path state to persist for the next task.
    :ivar checked:    False when no check actually ran. Exactly two ways to
                      get it: (a) check_task_output() was called with the
                      feature disabled; (b) evaluate() had neither a usable
                      pair of probes nor a usable pair of sizes. Note (b) is
                      a pair, not "neither file probed" - one file probing
                      and the other not is still no comparison, and the one
                      case of that which IS a failure (source readable,
                      output not) has already returned by the time this is
                      decided. Never a failure; an unrunnable check must not
                      block a good transcode. See evaluate() and issue #82.
                      Pinned by tests/unit/test_doc_claims.py.
    :ivar output_probe: the ffprobe dict for the output file, when one was
                      obtained. Carried on the result purely so the caller can
                      hand it on rather than probing the same bytes twice -
                      see the convergence check (issue #34). Nothing in this
                      module reads it back.
    """

    def __init__(self, failures=None, state=None, checked=True, output_probe=None):
        self.failures = failures or []
        self.state = state or {}
        self.checked = checked
        self.output_probe = output_probe

    @property
    def failed(self):
        return bool(self.failures)

    def report(self):
        """
        A human-readable block for the task log, so the reason shows up in the
        UI next to the failed task rather than only in the daemon log.

        :return:
        """
        if not self.failures:
            return ''
        lines = [
            '',
            'TRAWLARR OUTPUT SANITY CHECK FAILED',
            'The processed file was NOT written to the library and the original was left untouched.',
            'This file will not be queued again until the failed task is cleared from history.',
            '',
        ]
        for failure in self.failures:
            lines.append('  [{}] {}'.format(failure.get('id'), failure.get('message')))
        lines.append('')
        return '\n'.join(lines)


def audio_stream_signature(stream):
    """
    The identity of an audio stream for duplicate detection.

    What a transcode is ALLOWED to change is deliberately excluded: codec,
    channel layout spelling and sample rate. Normalising a Blu-ray remux that
    carries the same 5.1 English mix twice (DTS-HD plus AC3) to a single codec
    produces two tracks that only a codec-blind signature can recognise as the
    same two tracks that went in. Comparing on codec made that healthy pipeline
    look like duplication.

    What identifies a distinct track is kept: channel count, language, and the
    title tag - two 2-channel English tracks are not the same track if one of
    them is labelled "Commentary".

    :param stream:
    :return:
    """
    tags = stream.get('tags') or {}
    if not isinstance(tags, dict):
        tags = {}
    return (
        str(stream.get('channels', '')),
        str(tags.get('language', '')).lower(),
        str(tags.get('title', '')).lower(),
    )


def _streams(probe):
    streams = (probe or {}).get('streams')
    if not isinstance(streams, list):
        return []
    return [s for s in streams if isinstance(s, dict)]


def stream_count(probe):
    """
    Total number of streams in a probe result.

    :param probe:
    :return:
    """
    return len(_streams(probe))


def audio_signature_counts(probe):
    """
    Map of audio stream signature -> how many streams carry it.

    :param probe:
    :return:
    """
    counts = {}
    for stream in _streams(probe):
        if str(stream.get('codec_type', '')).lower() != 'audio':
            continue
        signature = audio_stream_signature(stream)
        counts[signature] = counts.get(signature, 0) + 1
    return counts


def audio_stream_count(probe):
    """
    Number of audio streams in a probe result.

    :param probe:
    :return:
    """
    return sum(1 for s in _streams(probe) if str(s.get('codec_type', '')).lower() == 'audio')


def new_duplicate_audio_groups(source_probe, output_probe):
    """
    Audio signatures the task MULTIPLIED.

    The pathology from the incident is audio tracks breeding: more of a given
    track coming out than went in. It is not the mere presence of two matching
    tracks in the output - a source can perfectly well contain two of the same
    thing, and a normalising plugin can perfectly well make two differing
    tracks look alike.

    Two guards, in order:

      1. If the output has no more audio streams than the input, nothing was
         multiplied and nothing is reported, whatever the signatures say. This
         is the hard invariant: a transcode that does not increase the audio
         track count can never be flagged, so re-tagging, re-titling or
         re-coding a fixed track layout is always safe.
      2. Otherwise, report only signatures whose count went UP and ended at
         two or more. A file that already carried duplicates before this task
         is not this task's doing, or every pass over an already-damaged
         library would fail forever.

    Both guards err the same way, deliberately. This check has no repeat
    tolerance - it fails a task on its first pass and discards the transcode -
    so a false positive costs a good encode and an operator's trust, while a
    false negative costs one more scan cycle before the runaway is caught on
    the following pass. Those are not symmetric, and the tolerance goes to the
    false negative.

    :param source_probe:
    :param output_probe:
    :return: list of (signature, source_count, output_count)
    """
    if audio_stream_count(output_probe) <= audio_stream_count(source_probe):
        return []
    source_counts = audio_signature_counts(source_probe)
    output_counts = audio_signature_counts(output_probe)
    duplicated = []
    for signature, output_count in output_counts.items():
        source_count = source_counts.get(signature, 0)
        if output_count >= 2 and output_count > source_count:
            duplicated.append((signature, source_count, output_count))
    return sorted(duplicated, key=lambda item: item[0])


def _describe_signature(signature):
    channels, language, title = signature
    parts = []
    if channels:
        parts.append('{}ch'.format(channels))
    parts.append('lang={}'.format(language or 'und'))
    if title:
        parts.append('title={!r}'.format(title))
    return ' '.join(parts)


def evaluate(source_probe, output_probe, source_size, output_size, previous_state=None, settings=None):
    """
    Judge one task's output against its input.

    Pure: no I/O, no database. Everything it needs is passed in, which is what
    makes the thresholds testable without an ffmpeg install.

    :param source_probe:    ffprobe dict for the input file (or None)
    :param output_probe:    ffprobe dict for the output file (or None)
    :param source_size:     input size in bytes (or None)
    :param output_size:     output size in bytes (or None)
    :param previous_state:  dict as returned by load_state()
    :param settings:        SanityCheckSettings
    :return: SanityResult
    """
    settings = settings or SanityCheckSettings()
    previous_state = previous_state or {}
    failures = []

    source_readable = bool(_streams(source_probe))
    output_readable = bool(_streams(output_probe))

    # --- the output could not be read at all -----------------------------
    # Issue #82. This used to be an "unrunnable check", reported as checked=
    # False and waved through, which meant the single most damaged output a
    # task can produce - one ffprobe will not open - was the one case that
    # always passed.
    #
    # It is asked as "the INPUT probed and the OUTPUT did not", never as
    # "the output did not probe", and the difference is the whole reason this
    # is safe to fail on. A missing ffmpeg or a build without the probe
    # library fails both files identically, so it is not reported here; it
    # degrades to the size comparison below when both sizes are readable, and
    # to checked=False when they are not.
    #
    # Storage faults are NOT in that exempt set, and the earlier wording that
    # put them there was wrong: the source and the output live on different
    # mounts (library and cache), so a mount going away can perfectly well
    # fail one probe and not the other. When it is the output's mount, this
    # fires - which is correct. An output nobody can read is not a delivery.
    #
    # Only a task that demonstrably had a readable file to work from and
    # produced something unreadable trips this.
    # Pinned by tests/unit/test_doc_claims.py.
    if source_readable and not output_readable:
        failures.append({
            'id':      CHECK_UNPROBEABLE_OUTPUT,
            'message': (
                'The input file probed cleanly but the output did not: it is missing, empty, or '
                'damaged badly enough that ffprobe cannot read a single stream from it. It has not '
                'been delivered to the library.'
            ),
        })
        # Nothing else can be compared against a file we cannot read, and the
        # task is failing regardless. Returning here keeps the report to the
        # one finding that actually explains it. No streak state is returned:
        # the output is discarded, so there is no growth to carry forward.
        return SanityResult(failures=failures, state={}, checked=True)

    have_probes = source_readable and output_readable
    have_sizes = bool(source_size) and bool(output_size)
    if not have_probes and not have_sizes:
        # Nothing to compare. Say so rather than reporting a clean bill of
        # health we did not actually earn.
        return SanityResult(checked=False)

    # --- duplicate identical audio streams ------------------------------
    if have_probes and settings.duplicate_audio_enabled:
        for signature, source_count, output_count in new_duplicate_audio_groups(source_probe, output_probe):
            failures.append({
                'id':      CHECK_DUPLICATE_AUDIO,
                'message': (
                    'Output contains {} matching audio streams ({}) where the input had {}, '
                    'and more audio streams overall. A plugin is duplicating audio rather '
                    'than replacing it.'
                ).format(output_count, _describe_signature(signature), source_count),
            })

    # --- repeated stream count growth -----------------------------------
    source_streams = stream_count(source_probe) if have_probes else None
    output_streams = stream_count(output_probe) if have_probes else None
    stream_streak = int(previous_state.get('stream_growth_streak', 0) or 0)
    if have_probes:
        if output_streams > source_streams:
            stream_streak += 1
        else:
            stream_streak = 0
        if stream_streak >= settings.growth_repeats:
            failures.append({
                'id':      CHECK_STREAM_GROWTH,
                'message': (
                    'Stream count grew again ({} -> {}); this is {} consecutive task(s) on this file '
                    'that added streams. A converged file should stop changing.'
                ).format(source_streams, output_streams, stream_streak),
            })

    # --- repeated file size growth --------------------------------------
    size_streak = int(previous_state.get('size_growth_streak', 0) or 0)
    if have_sizes:
        if output_size > (source_size * settings.size_growth_ratio):
            size_streak += 1
        else:
            size_streak = 0
        if size_streak >= settings.growth_repeats:
            failures.append({
                'id':      CHECK_SIZE_GROWTH,
                'message': (
                    'File size grew again ({} -> {} bytes, ratio {:.2f}); this is {} consecutive task(s) '
                    'on this file that made it larger.'
                ).format(source_size, output_size, (float(output_size) / float(source_size)), size_streak),
            })

    state = {
        'stream_growth_streak': stream_streak,
        'size_growth_streak':   size_streak,
        'last_stream_count':    output_streams,
        'last_size':            output_size,
    }
    return SanityResult(failures=failures, state=state)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_state(abspath):
    """
    Read the stored streak state for a path.

    Never raises: a database problem must not stop a task being delivered.

    :param abspath:
    :return: dict (empty when there is no row or the read failed)
    """
    if not abspath:
        return {}
    try:
        row = FileSanityState.get_or_none(FileSanityState.abspath == abspath)
    except Exception:
        logger.exception("Unable to read output sanity state for '%s'", abspath)
        return {}
    if row is None:
        return {}
    return {
        'stream_growth_streak': row.stream_growth_streak,
        'size_growth_streak':   row.size_growth_streak,
        'last_stream_count':    row.last_stream_count,
        'last_size':            row.last_size,
    }


def save_state(abspath, state, previous_abspath=None):
    """
    Persist the streak state under `abspath`.

    When the task renamed the file (a container change, say), the state is
    moved: `previous_abspath`'s row is dropped so the streak follows the file
    forward instead of being stranded on a path that no longer exists.

    Never raises, for the same reason as load_state().

    :param abspath:
    :param state:
    :param previous_abspath:
    :return: True when the state was written
    """
    if not abspath or not state:
        return False
    now = datetime.datetime.now()
    try:
        if previous_abspath and previous_abspath != abspath:
            FileSanityState.delete().where(FileSanityState.abspath == previous_abspath).execute()
        row = FileSanityState.get_or_none(FileSanityState.abspath == abspath)
        if row is None:
            FileSanityState.create(
                abspath=abspath,
                stream_growth_streak=state.get('stream_growth_streak', 0) or 0,
                size_growth_streak=state.get('size_growth_streak', 0) or 0,
                last_stream_count=state.get('last_stream_count'),
                last_size=state.get('last_size'),
                updated_at=now,
            )
        else:
            row.stream_growth_streak = state.get('stream_growth_streak', 0) or 0
            row.size_growth_streak = state.get('size_growth_streak', 0) or 0
            row.last_stream_count = state.get('last_stream_count')
            row.last_size = state.get('last_size')
            row.updated_at = now
            row.save()
        FileSanityState.delete().where(
            FileSanityState.updated_at < (now - datetime.timedelta(days=STATE_MAX_AGE_DAYS))
        ).execute()
    except Exception:
        logger.exception("Unable to record output sanity state for '%s'", abspath)
        return False
    return True


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_file(path):
    """
    ffprobe a file, returning None on any failure.

    :param path:
    :return:
    """
    if not path or not os.path.exists(path):
        return None
    try:
        from trawlarr.libs import unffmpeg
        return unffmpeg.Info().file_probe(path)
    except Exception as e:
        logger.info("Unable to probe '%s' for output sanity checks: %s", path, e)
        return None


def probe_from_task_data_store(task_id, path):
    """
    Reuse a probe a plugin already ran during this task, if there is one for
    THIS file.

    Worker plugins commonly cache an ffprobe result in the task data store, and
    re-probing is wasted work. But a plugin chain probes whichever file it is
    currently working on, which after the first plugin is the cache file, not
    the source. Handing back the wrong file's probe would make the comparison
    compare a file to itself and report "all clear" forever - precisely the
    class of silent failure these checks exist to catch.

    So a cached probe is only accepted when its own `format.filename` says it
    is the file we asked about.

    :param task_id:
    :param path:
    :return: probe dict or None
    """
    if task_id is None or not path:
        return None
    try:
        from trawlarr.libs.task import TaskDataStore
        runner_state = TaskDataStore.export_runner_state(task_id)
    except Exception:
        return None

    wanted = os.path.abspath(path)
    for plugin_values in (runner_state or {}).values():
        if not isinstance(plugin_values, dict):
            continue
        for runner_values in plugin_values.values():
            if not isinstance(runner_values, dict):
                continue
            for value in runner_values.values():
                if not isinstance(value, dict) or not isinstance(value.get('streams'), list):
                    continue
                filename = (value.get('format') or {}).get('filename')
                if not filename:
                    continue
                try:
                    if os.path.abspath(filename) == wanted:
                        return value
                except Exception:
                    continue
    return None


def _file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return None


def check_task_output(source_path, output_path, settings=None, task_id=None):
    """
    Run the checks for one completed task.

    Reads the persisted streak state for `source_path`, probes both files
    (reusing a plugin's probe of the source when one is available), evaluates,
    and returns the result. Writing the new state back is the caller's job -
    it depends on whether the output is going to be delivered and under which
    path.

    :param source_path:
    :param output_path:
    :param settings:  SanityCheckSettings
    :param task_id:   used to look for an already-cached source probe
    :return: SanityResult
    """
    settings = settings or SanityCheckSettings()
    if not settings.enabled:
        return SanityResult(checked=False)

    source_probe = probe_from_task_data_store(task_id, source_path) or probe_file(source_path)
    output_probe = probe_file(output_path)
    result = evaluate(
        source_probe=source_probe,
        output_probe=output_probe,
        source_size=_file_size(source_path),
        output_size=_file_size(output_path),
        previous_state=load_state(source_path),
        settings=settings,
    )
    result.output_probe = output_probe
    return result
