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
    Three cheap assertions, run in the post-processor after the worker has
    finished but before the output is allowed anywhere near the library:

      duplicate_audio  - the output contains more copies of an identical audio
                         stream (same codec, channel count, layout, sample
                         rate, language and title) than the input did.
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

    Duplicate identical audio streams need no repeat count. One task producing
    two byte-identical-looking stereo tracks where the input had one is already
    wrong, and it is measured relative to the input so a library that is
    already damaged does not flag on files this pipeline did not touch.

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
    :ivar checked:    False when there was not enough information to judge
                      (no probe, missing file). Never a failure - an
                      unrunnable check must not block a good transcode.
    """

    def __init__(self, failures=None, state=None, checked=True):
        self.failures = failures or []
        self.state = state or {}
        self.checked = checked

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

    Deliberately includes the title tag: two 2-channel English AAC tracks are
    not duplicates if one of them is labelled "Commentary".

    :param stream:
    :return:
    """
    tags = stream.get('tags') or {}
    if not isinstance(tags, dict):
        tags = {}
    return (
        str(stream.get('codec_name', '')).lower(),
        str(stream.get('channels', '')),
        str(stream.get('channel_layout', '')).lower(),
        str(stream.get('sample_rate', '')),
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


def new_duplicate_audio_groups(source_probe, output_probe):
    """
    Audio stream signatures the task multiplied.

    Only signatures whose count went UP and ended at two or more are reported.
    A file that already carried duplicates before this task is not this task's
    doing and is not reported, or every pass over an already-damaged library
    would fail forever.

    :param source_probe:
    :param output_probe:
    :return: list of (signature, source_count, output_count)
    """
    source_counts = audio_signature_counts(source_probe)
    output_counts = audio_signature_counts(output_probe)
    duplicated = []
    for signature, output_count in output_counts.items():
        source_count = source_counts.get(signature, 0)
        if output_count >= 2 and output_count > source_count:
            duplicated.append((signature, source_count, output_count))
    return sorted(duplicated, key=lambda item: item[0])


def _describe_signature(signature):
    codec, channels, layout, sample_rate, language, title = signature
    parts = [codec or 'unknown codec']
    if channels:
        parts.append('{}ch'.format(channels))
    if layout:
        parts.append(layout)
    if sample_rate:
        parts.append('{}Hz'.format(sample_rate))
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

    have_probes = bool(_streams(source_probe)) and bool(_streams(output_probe))
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
                    'Output contains {} identical audio streams ({}) where the input had {}. '
                    'A plugin is duplicating audio rather than replacing it.'
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
    return result
