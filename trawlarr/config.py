#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
    trawlarr.config.py

    Written by:               Josh.5 <jsunnex@gmail.com>
    Date:                     06 Dec 2018, (7:21 AM)

    Copyright:
           Copyright (C) Josh Sunnex - All Rights Reserved

           Permission is hereby granted, free of charge, to any person obtaining a copy
           of this software and associated documentation files (the "Software"), to deal
           in the Software without restriction, including without limitation the rights
           to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
           copies of the Software, and to permit persons to whom the Software is
           furnished to do so, subject to the following conditions:

           The above copyright notice and this permission notice shall be included in all
           copies or substantial portions of the Software.

           THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
           EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
           MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
           IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
           DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
           OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
           OR OTHER DEALINGS IN THE SOFTWARE.

"""

import os
import json

from trawlarr import metadata
from trawlarr.libs import common, runtimepaths
from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.singleton import SingletonType

try:
    from json.decoder import JSONDecodeError
except ImportError:
    JSONDecodeError = ValueError

logger = TrawlarrLogging.get_logger(name="Config")

#: Runtime paths, which are derived state rather than user settings.
#:
#: These four are resolved during startup from the home directory, the
#: command line and the environment, and every subsystem (logging, the plugin
#: loader, the userdata store) caches its location from them.
#:
#: They are deliberately **not persisted to settings.json and ignored when it
#: is read back**. Persisting them makes the application's own settings file
#: pin it to whatever absolute path it happened to use when the file was last
#: written — which is exactly how the ``.unmanic`` -> ``.trawlarr`` move used
#: to undo itself. A correctly migrated install whose settings.json had ever
#: been written would come up, read ``<HOME>/.unmanic/...`` out of its own
#: settings, recreate the legacy directory, and run entirely from it: new
#: empty database, new logs, new plugins directory, migrated data ignored. On
#: the next restart both directories hold data, so the legacy-config guard
#: stays quiet forever. Recomputing them on every start makes the location a
#: function of how the process was launched, which is what it always was.
#:
#: Overriding them is still supported, but only through the launch surface -
#: `--config`/`unmanic_path` on the command line, or the environment (see
#: `Config.__import_settings_from_env`). Those are re-applied on every start.
DERIVED_PATH_CONFIG_KEYS = frozenset({
    'config_path',
    'log_path',
    'plugins_path',
    'userdata_path',
})

#: Configuration keys that the HTTP API is never permitted to write.
#:
#: Writing one over the API does not move anything on disk; it only
#: desynchronises the running process from its own files.
#:
#: They remain freely writable by internal callers via `set_config_item()` /
#: `set_bulk_config_items()` — the restriction is a property of the API
#: boundary, not of the config object. Requests naming any of these keys are
#: rejected with a 400; see `ApiSettingsHandler.write_settings()`.
API_PROTECTED_CONFIG_KEYS = DERIVED_PATH_CONFIG_KEYS

#: Seconds a worker subprocess may show no sign of life before the worker
#: stall detector terminates it. See `trawlarr.libs.workers` for what counts
#: as a sign of life - it is deliberately much broader than "reported a
#: progress percentage", because plenty of healthy commands go a long time
#: between progress updates.
DEFAULT_WORKER_STALL_TIMEOUT = 300

#: Lower bound applied to `worker_stall_timeout`. A misconfigured two-second
#: threshold would kill healthy transcodes, which is a far worse failure than
#: not having a stall detector at all, so the value is clamped rather than
#: trusted.
MINIMUM_WORKER_STALL_TIMEOUT = 60


class Config(object, metaclass=SingletonType):
    app_version = ''

    test = ''

    def __init__(self, config_path=None, **kwargs):
        # Set the default UI Port
        self.ui_port = 8888
        self.ui_address = ''

        # SSL/TLS settings
        self.ssl_enabled = False
        self.ssl_certfilepath = None
        self.ssl_keyfilepath = None

        # Set default directories
        home_directory = common.get_home_dir()
        app_directory = runtimepaths.app_dir(home_directory)
        self.config_path = os.path.join(app_directory, 'config')
        self.log_path = os.path.join(app_directory, 'logs')
        self.plugins_path = os.path.join(app_directory, 'plugins')
        self.userdata_path = os.path.join(app_directory, 'userdata')

        # Configure debugging
        self.debugging = False

        # Configure log buffer retention (in days)
        self.log_buffer_retention = 0

        # Configure first run (future feature)
        self.first_run = False
        self.release_notes_viewed = None
        self.trial_welcome_viewed = None

        # Library Settings:
        self.library_path = common.get_default_library_path()
        self.enable_library_scanner = False
        self.schedule_full_scan_minutes = 1440
        self.follow_symlinks = True
        self.concurrent_file_testers = 2
        self.run_full_scan_on_start = False
        self.clear_pending_tasks_on_restart = True
        self.auto_manage_completed_tasks = False
        self.compress_completed_tasks_logs = False
        self.max_age_of_completed_tasks = 91
        self.always_keep_failed_tasks = True

        # Output sanity checks (see trawlarr/libs/sanity.py and issue #35).
        # Structural assertions on a task's output vs its input, run before the
        # output is allowed into the library. Thresholds are exposed because
        # "materially larger" and "how many repeats" genuinely depend on what a
        # library's plugin flow is for.
        self.sanity_checks_enabled = True
        # Consecutive tasks on the same file that may grow it before the file is
        # flagged and halted. 1 would fail the first legitimate stereo downmix;
        # 2 catches a runaway on its second pass.
        self.sanity_check_growth_repeats = 2
        # Output/input size ratio above which a task counts as having grown the
        # file. A few percent of container overhead is not growth.
        self.sanity_check_size_growth_ratio = 1.05
        # Flag a task that ends up with MORE audio streams than the input and
        # more copies of some comparable track (same channels/language/title)
        # than the input had. Needs no repeat count - multiplying audio once is
        # already wrong. A task that does not increase the audio stream count is
        # never flagged, so normalising a dual-codec remux to a single codec is
        # not mistaken for duplication.
        self.sanity_check_duplicate_audio = True

        # Durable task failure state (see trawlarr/libs/taskfailure.py and
        # issue #25). How many times the same file may fail in a row before a
        # retry is refused unless it is explicitly forced. A success on the
        # file resets the count.
        self.max_consecutive_task_failures = 3

        # Worker settings
        self.cache_path = common.get_default_cache_path()
        self.worker_stall_detection_enabled = True
        self.worker_stall_timeout = DEFAULT_WORKER_STALL_TIMEOUT

        # Installation identity (used to label forwarded logs)
        self.installation_name = ''

        # Legacy config
        # TODO: Remove this before next major version bump
        self.number_of_workers = None
        self.worker_event_schedules = None

        # Import env variables and override all previous settings.
        self.__import_settings_from_env()

        # Import Unmanic path settings from command params
        if kwargs.get('unmanic_path'):
            self.set_config_item('config_path', os.path.join(kwargs.get('unmanic_path'), 'config'), save_settings=False)
            self.set_config_item('plugins_path', os.path.join(kwargs.get('unmanic_path'), 'plugins'), save_settings=False)
            self.set_config_item('userdata_path', os.path.join(kwargs.get('unmanic_path'), 'userdata'), save_settings=False)

        # Finally, re-read config from file and override all previous settings.
        self.__import_settings_from_file(config_path)

        # Overwrite current settings with given args
        if config_path:
            self.set_config_item('config_path', config_path, save_settings=False)

        # Overwrite all other settings passed from command params
        if kwargs.get('port'):
            self.set_config_item('ui_port', kwargs.get('port'), save_settings=False)
        if kwargs.get('address'):
            self.set_config_item('ui_address', kwargs.get('address'), save_settings=False)

        # Apply settings to the unmanic logger
        self.__setup_unmanic_logger()

    def get_config_as_dict(self):
        """
        Return a dictionary of configuration fields and their current values

        :return:
        """
        return self.__dict__

    def get_config_keys(self):
        """
        Return a list of configuration fields

        :return:
        """
        return self.get_config_as_dict().keys()

    def __setup_unmanic_logger(self):
        """
        Pass configuration to the global logger

        :return:
        """
        TrawlarrLogging.get_logger(settings=self)

    def __import_settings_from_env(self):
        """
        Read configuration from environment variables.
        This is useful for running in a docker container or for unit testing.

        This runs before settings.json is read, and it is the supported way to
        override the runtime paths in `DERIVED_PATH_CONFIG_KEYS`. Because those
        keys are neither written to nor read from settings.json, an override
        set here is not shadowed by a stale absolute path in the settings file,
        and it is re-applied identically on every start.

        :return:
        """
        for setting in self.get_config_keys():
            if setting in os.environ:
                self.set_config_item(setting, os.environ.get(setting), save_settings=False)

    def __import_settings_from_file(self, config_path=None):
        """
        Read configuration from the settings JSON file.

        :return:
        """
        # If config path was not passed as variable, use the default one
        if not config_path:
            config_path = self.get_config_path()
        # Ensure the config path exists
        if not os.path.exists(config_path):
            os.makedirs(config_path)
        settings_file = os.path.join(config_path, 'settings.json')
        if os.path.exists(settings_file):
            data = {}
            try:
                with open(settings_file) as infile:
                    data = json.load(infile)
            except Exception as e:
                logger.exception("Exception in reading saved settings from file: %s", e)
            # Drop the derived runtime paths. Older settings.json files (and
            # any written by an older build) carry absolute paths in them; the
            # values already computed for this process are authoritative.
            data = {key: value for key, value in data.items() if key not in DERIVED_PATH_CONFIG_KEYS}
            # Set data to Config class
            self.set_bulk_config_items(data, save_settings=False)

    def reload(self):
        """
        Reload configuration from file
        :return:
        """
        self.__import_settings_from_file()

    def __write_settings_to_file(self):
        """
        Dump current settings to the settings JSON file.

        The derived runtime paths are excluded. See `DERIVED_PATH_CONFIG_KEYS`.

        :return:
        """
        if not os.path.exists(self.get_config_path()):
            os.makedirs(self.get_config_path())
        settings_file = os.path.join(self.get_config_path(), 'settings.json')
        data = {
            key: value
            for key, value in self.get_config_as_dict().items()
            if key not in DERIVED_PATH_CONFIG_KEYS
        }
        result = common.json_dump_to_file(data, settings_file)
        if not result['success']:
            for message in result['errors']:
                logger.error(message)
            raise Exception("Exception in writing settings to file")

    def get_config_item(self, key):
        """
        Get setting from either this class or the Settings model

        :param key:
        :return:
        """
        # First attempt to fetch it from this class' get functions
        if hasattr(self, "get_{}".format(key)):
            getter = getattr(self, "get_{}".format(key))
            if callable(getter):
                return getter()

    def set_config_item(self, key, value, save_settings=True):
        """
        Assigns a value to a given configuration field.
        This is applied to both this class.

        If 'save_settings' is set to False, then settings are only
        assigned and not saved to file.

        :param key:
        :param value:
        :param save_settings:
        :return:
        """
        # Get lowercase value of key
        field_id = key.lower()
        # Check if key is a valid setting
        if field_id not in self.get_config_keys():
            logger.warning("Attempting to save unknown key: %s", key)
            # Do not proceed if this is any key other than the database
            return

        # If in a special config list, execute that command
        if hasattr(self, "set_{}".format(key)):
            setter = getattr(self, "set_{}".format(key))
            if callable(setter):
                setter(value)
        else:
            # Assign value directly to class attribute
            setattr(self, key, value)

        # Save settings (if requested)
        if save_settings:
            try:
                self.__write_settings_to_file()
            except Exception as e:
                logger.exception("Failed to write settings to file: %s", str(self.get_config_as_dict()))

    def set_bulk_config_items(self, items, save_settings=True):
        """
        Write bulk config items to this class.

        :param items:
        :param save_settings:
        :return:
        """
        # Set values that match the settings model attributes
        config_keys = self.get_config_keys()
        for config_key in config_keys:
            # Only import the item if it exists (Running a get here would default a missing var to None)
            if config_key in items:
                self.set_config_item(config_key, items[config_key], save_settings=save_settings)

    @staticmethod
    def read_version():
        """
        Return the application's version number as a string

        :return:
        """
        return metadata.read_version_string('long')

    def read_system_logs(self, lines=None):
        """
        Return an array of system log lines

        :param lines:
        :return:
        """
        log_lines = []
        log_file = os.path.join(self.log_path, 'unmanic.log')
        line_count = 0
        for line in reversed(list(open(log_file))):
            log_lines.insert(0, line.rstrip())
            line_count += 1
            if line_count == lines:
                break
        return log_lines

    def get_ui_port(self):
        """
        Get setting - ui_port

        :return:
        """
        return self.ui_port

    def get_ui_address(self):
        """
        Get setting - ui_address

        :return:
        """
        return self.ui_address

    def get_cache_path(self):
        """
        Get setting - cache_path

        :return:
        """
        return self.cache_path

    def set_cache_path(self, cache_path):
        """
        Get setting - cache_path

        :return:
        """
        if cache_path == "":
            logger.warning("Cache path cannot be empty. Resetting it to default.")
            cache_path = common.get_default_cache_path()
        self.cache_path = cache_path

    def get_config_path(self):
        """
        Get setting - config_path

        :return:
        """
        return self.config_path

    def get_debugging(self):
        """
        Get setting - debugging

        :return:
        """
        return self.debugging

    def set_debugging(self, value):
        """
        Set setting - debugging

        This requires an update to the logger object

        :return:
        """
        if value:
            TrawlarrLogging.enable_debugging()
        else:
            TrawlarrLogging.disable_debugging()
        self.debugging = value

    def get_log_buffer_retention(self):
        """
        Get setting - log_buffer_retention

        :return:
        """
        return self.log_buffer_retention

    def set_log_buffer_retention(self, value):
        """
        Set setting - log_buffer_retention

        This requires an update to the logger object

        :return:
        """
        try:
            retention_days = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"log_buffer_retention must be an integer, got {value!r}")
        try:
            # On Unmanic startup, it may not have yet initialised the logger when this is first run.
            TrawlarrLogging.set_remote_logging_retention(retention_days)
        except (AttributeError):
            pass
        self.log_buffer_retention = retention_days

    def get_first_run(self):
        """
        Get setting - first_run

        :return:
        """
        return self.first_run

    def get_release_notes_viewed(self):
        """
        Get setting - release_notes_viewed

        :return:
        """
        return self.release_notes_viewed

    def get_trial_welcome_viewed(self):
        """
        Get setting - trial_welcome_viewed

        :return:
        """
        return self.trial_welcome_viewed

    def get_library_path(self):
        """
        Get setting - library_path

        :return:
        """
        return self.library_path

    def get_clear_pending_tasks_on_restart(self):
        """
        Get setting - clear_pending_tasks_on_restart

        :return:
        """
        return self.clear_pending_tasks_on_restart

    def get_auto_manage_completed_tasks(self):
        """
        Get setting - auto_manage_completed_tasks

        :return:
        """
        return self.auto_manage_completed_tasks

    def get_max_age_of_completed_tasks(self):
        """
        Get setting - max_age_of_completed_tasks

        :return:
        """
        return self.max_age_of_completed_tasks

    def get_compress_completed_tasks_logs(self):
        """
        Get setting - compress_completed_tasks_logs

        :return:
        """
        return self.compress_completed_tasks_logs

    def get_always_keep_failed_tasks(self):
        """
        Get setting - always_keep_failed_tasks

        :return:
        """
        return self.always_keep_failed_tasks

    def get_sanity_checks_enabled(self):
        """
        Get setting - sanity_checks_enabled

        :return:
        """
        return self.sanity_checks_enabled

    def get_sanity_check_growth_repeats(self):
        """
        Get setting - sanity_check_growth_repeats

        :return:
        """
        return self.sanity_check_growth_repeats

    def get_sanity_check_size_growth_ratio(self):
        """
        Get setting - sanity_check_size_growth_ratio

        :return:
        """
        return self.sanity_check_size_growth_ratio

    def get_sanity_check_duplicate_audio(self):
        """
        Get setting - sanity_check_duplicate_audio

        :return:
        """
        return self.sanity_check_duplicate_audio

    def get_max_consecutive_task_failures(self):
        """
        Get setting - max_consecutive_task_failures

        :return:
        """
        return self.max_consecutive_task_failures

    def get_log_path(self):
        """
        Get setting - log_path

        :return:
        """
        return self.log_path

    def get_worker_stall_detection_enabled(self):
        """
        Get setting - worker_stall_detection_enabled

        :return:
        """
        return bool(self.worker_stall_detection_enabled)

    def set_worker_stall_detection_enabled(self, value):
        """
        Set setting - worker_stall_detection_enabled

        Accepts the string forms that arrive from environment variables as
        well as real booleans.

        :return:
        """
        if isinstance(value, str):
            value = value.strip().lower() in ('true', 'yes', 'on', '1')
        self.worker_stall_detection_enabled = bool(value)

    def get_worker_stall_timeout(self):
        """
        Get setting - worker_stall_timeout

        Always returns a usable number of seconds. A value that is missing,
        non-numeric or dangerously small falls back to a safe one rather than
        arming a hair-trigger that kills healthy work.

        :return:
        """
        try:
            timeout = int(self.worker_stall_timeout)
        except (TypeError, ValueError):
            logger.warning(
                "Configured worker_stall_timeout %r is not a number. Using %s seconds.",
                self.worker_stall_timeout, DEFAULT_WORKER_STALL_TIMEOUT)
            return DEFAULT_WORKER_STALL_TIMEOUT
        if timeout < MINIMUM_WORKER_STALL_TIMEOUT:
            logger.warning(
                "Configured worker_stall_timeout of %s seconds is below the minimum of %s seconds. "
                "Using the minimum.", timeout, MINIMUM_WORKER_STALL_TIMEOUT)
            return MINIMUM_WORKER_STALL_TIMEOUT
        return timeout

    def get_number_of_workers(self):
        """
        Get setting - number_of_workers

        :return:
        """
        return self.number_of_workers

    def get_worker_event_schedules(self):
        """
        Get setting - worker_event_schedules

        :return:
        """
        return self.worker_event_schedules

    def get_enable_library_scanner(self):
        """
        Get setting - enable_library_scanner

        :return:
        """
        return self.enable_library_scanner

    def get_run_full_scan_on_start(self):
        """
        Get setting - run_full_scan_on_start

        :return:
        """
        return self.run_full_scan_on_start

    def get_schedule_full_scan_minutes(self):
        """
        Get setting - schedule_full_scan_minutes

        :return:
        """
        return self.schedule_full_scan_minutes

    def get_follow_symlinks(self):
        """
        Get setting - follow_symlinks

        :return:
        """
        return self.follow_symlinks

    def get_concurrent_file_testers(self):
        """
        Get setting - concurrent_file_testers

        :return:
        """
        return self.concurrent_file_testers

    def get_plugins_path(self):
        """
        Get setting - config_path

        :return:
        """
        return self.plugins_path

    def get_userdata_path(self):
        """
        Get setting - userdata_path

        :return:
        """
        return self.userdata_path

    def get_installation_name(self):
        """
        Get setting - installation_name

        :return:
        """
        return self.installation_name

    def get_ssl_enabled(self):
        """
        Get setting - ssl_enabled

        :return:
        """
        # Convert string to boolean if necessary (for environment variables)
        if isinstance(self.ssl_enabled, str):
            return self.ssl_enabled.lower() in ('true', '1', 'yes', 'on')
        return bool(self.ssl_enabled)

    def get_ssl_certfilepath(self):
        """
        Get setting - ssl_certfilepath

        :return:
        """
        return self.ssl_certfilepath

    def get_ssl_keyfilepath(self):
        """
        Get setting - ssl_keyfilepath

        :return:
        """
        return self.ssl_keyfilepath
