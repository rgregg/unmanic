#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    trawlarr.libs.plugin_settings.py

    Required plugin settings. See issue #40.

    THE PROBLEM
    -----------
    A plugin whose settings are incomplete does not fail - it does nothing.
    The recorded incident is a stream-mapping plugin left with an empty
    `stream_language` on one worker: every runner returned `data` unchanged,
    the worker reported success, the postprocessor moved a byte-identical file
    back into place and the task went green. Nothing in the log, the UI or the
    task history said the word "settings". The library simply stopped being
    processed and looked healthy while it happened.

    HOW A SETTING IS DECLARED REQUIRED
    ----------------------------------
    By adding `'required': True` to the setting's entry in the plugin's
    `form_settings` dict:

        form_settings = {
            'stream_language': {
                'label':    'Stream language to keep',
                'required': True,
            },
        }

    `form_settings` was chosen over info.json, a new file, or a new base-class
    attribute for one reason: it is the only place that already describes
    settings one key at a time, it is already per-setting metadata rather than
    per-plugin, and it is already read by both the API and the WebUI. A plugin
    that declares nothing has no `required` keys, so it produces no findings
    and behaves exactly as it does today. That is the whole compatibility
    story: silence is the default, and every community plugin ever written is
    silent.

    The declaration is deliberately the PLUGIN AUTHOR's, not the operator's.
    An operator-side "these keys are required" list would let a user mark a
    setting required that the plugin treats as optional, which converts a
    working install into a hard stop - the exact false-alarm failure this
    codebase has already rejected twice. The author knows which of their
    settings has no sensible empty behaviour; nobody else does.

    Trade-off, stated plainly: a third-party plugin that never adds `required`
    is not protected by any of this. We cannot edit community plugins, and
    guessing which of their settings matter would be inventing facts about
    code we do not own. What this buys is that the mechanism EXISTS, costs a
    plugin author one line, and degrades to today's behaviour otherwise.

    WHAT COUNTS AS "UNSET"
    ----------------------
    This is the part that decides whether the check is trustworthy, so it is
    conservative on purpose:

      unset      None; a string that is empty or only whitespace; an empty
                 list, tuple, set or dict (a required multi-select with
                 nothing chosen).

      NOT unset  False. A required checkbox that is off is a decision the user
                 made, not an omission - `False` is a legitimate value for a
                 boolean setting, and treating it as missing would fire on
                 every install that deliberately turned something off.

      NOT unset  0, 0.0. Zero is a legitimate number. A required "offset" of 0
                 is configured.

      NOT unset  anything else, including objects we do not understand.

    Only a `form_settings` entry that is a dict whose `required` value is
    literally `True` counts. A truthy string or a non-dict entry is ignored,
    so a plugin using the key for something else cannot be misread into
    failing its own tasks.

    A `form_settings` key with no matching key in `settings` is skipped
    entirely. `form_settings` legitimately carries display-only pseudo-entries
    (`section_header`, `section_details`, `section_admonition`) that have no
    stored value; they can never be "set" and must never be reported.

    WHERE THE CHECK RUNS - AND WHY IN TWO PLACES
    --------------------------------------------
    1. AT STARTUP, as a set of `RegistrationFinding` results merged into the
       report from `plugin_registration` (issue #38 left that seam
       deliberately: `RegistrationFinding` + `RegistrationReport.extend`).
       This is what "surfaced against the library" means - the finding names
       the library whose pipeline references the plugin, and reaches the
       operator through the same single notification channel as every other
       "your plugin will not do what you think" finding. Two competing
       diagnostic banners would be its own confusion.

    2. IMMEDIATELY BEFORE A WORKER RUNS ANY RUNNER, in
       `Worker.__exec_worker_runners_on_set_task`, which refuses the task
       outright.

    Startup alone is not enough: settings are edited in the WebUI while the
    service runs, so the state a worker sees can go bad hours after the
    startup check passed. A runtime check alone is not enough either: it only
    speaks when a file happens to be queued, and the operator's question is
    "why is this library not being processed", asked while nothing is queued.

    The runtime gate is placed BEFORE the first runner, before the
    `events.worker_process_started` event, and before any cache file exists,
    because "refuse to run" mid-pipeline is not refusing - it is abandoning a
    half-processed file. Failing at the top means the source file has not been
    touched, no cache file has been written, and the task lands in history as
    a failure with a reason attached, which is precisely the outcome the
    silent no-op denied us.

    WHAT THIS DOES NOT CATCH
    ------------------------
    - Runtime plugin types other than `worker.process` are checked at startup
      but have no pre-flight gate. A `library_management.file_test` plugin
      with an unset required setting still runs; refusing there would silently
      change which files are queued, which is a different and worse failure.
    - A setting that is set but wrong (a language code that matches no stream,
      a path that does not exist). Only emptiness is detectable from here.
    - Plugins that declare nothing required, i.e. every plugin that exists
      today.
    - Settings a plugin computes in its own `__init__` and never exposes.
