#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_output_sanity_checks.py

    Tests for trawlarr/libs/sanity.py and its hook in the post-processor.

    The motivating incident (issue #35): a plugin bug appended an extra AAC
    stereo track to a file on every library scan. Every task reported success.
    One episode reached twenty-five identical stereo tracks before a human
    noticed.

    Invariants covered:

    1. The incident is caught on the SECOND pass, not the twenty-fifth.
    2. A single legitimate structural change (adding one stereo downmix,
       one container remux that grows the file) is NOT flagged. A check that
       cries wolf gets turned off and then protects nothing.
    3. Duplicates the pipeline did not create - a file that already carried
       two identical tracks before the task - are not flagged, or every pass
       over an already-damaged library would fail forever.
    4. Two audio tracks that agree on channels and language but differ in
       title (a commentary track) are not duplicates.
    4a. Duplication is measured as GROWTH against the source, never as
       likeness within the output. A task that does not increase the audio
       stream count is never flagged, so normalising a dual-codec Blu-ray
       remux (DTS-HD 5.1 + AC3 5.1 -> two EAC3 5.1 tracks) passes. This check
       has no repeat tolerance, so a false positive costs a good encode
       immediately while a false negative costs one extra scan cycle; the
       bias goes to the false negative.
    5. Growth is only a failure once it repeats `growth_repeats` times in a
       row on the same file; a non-growing task resets the streak.
    6. On failure the post-processor discards the output, leaves the source
       file exactly as it was, marks the task failed, and writes the reason
       into the task log.
    7. On success the post-processor delivers the file as normal and records
       the streak state against the file's new path.
    8. A checker that cannot run (no probe, no ffmpeg) never blocks delivery.
"""
import logging
import os
from unittest import mock

import pytest

from trawlarr.libs import sanity
from trawlarr.libs.postprocessor import PostProcessor


# ---------------------------------------------------------------------------
# Probe fixtures
# ---------------------------------------------------------------------------

def _audio(codec='aac', channels=2, layout='stereo', rate='48000', language='eng', title=None):
    tags = {'language': language}
    if title is not None:
        tags['title'] = title
    return {
        'codec_type':     'audio',
        'codec_name':     codec,
        'channels':       channels,
        'channel_layout': layout,
        'sample_rate':    rate,
        'tags':           tags,
    }


def _video():
    return {'codec_type': 'video', 'codec_name': 'h264'}


def _probe(streams, filename=None):
    probe = {'streams': list(streams)}
    if filename:
        probe['format'] = {'filename': filename}
    return probe


def _episode_probe(extra_stereo_tracks):
    """The incident file: video, the original 5.1 track, and however many
    appended AAC stereo tracks it has accumulated so far."""
    streams = [_video(), _audio(codec='ac3', channels=6, layout='5.1')]
    streams += [_audio() for _ in range(extra_stereo_tracks)]
    return _probe(streams)


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------

class TestRunawayAudioDuplication:

    def test_first_appended_track_is_not_flagged(self):
        """Pass one: the file gains its first AAC stereo track. That is
        indistinguishable from a legitimate 'add a stereo downmix' plugin, so
        it must pass."""
        result = sanity.evaluate(
            source_probe=_episode_probe(0),
            output_probe=_episode_probe(1),
            source_size=1000, output_size=1010,
            previous_state={},
        )
        assert result.failed is False
        # But the growth was remembered.
        assert result.state['stream_growth_streak'] == 1

    def test_second_appended_track_is_flagged(self):
        """Pass two: now there are two identical AAC stereo tracks where the
        input had one. This is the pass that had to catch it and did not."""
        result = sanity.evaluate(
            source_probe=_episode_probe(1),
            output_probe=_episode_probe(2),
            source_size=1010, output_size=1020,
            previous_state={'stream_growth_streak': 1, 'size_growth_streak': 0},
        )
        assert result.failed is True
        ids = [f['id'] for f in result.failures]
        assert sanity.CHECK_DUPLICATE_AUDIO in ids
        # And the repeated stream growth fired independently on the same pass.
        assert sanity.CHECK_STREAM_GROWTH in ids

    def test_flagged_again_at_twenty_five_tracks(self):
        """An already-ruined file that gets a twenty-sixth track still trips:
        the duplicate count went up during THIS task."""
        result = sanity.evaluate(
            source_probe=_episode_probe(25),
            output_probe=_episode_probe(26),
            source_size=2000, output_size=2010,
            previous_state={},
        )
        assert sanity.CHECK_DUPLICATE_AUDIO in [f['id'] for f in result.failures]

    def test_appended_copy_in_a_different_codec_is_still_caught(self):
        """The duplicate the plugin appends need not match on codec. Comparing
        on channels/language/title catches a stereo track that was appended as
        Opus next to the AAC one it copied."""
        source = _probe([_video(), _audio(codec='ac3', channels=6, layout='5.1'), _audio()])
        output = _probe([_video(), _audio(codec='ac3', channels=6, layout='5.1'),
                         _audio(), _audio(codec='opus')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=2100,
            previous_state={},
        )
        assert sanity.CHECK_DUPLICATE_AUDIO in [f['id'] for f in result.failures]

    def test_duplication_during_a_dual_codec_normalisation_is_still_caught(self):
        """The healthy case is two-in/two-out. Two in and THREE out, all
        matching, is the runaway starting on a remux - and still fails."""
        source = _probe([_video(), _audio(codec='dts', channels=6, layout='5.1'),
                         _audio(codec='ac3', channels=6, layout='5.1')])
        output = _probe([_video()] + [_audio(codec='eac3', channels=6, layout='5.1')
                                      for _ in range(3)])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=2100,
            previous_state={},
        )
        assert sanity.CHECK_DUPLICATE_AUDIO in [f['id'] for f in result.failures]


# ---------------------------------------------------------------------------
# False positives
# ---------------------------------------------------------------------------

class TestDoesNotCryWolf:

    def test_a_preexisting_duplicate_pair_survives_an_unrelated_addition(self):
        """Pins the `output_count > source_count` term specifically.

        The source already carries two matching English stereo tracks -- old
        damage, not this task's doing. This task adds a surround track, so
        the audio count grows and the count guard does not apply. The matching
        pair is unchanged at two, so nothing was duplicated *here* and the
        task must not be blamed. Without the strictly-greater comparison this
        would flag on every future pass over an already-damaged file.
        """
        source = _probe([_video(), _audio('aac', 2, 'stereo'), _audio('aac', 2, 'stereo')])
        output = _probe([_video(), _audio('aac', 2, 'stereo'), _audio('aac', 2, 'stereo'),
                         _audio('eac3', 6, '5.1')])
        assert sanity.evaluate(source, output, 2000, 1500, {}).failed is False

    def test_preexisting_duplicates_are_not_this_tasks_fault(self):
        """The input already had two identical stereo tracks and the output
        still has two. Flagging this would fail every future pass over a
        library that was damaged before these checks existed."""
        result = sanity.evaluate(
            source_probe=_episode_probe(2),
            output_probe=_episode_probe(2),
            source_size=1000, output_size=900,
            previous_state={},
        )
        assert result.failed is False

    def test_commentary_track_is_not_a_duplicate(self):
        """Same codec, same channel count, same language - different track.
        The title tag is what tells them apart."""
        source = _probe([_video(), _audio(title='Main')])
        output = _probe([_video(), _audio(title='Main'), _audio(title='Commentary')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=1000, output_size=1100,
            previous_state={},
        )
        assert result.failed is False

    def test_single_growing_remux_is_not_flagged(self):
        """One task that makes the file bigger - a lossless remux, a re-encode
        from a lower bitrate source - is legitimate."""
        result = sanity.evaluate(
            source_probe=_probe([_video(), _audio()]),
            output_probe=_probe([_video(), _audio()]),
            source_size=1000, output_size=2000,
            previous_state={},
        )
        assert result.failed is False
        assert result.state['size_growth_streak'] == 1

    def test_shrinking_task_resets_the_streak(self):
        """A file that grew last time and shrank this time is not runaway."""
        result = sanity.evaluate(
            source_probe=_probe([_video(), _audio(), _audio(title='Commentary')]),
            output_probe=_probe([_video(), _audio()]),
            source_size=2000, output_size=1000,
            previous_state={'stream_growth_streak': 1, 'size_growth_streak': 1},
        )
        assert result.failed is False
        assert result.state['stream_growth_streak'] == 0
        assert result.state['size_growth_streak'] == 0

    def test_dual_codec_source_normalised_to_one_codec_is_not_flagged(self):
        """The regression this class exists for.

        A standard Blu-ray remux carries the same mix twice in two codecs -
        DTS-HD 5.1 English AND AC3 5.1 English. A plugin that normalises all
        audio to EAC3 turns those into two output tracks that match each
        other. Two tracks went in, two came out: nothing multiplied. Flagging
        it would discard a correct transcode on its first pass, with no repeat
        tolerance to soften it."""
        source = _probe([_video(), _audio(codec='dts', channels=6, layout='5.1'),
                         _audio(codec='ac3', channels=6, layout='5.1')])
        output = _probe([_video(), _audio(codec='eac3', channels=6, layout='5.1'),
                         _audio(codec='eac3', channels=6, layout='5.1')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=1500,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_stereo_downmix_added_once_alongside_the_surround_track(self):
        """The single most common legitimate plugin flow there is."""
        source = _probe([_video(), _audio(codec='dts', channels=6, layout='5.1')])
        output = _probe([_video(), _audio(codec='eac3', channels=6, layout='5.1'), _audio()])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=1800,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_commentary_matching_the_main_track_after_normalisation(self):
        """Main and commentary already share codec, language and channels;
        after a re-encode they share sample rate too. Only the title tells
        them apart, and neither of them multiplied."""
        source = _probe([_video(), _audio(codec='ac3', title='Main'),
                         _audio(codec='ac3', title='Commentary')])
        output = _probe([_video(), _audio(codec='aac', title='Main'),
                         _audio(codec='aac', title='Commentary')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=1500,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_untitled_commentary_alongside_an_untitled_main_track(self):
        """A remux that drops the title tags leaves two tracks that are
        indistinguishable in the output. The count did not change, so this
        is still conservation and must pass - the invariant is about growth,
        not about how alike the output tracks look."""
        source = _probe([_video(), _audio(codec='ac3', title='Main'),
                         _audio(codec='ac3', title='Commentary')])
        output = _probe([_video(), _audio(codec='aac'), _audio(codec='aac')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=1500,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_multi_language_release_sharing_one_codec(self):
        """Four language tracks normalised to a single codec. Four in, four
        out."""
        languages = ['eng', 'fra', 'deu', 'spa']
        source = _probe([_video()] + [_audio(codec='ac3', channels=6, layout='5.1', language=lang)
                                      for lang in languages])
        output = _probe([_video()] + [_audio(codec='eac3', channels=6, layout='5.1', language=lang)
                                      for lang in languages])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=4000, output_size=3000,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_reencode_keeping_the_same_track_layout(self):
        """Same layout in and out, every stream re-encoded. Nothing about a
        codec, sample rate or layout-spelling change is evidence of
        duplication."""
        source = _probe([_video(), _audio(codec='dts', channels=6, layout='5.1', rate='48000'),
                         _audio(codec='dts', channels=2, rate='48000')])
        output = _probe([_video(), _audio(codec='opus', channels=6, layout='5.1(side)', rate='44100'),
                         _audio(codec='opus', channels=2, rate='44100')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=2000, output_size=1000,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_audio_track_dropped_during_normalisation_is_not_flagged(self):
        """Fewer audio streams out than in cannot be duplication, however
        alike the survivors look."""
        source = _probe([_video(), _audio(codec='dts', channels=6, layout='5.1'),
                         _audio(codec='ac3', channels=6, layout='5.1'),
                         _audio(codec='truehd', channels=6, layout='5.1')])
        output = _probe([_video(), _audio(codec='eac3', channels=6, layout='5.1'),
                         _audio(codec='eac3', channels=6, layout='5.1')])
        result = sanity.evaluate(
            source_probe=source, output_probe=output,
            source_size=3000, output_size=1500,
            previous_state={},
        )
        assert result.failed is False, result.report()

    def test_container_overhead_is_not_size_growth(self):
        """A rewrite that lands a fraction of a percent larger has not 'grown'
        the file; the default ratio absorbs container overhead."""
        result = sanity.evaluate(
            source_probe=_probe([_video(), _audio()]),
            output_probe=_probe([_video(), _audio()]),
            source_size=1000, output_size=1010,
            previous_state={'size_growth_streak': 5},
        )
        assert result.failed is False
        assert result.state['size_growth_streak'] == 0


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

class TestThresholds:

    def test_repeats_threshold_is_configurable(self):
        """With repeats set to 3, the second consecutive growth still passes."""
        settings = sanity.SanityCheckSettings(growth_repeats=3)
        result = sanity.evaluate(
            source_probe=_probe([_video(), _audio()]),
            output_probe=_probe([_video(), _audio(), _audio(title='x')]),
            source_size=1000, output_size=1000,
            previous_state={'stream_growth_streak': 1},
            settings=settings,
        )
        assert result.failed is False
        assert result.state['stream_growth_streak'] == 2

    def test_repeats_below_one_is_clamped(self):
        """A repeat count of 0 would fail every growing task including the
        first legitimate one. Clamp it rather than honour it."""
        assert sanity.SanityCheckSettings(growth_repeats=0).growth_repeats == 1
        assert sanity.SanityCheckSettings(growth_repeats=-5).growth_repeats == 1

    def test_duplicate_audio_check_can_be_disabled(self):
        settings = sanity.SanityCheckSettings(duplicate_audio_enabled=False)
        result = sanity.evaluate(
            source_probe=_episode_probe(1),
            output_probe=_episode_probe(2),
            source_size=1000, output_size=1000,
            previous_state={},
            settings=settings,
        )
        assert sanity.CHECK_DUPLICATE_AUDIO not in [f['id'] for f in result.failures]

    def test_string_valued_settings_are_coerced(self):
        """Config values imported from the environment arrive as strings.
        bool('false') is True, which would silently leave a disabled check on."""
        assert sanity.SanityCheckSettings(enabled='false').enabled is False
        assert sanity.SanityCheckSettings(enabled='true').enabled is True
        assert sanity.SanityCheckSettings(growth_repeats='3').growth_repeats == 3
        assert sanity.SanityCheckSettings(size_growth_ratio='1.5').size_growth_ratio == 1.5
        # Garbage falls back to the default rather than raising.
        assert sanity.SanityCheckSettings(growth_repeats='banana').growth_repeats == 2

    def test_settings_read_from_a_config_without_the_getters(self):
        """An older settings object must yield the documented defaults, not an
        exception - a threshold lookup must never take down post-processing."""
        settings = sanity.SanityCheckSettings.from_settings(object())
        assert settings.enabled is True
        assert settings.growth_repeats == sanity.DEFAULT_GROWTH_REPEATS


class TestUncheckableOutput:

    def test_no_probe_and_no_sizes_reports_unchecked(self):
        """Report 'could not check' rather than a clean bill of health we did
        not earn."""
        result = sanity.evaluate(None, None, None, None, {})
        assert result.checked is False
        assert result.failed is False

    def test_sizes_alone_are_still_compared(self):
        """No ffprobe available, but the sizes are free."""
        result = sanity.evaluate(None, None, 1000, 5000, {'size_growth_streak': 1})
        assert result.checked is True
        assert [f['id'] for f in result.failures] == [sanity.CHECK_SIZE_GROWTH]


# ---------------------------------------------------------------------------
# Reusing a plugin's cached probe
# ---------------------------------------------------------------------------

class TestCachedProbeReuse:

    def test_cached_probe_for_the_right_file_is_reused(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x")
        cached = _probe([_video(), _audio()], filename=str(source))
        runner_state = {'some_plugin': {'on_worker_process': {'probe_info': cached}}}

        with mock.patch("trawlarr.libs.task.TaskDataStore.export_runner_state",
                        return_value=runner_state):
            found = sanity.probe_from_task_data_store(1, str(source))

        assert found is cached

    def test_cached_probe_for_a_different_file_is_refused(self, tmp_path):
        """A worker plugin chain probes whichever file it is currently working
        on, which after the first plugin is the cache file. Accepting it would
        compare the output to itself and report 'all clear' forever."""
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x")
        cache_file = tmp_path / "cache" / "movie-abc123.mkv"
        cached = _probe([_video(), _audio()], filename=str(cache_file))
        runner_state = {'some_plugin': {'on_worker_process': {'probe_info': cached}}}

        with mock.patch("trawlarr.libs.task.TaskDataStore.export_runner_state",
                        return_value=runner_state):
            found = sanity.probe_from_task_data_store(1, str(source))

        assert found is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def sanity_state_db():
    """Bind FileSanityState to a throwaway in-memory database."""
    from peewee import SqliteDatabase
    from trawlarr.libs.unmodels import FileSanityState

    database = SqliteDatabase(':memory:')
    with database.bind_ctx([FileSanityState]):
        database.create_tables([FileSanityState])
        yield database
    database.close()


class TestStatePersistence:

    def test_state_round_trips(self, sanity_state_db):
        assert sanity.load_state('/library/ep.mkv') == {}
        sanity.save_state('/library/ep.mkv', {
            'stream_growth_streak': 1,
            'size_growth_streak':   0,
            'last_stream_count':    3,
            'last_size':            123,
        })
        assert sanity.load_state('/library/ep.mkv') == {
            'stream_growth_streak': 1,
            'size_growth_streak':   0,
            'last_stream_count':    3,
            'last_size':            123,
        }
        # A second write updates in place rather than duplicating the row.
        sanity.save_state('/library/ep.mkv', {'stream_growth_streak': 2, 'size_growth_streak': 0})
        assert sanity.load_state('/library/ep.mkv')['stream_growth_streak'] == 2

    def test_streak_follows_a_renamed_file(self, sanity_state_db):
        """A container change moves the file to a new path. The streak has to
        move with it, or a runaway that also changes the extension resets its
        own counter on every pass and is never caught."""
        sanity.save_state('/library/ep.avi', {'stream_growth_streak': 1, 'size_growth_streak': 1})
        sanity.save_state('/library/ep.mkv', {'stream_growth_streak': 2, 'size_growth_streak': 2},
                          previous_abspath='/library/ep.avi')

        assert sanity.load_state('/library/ep.avi') == {}
        assert sanity.load_state('/library/ep.mkv')['stream_growth_streak'] == 2

    def test_database_failure_is_not_fatal(self):
        """No database bound at all. The checks must degrade, not explode -
        they are a safety net, not a gate."""
        assert sanity.load_state('/library/ep.mkv') == {}
        assert sanity.save_state('/library/ep.mkv', {'stream_growth_streak': 1}) is False


# ---------------------------------------------------------------------------
# Post-processor wiring
# ---------------------------------------------------------------------------

def _build_postprocessor(cache_path, source_abspath, dest_path):
    pp = PostProcessor.__new__(PostProcessor)
    pp.logger = logging.getLogger("test.postprocessor.sanity")
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
    dest = tmp_path / "library" / "episode.mkv"
    return cache, source, dest


def _run_post_process(pp, probes, saved_state, previous_state=None):
    """Run post_process_file with the probe and state layers stubbed out, so
    the real evaluate()/check_task_output() logic and the real file movement
    both run."""
    plugin_handler = mock.Mock()
    plugin_handler.get_enabled_plugin_modules_by_type.return_value = []

    def _save(abspath, state, previous_abspath=None):
        saved_state.append((abspath, state, previous_abspath))
        return True

    with mock.patch("trawlarr.libs.postprocessor.PluginsHandler",
                    return_value=plugin_handler), \
            mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"), \
            mock.patch("trawlarr.libs.sanity.probe_file",
                       side_effect=lambda p: probes.get(os.path.abspath(p))), \
            mock.patch("trawlarr.libs.sanity.probe_from_task_data_store", return_value=None), \
            mock.patch("trawlarr.libs.sanity.load_state", return_value=(previous_state or {})), \
            mock.patch("trawlarr.libs.sanity.save_state", side_effect=_save):
        pp.post_process_file()


class TestPostProcessorHalting:

    def test_duplicated_audio_output_is_never_delivered(self, library_task):
        """The headline behaviour. The worker produced a file with a duplicate
        stereo track; the library file must be left exactly as it was."""
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        probes = {
            os.path.abspath(str(source)): _episode_probe(1),
            os.path.abspath(str(cache)):  _episode_probe(2),
        }
        saved_state = []

        _run_post_process(pp, probes, saved_state)

        # The original file is untouched.
        assert source.read_bytes() == b"original"
        # Nothing was delivered.
        assert pp._last_destination_files == []
        # The task is now failed, which is what blacklists the file and raises
        # the UI notification.
        assert pp.current_task.task.success is False
        # And the reason is in the task log where a user will see it.
        assert len(pp.current_task.command_log) == 1
        report = pp.current_task.command_log[0]
        assert "OUTPUT SANITY CHECK FAILED" in report
        assert sanity.CHECK_DUPLICATE_AUDIO in report
        # The streak was recorded against the source, since that is still the
        # file on disk.
        assert saved_state and saved_state[0][0] == str(source)

    def test_clean_output_is_delivered_normally(self, library_task):
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        probes = {
            os.path.abspath(str(source)): _probe([_video(), _audio(codec='ac3', channels=6, layout='5.1')]),
            os.path.abspath(str(cache)):  _probe([_video(), _audio()]),
        }
        saved_state = []

        _run_post_process(pp, probes, saved_state)

        assert dest.read_bytes() == b"transcoded output"
        assert pp.current_task.task.success is True
        assert pp.current_task.command_log == []
        assert pp._last_destination_files == [str(dest)]
        assert saved_state and saved_state[0][0] == str(dest)

    def test_first_stream_addition_is_delivered(self, library_task):
        """Regression guard for the false-positive direction: a plugin adding
        a stereo downmix for the first time must not have its output thrown
        away."""
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        probes = {
            os.path.abspath(str(source)): _episode_probe(0),
            os.path.abspath(str(cache)):  _episode_probe(1),
        }
        _run_post_process(pp, probes, [])

        assert dest.read_bytes() == b"transcoded output"
        assert pp.current_task.task.success is True

    def test_repeated_stream_growth_halts_on_the_second_pass(self, library_task):
        """Growth with no duplicate audio to give it away - caught purely by
        the streak carried over from the previous task on this file."""
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        probes = {
            os.path.abspath(str(source)): _probe([_video(), _audio(title='a')]),
            os.path.abspath(str(cache)):  _probe([_video(), _audio(title='a'), _audio(title='b')]),
        }
        _run_post_process(pp, probes, [], previous_state={'stream_growth_streak': 1})

        assert source.read_bytes() == b"original"
        assert pp.current_task.task.success is False
        assert sanity.CHECK_STREAM_GROWTH in pp.current_task.command_log[0]

    def test_unprobeable_output_is_still_delivered(self, library_task):
        """No ffmpeg, no probe. The checks degrade to the size comparison and
        must never block a good transcode on their own inability to run."""
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        _run_post_process(pp, {}, [])

        assert dest.read_bytes() == b"transcoded output"
        assert pp.current_task.task.success is True

    def test_checker_exception_does_not_stall_the_pipeline(self, library_task):
        cache, source, dest = library_task
        pp = _build_postprocessor(str(cache), str(source), str(dest))
        plugin_handler = mock.Mock()
        plugin_handler.get_enabled_plugin_modules_by_type.return_value = []

        with mock.patch("trawlarr.libs.postprocessor.PluginsHandler",
                        return_value=plugin_handler), \
                mock.patch.object(pp, "_PostProcessor__cleanup_cache_files"), \
                mock.patch("trawlarr.libs.sanity.check_task_output",
                           side_effect=RuntimeError("boom")):
            pp.post_process_file()

        assert dest.read_bytes() == b"transcoded output"
        assert pp.current_task.task.success is True