"""

from trawlarr.libs.logs import TrawlarrLogging
from trawlarr.libs.plugin_registration import (
    RegistrationFinding,
    SEVERITY_ERROR,
)

#: Finding code, in the vocabulary of plugin_registration.RegistrationFinding.
FINDING_REQUIRED_SETTING_UNSET = 'required_setting_unset'

#: The key a plugin author adds to a `form_settings` entry.
REQUIRED_KEY = 'required'

#: UI notification raised when a worker refuses a task. Stable so repeated
#: refusals replace the banner in place instead of stacking one per task.
NOTIFICATION_UUID = 'pluginRequiredSettingsUnset'

logger = TrawlarrLogging.get_logger(name='PluginSettings')


def setting_is_unset(value):
    """
    Is this value "not configured"?

    See the module docstring for the reasoning. Briefly: None, blank strings
    and empty containers are unset; False and 0 are configured values.

    :param value:
    :return: bool
    """
    if value is None:
        return True
    if isinstance(value, bool):
        # Deliberately before the numeric case: bool is a subclass of int.
        return False
    if isinstance(value, str):
        return value.strip() == ''
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def required_setting_keys(form_settings):
    """
    The keys a plugin has declared required.

    Only a dict entry whose `required` value is literally `True` counts.

    :param form_settings:
    :return: set of keys
    """
    keys = set()
    if not isinstance(form_settings, dict):
        return keys
    for key, entry in form_settings.items():
        if isinstance(entry, dict) and entry.get(REQUIRED_KEY) is True:
            keys.add(key)
    return keys


def _setting_label(form_settings, key):
    entry = form_settings.get(key) if isinstance(form_settings, dict) else None
    if isinstance(entry, dict):
        label = entry.get('label')
        if isinstance(label, str) and label.strip():
            return label.strip()
    return key


def find_unset_required_settings(settings, form_settings):
    """
    The required settings of one plugin that have no value.

    A required key that does not appear in `settings` is skipped: it is a
    display-only `form_settings` entry (`section_header` and friends) with no
    stored value, not a missing configuration.

    :param settings: the plugin's effective settings, as the runner will see them
    :param form_settings: the plugin's form metadata
    :return: list of {'key', 'label'} dicts, ordered by key
    """
    if not isinstance(settings, dict):
        return []
    unset = []
    for key in sorted(required_setting_keys(form_settings)):
        if key not in settings:
            continue
        if setting_is_unset(settings.get(key)):
            unset.append({
                'key':   key,
                'label': _setting_label(form_settings, key),
            })
    return unset


def unset_required_settings_for_plugin(plugin_id, library_id=None, plugin_executor=None):
    """
    Load one plugin's settings exactly as the runner will see them and report
    the required ones that are empty.

    The settings are read through `PluginExecutor.get_plugin_settings`, the
    same path the WebUI and the worker use, deliberately: a validator with its
    own private view of a plugin's configuration could disagree with the code
    that actually runs, and a diagnostic that is wrong about the thing it is
    diagnosing is worse than none.

    Never raises. A plugin that cannot be loaded reports nothing here - that
    case is already `plugin_not_installed` in the registration validator.

    :param plugin_id: the plugin DIRECTORY id
    :param library_id: the library whose settings profile to read
    :param plugin_executor: injected for tests
    :return: list of {'key', 'label'} dicts
    """
    try:
        if plugin_executor is None:
            from trawlarr.libs.unplugins import PluginExecutor
            plugin_executor = PluginExecutor()
        settings, form_settings = plugin_executor.get_plugin_settings(plugin_id, library_id=library_id)
    except Exception:
        logger.debug("Unable to read settings for plugin '%s'", plugin_id, exc_info=True)
        return []
    return find_unset_required_settings(settings, form_settings)


def _describe_library(library_id, library_name):
    if library_name:
        return "library '{}' (id {})".format(library_name, library_id)
    return "library id {}".format(library_id)


def _finding_message(plugin_id, plugin_name, unset, library_id, library_name):
    names = ", ".join("'{}' ({})".format(item['label'], item['key']) for item in unset)
    if len(unset) == 1:
        subject = "a required setting that is not configured"
    else:
        subject = "{} required settings that are not configured".format(len(unset))
    return (
        "CONFIGURATION ERROR: plugin '{}' (id '{}') is enabled on {} with {}: {}. Trawlarr refuses to run this "
        "plugin until it is configured, because a plugin that runs unconfigured returns the file unchanged and "
        "reports success - the library looks processed and is not. Configure the setting, or disable the plugin "
        "for this library."
    ).format(
        plugin_name or plugin_id,
        plugin_id,
        _describe_library(library_id, library_name),
        subject,
        names,
    )


def validate_required_plugin_settings(plugin_executor=None):
    """
    Every library, every plugin enabled on it, every required setting.

    Reads only. Findings are `RegistrationFinding` objects so the startup
    entry point can merge them into the registration report rather than
    opening a second diagnostic channel.

    Never raises.

    :param plugin_executor: injected for tests
    :return: list of RegistrationFinding
    """
    findings = []
    try:
        from trawlarr.libs.unmodels import EnabledPlugins, Libraries, Plugins

        plugins_by_row_id = {row['id']: row for row in Plugins.select().dicts()}
        library_names = {row['id']: row.get('name') for row in Libraries.select().dicts()}
        enabled_rows = list(EnabledPlugins.select().dicts())
    except Exception:
        logger.exception("Unable to read the plugin tables to check required plugin settings")
        return findings

    # Deterministic order, and one settings load per (library, plugin) pair.
    seen = set()
    for row in sorted(enabled_rows, key=lambda r: (r.get('library_id') or 0, r.get('plugin_id') or 0)):
        library_id = row.get('library_id')
        plugin = plugins_by_row_id.get(row.get('plugin_id'))
        if plugin is None:
            # Already reported as missing_plugin_record by plugin_registration.
            continue
        plugin_directory_id = plugin.get('plugin_id')
        if (library_id, plugin_directory_id) in seen:
            continue
        seen.add((library_id, plugin_directory_id))

        unset = unset_required_settings_for_plugin(
            plugin_directory_id, library_id=library_id, plugin_executor=plugin_executor)
        if not unset:
            continue

        findings.append(RegistrationFinding(
            code=FINDING_REQUIRED_SETTING_UNSET,
            severity=SEVERITY_ERROR,
            library_id=library_id,
            library_name=library_names.get(library_id),
            plugin_id=plugin_directory_id,
            plugin_row_id=row.get('plugin_id'),
            message=_finding_message(
                plugin_directory_id, plugin.get('name'), unset, library_id, library_names.get(library_id)),
        ))

    return findings


def check_plugins_before_run(plugin_ids, library_id=None, plugin_executor=None):
    """
    The pre-flight gate. Given the plugins a worker is about to run against a
    library, return the ones that are missing required settings.

    :param plugin_ids: iterable of plugin DIRECTORY ids, in execution order
    :param library_id:
    :param plugin_executor: injected for tests
    :return: list of {'plugin_id', 'unset'} dicts, empty when everything is configured
    """
    misconfigured = []
    for plugin_id in plugin_ids or []:
        if not plugin_id:
            continue
        unset = unset_required_settings_for_plugin(
            plugin_id, library_id=library_id, plugin_executor=plugin_executor)
        if unset:
            misconfigured.append({
                'plugin_id': plugin_id,
                'unset':     unset,
            })
    return misconfigured


def describe_misconfigured_plugins(misconfigured, library_id=None, library_name=None):
    """
    One human-readable paragraph naming every plugin and every empty setting.

    Written out in full rather than summarised because the failure this
    replaces was a message too vague to act on - there wasn't one at all.

    :param misconfigured: as returned by check_plugins_before_run
    :param library_id:
    :param library_name:
    :return: str
    """
    parts = []
    for item in misconfigured:
        names = ", ".join("'{}' ({})".format(u['label'], u['key']) for u in item['unset'])
        parts.append("plugin '{}' is missing {}".format(item['plugin_id'], names))
    return (
        "CONFIGURATION ERROR: this task was refused before any processing began because {}. "
        "An unconfigured plugin returns the file unchanged and reports success, so Trawlarr fails the task "
        "instead of pretending it worked. The source file has NOT been modified. "
        "Configure the setting on {}, or disable the plugin for that library, then re-queue the file."
    ).format("; ".join(parts), _describe_library(library_id, library_name))


def raise_configuration_notification(message):
    """
    Put the refusal in front of the operator, not only in the log.

    Uses a stable UUID so a run of refused tasks replaces one banner rather
    than stacking hundreds. Never raises.

    :param message:
    :return:
    """
    try:
        from trawlarr.libs.notifications import Notifications
        Notifications().update({
            'uuid':       NOTIFICATION_UUID,
            'type':       'error',
            'icon':       'report_problem',
            'label':      'pluginRequiredSettingsLabel',
            'message':    message,
            'navigation': {
                'push': '/ui/settings-library',
            },
        })
    except Exception:
        logger.exception("Failed to raise the unset-required-settings notification")
