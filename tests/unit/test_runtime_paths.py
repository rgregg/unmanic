#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Ryan Gregg
#
# This file is part of Trawlarr, a fork of Unmanic.
# See LICENSE for the full license text.

"""
    test_runtime_paths.py

    Trawlarr keeps its configuration in ~/.trawlarr/ where Unmanic kept
    ~/.unmanic/, and serves itself under /trawlarr/ where Unmanic served
    /unmanic/. Both are clean breaks: no migration, no fallback, no alias.

    A clean break on a URL announces itself with a 404. A clean break on a
    config directory announces nothing at all — the application would just
    create an empty one and come up looking like a fresh install, with every
    library, plugin, setting and completed task apparently gone. Nothing
    would actually have been deleted, but the only way to establish that is
    to go looking, at whatever hour the container restarted.

    So the directory rename carries a guard, and these tests hold it to all
    three states an upgrade can be in:

      1. legacy directory populated, new one absent   -> refuse to start
      2. neither present (a genuinely fresh install)  -> start normally
      3. both present                                 -> start normally,
                                                         against the new one

    Every test drives a temporary HOME. The real ~/.unmanic is never read.
"""
import asyncio
import http.client
import os
import queue
import shutil
import subprocess
import tempfile
import threading

import pytest

from trawlarr.libs import runtimepaths


def _fresh_config():
    """
    Build a Config that actually re-reads the environment.

    Config is a singleton, and by the time this module runs some other test
    has usually built one against the real home directory. Reach through the
    class's own metaclass rather than an imported `SingletonType` symbol: the
    compatibility shim can leave more than one copy of the metaclass alive,
    and clearing the wrong registry silently hands back the cached instance.
    """
    from trawlarr import config

    registry = type(config.Config)._instances
    registry.pop(config.Config, None)
    return config.Config()


def _drop_cached_config():
    from trawlarr import config

    type(config.Config)._instances.pop(config.Config, None)


def _populate(directory, *relpaths):
    """Create a directory tree with a file in it, standing in for an install"""
    os.makedirs(directory, exist_ok=True)
    for relpath in relpaths:
        target = os.path.join(directory, relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as handle:
            handle.write('{}')


@pytest.fixture(autouse=True)
def _no_ignore_override(monkeypatch):
    """The guard has an env escape hatch; keep it out of the way by default"""
    monkeypatch.delenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, raising=False)


class TestTheGuardCatchesAnUnmigratedInstall:
    """
    State 1: the operator pulled the new image without moving their data.
    """

    def test_it_refuses_to_start(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_message_names_both_directories_in_full(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        # An operator reading this at 2am needs the two absolute paths, not
        # the concept of a rename.
        assert str(tmp_path / '.unmanic') in message
        assert str(tmp_path / '.trawlarr') in message

    def test_the_message_names_both_database_filenames(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/unmanic.db')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert 'unmanic.db' in message
        assert 'trawlarr.db' in message

    def test_the_message_says_nothing_was_deleted(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert 'Nothing has been moved or deleted' in message

    def test_an_empty_new_directory_does_not_defeat_the_guard(self, tmp_path):
        # The Docker entrypoint runs `mkdir -p /config/.trawlarr` before the
        # application starts. If the guard tested existence rather than
        # contents, that mkdir alone would wave an unmigrated install
        # straight through — the exact failure this exists to prevent.
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        os.makedirs(str(tmp_path / '.trawlarr'))

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_guard_never_touches_the_legacy_directory(self, tmp_path):
        legacy = tmp_path / '.unmanic'
        _populate(str(legacy), 'config/settings.json', 'plugins/marker')

        runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert (legacy / 'config' / 'settings.json').is_file()
        assert (legacy / 'plugins' / 'marker').is_file()
        assert not (tmp_path / '.trawlarr').exists()


def _commands_from_message(message):
    """
    Pull the shell block back out of the operator-facing message.

    Deliberately parsed out of the printed message rather than read from the
    function that builds it: what matters is that the text an operator sees
    is the text that works when pasted.
    """
    return '\n'.join(
        line.strip() for line in message.splitlines()
        if line.startswith('    ') and ('mv ' in line or 'mkdir ' in line)
    )


def _paste_and_run(message, cwd):
    return subprocess.run(
        ['bash', '-c', _commands_from_message(message)],
        cwd=str(cwd), capture_output=True, text=True,
    )


class TestThePrintedMigrationActuallyMigrates:
    """
    The remediation in the refuse-to-start message is the whole point of the
    message. An earlier version printed two bare `mv` commands, and the first
    of them was wrong in the single most common case: the Docker entrypoint
    runs `mkdir -p /config/.trawlarr` before the application starts, including
    on the run that refuses, so the destination already exists and
    `mv legacy new` moves the legacy directory INSIDE it. The result was an
    apparently-fresh install with the real data at `.trawlarr/.unmanic/`, and
    a guard that could never fire again because `.trawlarr` now held
    something.

    So these tests do not read the message. They paste it into a shell.
    """

    def _legacy_install(self, tmp_path, db_body=b'SQLite format 3\x00legacy'):
        legacy = tmp_path / '.unmanic'
        _populate(str(legacy), 'config/settings.json', 'plugins/my_plugin/marker')
        (legacy / 'config' / 'unmanic.db').write_bytes(db_body)
        return legacy

    def test_it_works_when_the_destination_already_exists_and_is_empty(self, tmp_path):
        # The Docker upgrade. This is the normal path, not an edge case.
        self._legacy_install(tmp_path)
        os.makedirs(str(tmp_path / '.trawlarr'))

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))
        result = _paste_and_run(message, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ''
        new_dir = tmp_path / '.trawlarr'
        assert (new_dir / 'config' / 'trawlarr.db').read_bytes() == b'SQLite format 3\x00legacy'
        assert (new_dir / 'config' / 'settings.json').is_file()
        assert (new_dir / 'plugins' / 'my_plugin' / 'marker').is_file()
        # The failure mode this exists to prevent: data one level down.
        assert not (new_dir / '.unmanic').exists()
        assert not (tmp_path / '.unmanic').exists()

    def test_it_works_when_the_destination_does_not_exist(self, tmp_path):
        # Bare metal, where nothing has pre-created the directory.
        self._legacy_install(tmp_path)

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))
        result = _paste_and_run(message, tmp_path)

        assert result.returncode == 0, result.stderr
        new_dir = tmp_path / '.trawlarr'
        assert (new_dir / 'config' / 'trawlarr.db').is_file()
        assert (new_dir / 'plugins' / 'my_plugin' / 'marker').is_file()
        assert not (new_dir / '.unmanic').exists()

    @pytest.mark.parametrize('destination_exists', [True, False])
    def test_the_guard_is_satisfied_afterwards(self, tmp_path, destination_exists):
        self._legacy_install(tmp_path)
        if destination_exists:
            os.makedirs(str(tmp_path / '.trawlarr'))

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))
        assert _paste_and_run(message, tmp_path).returncode == 0

        # The operator ran what they were told to run; the next start must
        # come up, and must not print the same message again.
        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    def test_the_write_ahead_log_travels_with_the_database(self, tmp_path):
        # The schema sets journal_mode=wal. A -wal left behind under the old
        # basename is silently discarded by SQLite, taking every
        # un-checkpointed transaction with it.
        legacy = self._legacy_install(tmp_path)
        (legacy / 'config' / 'unmanic.db-wal').write_bytes(b'wal')
        (legacy / 'config' / 'unmanic.db-shm').write_bytes(b'shm')
        os.makedirs(str(tmp_path / '.trawlarr'))

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))
        assert _paste_and_run(message, tmp_path).returncode == 0

        config_dir = tmp_path / '.trawlarr' / 'config'
        assert (config_dir / 'trawlarr.db-wal').read_bytes() == b'wal'
        assert (config_dir / 'trawlarr.db-shm').read_bytes() == b'shm'
        assert not (config_dir / 'unmanic.db-wal').exists()

    def test_absent_sidecars_do_not_break_the_chain(self, tmp_path):
        # The common case: a cleanly shut down database has no -wal or -shm.
        self._legacy_install(tmp_path)

        result = _paste_and_run(
            runtimepaths.check_for_legacy_config_directory(str(tmp_path)), tmp_path)

        assert result.returncode == 0, result.stderr
        assert (tmp_path / '.trawlarr' / 'config' / 'trawlarr.db').is_file()

    def test_a_failing_step_stops_the_sequence_and_changes_nothing(self, tmp_path):
        # No database to rename. The chain must stop at step one, leaving
        # everything under the legacy path so the guard still fires next time
        # rather than half-migrating behind the operator's back.
        legacy = tmp_path / '.unmanic'
        _populate(str(legacy), 'config/settings.json', 'plugins/my_plugin/marker')
        os.makedirs(str(tmp_path / '.trawlarr'))

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))
        result = _paste_and_run(message, tmp_path)

        assert result.returncode != 0
        assert (legacy / 'config' / 'settings.json').is_file()
        assert (legacy / 'plugins' / 'my_plugin' / 'marker').is_file()
        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_destination_is_never_clobbered_if_it_holds_data(self, tmp_path):
        # rmdir refuses a non-empty directory, so the sequence stops instead
        # of nesting one install inside another.
        self._legacy_install(tmp_path)
        _populate(str(tmp_path / '.trawlarr'), 'config/settings.json')

        command = runtimepaths.legacy_config_migration_command(str(tmp_path))
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)

        assert result.returncode != 0
        assert not (tmp_path / '.trawlarr' / '.unmanic').exists()
        assert (tmp_path / '.unmanic').is_dir()

    def test_the_whole_sequence_is_one_and_chained_command(self, tmp_path):
        # A `;` or a newline between steps lets a failure scroll past.
        self._legacy_install(tmp_path)

        commands = _commands_from_message(
            runtimepaths.check_for_legacy_config_directory(str(tmp_path)))
        lines = commands.splitlines()

        assert len(lines) > 1
        for line in lines[:-1]:
            assert line.endswith('&&'), line
        assert ';' not in lines[-1]

    def test_the_message_says_where_to_run_it(self, tmp_path):
        # The container is refusing to start, so `docker exec` is not
        # available. Without this the operator's first move is to try it.
        self._legacy_install(tmp_path)

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert 'HOST' in message
        assert 'docker exec' in message

    def test_the_database_is_renamed_before_anything_moves(self, tmp_path):
        self._legacy_install(tmp_path)

        lines = _commands_from_message(
            runtimepaths.check_for_legacy_config_directory(str(tmp_path))).splitlines()

        # The first step operates entirely inside the legacy directory.
        assert lines[0].startswith('mv {}'.format(tmp_path / '.unmanic' / 'config'))
        # The directory relocation is last.
        assert lines[-1] == 'mv {} {}'.format(tmp_path / '.unmanic', tmp_path / '.trawlarr')


class TestTheDocsPrintTheSameMigration:
    """
    Three copies of these instructions exist: the refuse-to-start message,
    README.md and FORK.md. They were all wrong together once; keep them
    right together by generating the docs' answer from the same function.
    """

    REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

    @pytest.mark.parametrize('document', ['README.md', 'FORK.md'])
    def test_it_documents_the_command_the_application_prints(self, document):
        with open(os.path.join(self.REPO_ROOT, document), encoding='utf-8') as handle:
            text = handle.read()

        # Docker's /config is HOME inside the container, so this is the same
        # command an upgrading operator is shown.
        for line in runtimepaths.legacy_config_migration_command_lines('/config'):
            assert line in text, '{} is missing: {}'.format(document, line)

    @pytest.mark.parametrize('document', ['README.md', 'FORK.md'])
    def test_the_broken_instruction_is_gone(self, document):
        # `mv /config/.unmanic /config/.trawlarr` on its own line, with no
        # preceding rmdir, is the instruction that nested the install one
        # level down on every Docker upgrade.
        with open(os.path.join(self.REPO_ROOT, document), encoding='utf-8') as handle:
            lines = [line.strip() for line in handle]

        for index, line in enumerate(lines):
            if line == 'mv /config/.unmanic /config/.trawlarr':
                assert 'rmdir /config/.trawlarr' in lines[index - 1], (
                    '{}:{} moves the legacy directory onto a destination that '
                    'has not been removed first'.format(document, index + 1))


class TestAFreshInstallStartsNormally:
    """
    State 2: no Unmanic, no Trawlarr. Nothing to warn anyone about.
    """

    def test_it_does_not_refuse_to_start(self, tmp_path):
        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    def test_the_config_object_creates_the_new_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        try:
            settings = _fresh_config()

            assert settings.get_config_path() == str(tmp_path / '.trawlarr' / 'config')
            assert (tmp_path / '.trawlarr' / 'config').is_dir()
            assert not (tmp_path / '.unmanic').exists()
        finally:
            _drop_cached_config()

    def test_an_empty_legacy_directory_is_not_an_install(self, tmp_path):
        # A leftover empty directory is not data. Refusing on it would be a
        # false alarm the operator cannot clear without deleting something.
        os.makedirs(str(tmp_path / '.unmanic'))

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None


class TestBothPresentStartsAgainstTheNewDirectory:
    """
    State 3: the operator moved (or copied) their data. Get out of the way.
    """

    def test_it_does_not_refuse_to_start(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        _populate(str(tmp_path / '.trawlarr'), 'config/settings.json')

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    def test_the_new_directory_is_the_one_used(self, tmp_path, monkeypatch):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        _populate(str(tmp_path / '.trawlarr'), 'config/settings.json')
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        try:
            settings = _fresh_config()

            assert settings.get_config_path() == str(tmp_path / '.trawlarr' / 'config')
            assert '.trawlarr' in settings.get_config_path()
            assert '.unmanic' not in settings.get_config_path()
            assert '.unmanic' not in settings.get_plugins_path()
            assert '.unmanic' not in settings.get_userdata_path()
        finally:
            _drop_cached_config()


class TestTheEscapeHatch:
    """
    An operator who knows the legacy directory is stale must be able to say
    so without deleting it first.
    """

    @pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
    def test_the_env_var_suppresses_the_guard(self, tmp_path, monkeypatch, value):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, value)

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is None

    @pytest.mark.parametrize('value', ['', '0', 'false', 'no'])
    def test_a_falsey_value_leaves_the_guard_armed(self, tmp_path, monkeypatch, value):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv(runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR, value)

        assert runtimepaths.check_for_legacy_config_directory(str(tmp_path)) is not None

    def test_the_message_advertises_the_escape_hatch(self, tmp_path):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')

        message = runtimepaths.check_for_legacy_config_directory(str(tmp_path))

        assert runtimepaths.IGNORE_LEGACY_CONFIG_ENV_VAR in message


class TestTheServiceEntrypointHonoursTheGuard:
    """
    The guard is only worth having if `main()` actually stops for it.
    """

    def test_it_exits_non_zero_and_prints_the_message(self, tmp_path, monkeypatch, capsys):
        _populate(str(tmp_path / '.unmanic'), 'config/settings.json')
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        from trawlarr import service

        with pytest.raises(SystemExit) as exit_info:
            service.guard_against_legacy_config_directory()

        assert exit_info.value.code == 1

        # stderr, not stdout: this is a failure, and operators pipe logs.
        stderr = capsys.readouterr().err
        assert str(tmp_path / '.unmanic') in stderr
        assert str(tmp_path / '.trawlarr') in stderr

    def test_it_returns_quietly_on_a_fresh_install(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv('HOME_DIR', str(tmp_path))

        from trawlarr import service

        assert service.guard_against_legacy_config_directory() is None
        assert capsys.readouterr().err == ''


class TestTheDatabaseFilenameMoved:

    def test_init_db_names_the_trawlarr_database(self, tmp_path, monkeypatch):
        from trawlarr import service

        recorded = {}

        class _StubMigrations:
            def __init__(self, settings):
                recorded['settings'] = settings

            def update_schema(self):
                pass

        class _StubDatabase:
            @staticmethod
            def select_database(settings):
                return 'db-connection'

        import trawlarr.libs.unmodels.lib as unmodels_lib

        monkeypatch.setattr(service, 'Migrations', _StubMigrations)
        monkeypatch.setattr(unmodels_lib, 'Database', _StubDatabase)

        config_path = str(tmp_path / 'config')
        assert service.init_db(config_path) == 'db-connection'

        assert recorded['settings']['FILE'] == os.path.join(config_path, 'trawlarr.db')


class TestTheApiPrefixMoved:

    def test_the_url_constants_carry_the_new_name(self):
        assert runtimepaths.URL_PREFIX == '/trawlarr'
        assert runtimepaths.API_URL_PREFIX == '/trawlarr/api'

    def test_no_route_is_still_mounted_under_the_old_prefix(self):
        # A clean break means the old prefix is not served at all, by
        # anything - not the API, not the UI, not the static assets.
        from trawlarr.libs import uiserver

        source = open(uiserver.__file__, encoding='utf-8').read()

        assert '"/unmanic/' not in source
        assert "'/unmanic/" not in source

    def test_the_swagger_contract_advertises_the_new_server_url(self):
        from trawlarr.webserver.api_v2.schema.swagger import build_swagger_spec

        spec, _errors = build_swagger_spec()

        assert spec.to_dict()['servers'] == [
            {'url': '/trawlarr/api/v2/', 'description': 'Current environment'},
        ]


@pytest.fixture(scope='module')
def live_server():
    """
    Start the real web application on a real socket and yield its address.

    Nothing here is stubbed. The bug this pins was invisible to reading the
    route table: `make_web_app` builds its `Application` with the routes
    passed to the constructor, and every later `add_handlers` group is
    inserted via `default_router.rules.insert(-1, rule)` — i.e. *ahead* of
    them. A catch-all in the constructor list therefore stays last forever
    and nothing can ever 404, however the table reads.
    """
    import tornado.httpserver
    import tornado.ioloop
    import tornado.netutil

    home = tempfile.mkdtemp()
    previous_home = os.environ.get('HOME_DIR')
    os.environ['HOME_DIR'] = home
    _drop_cached_config()

    from trawlarr.libs import uiserver

    data_queues = {
        'inotifytasks':      queue.Queue(),
        'logging':           queue.Queue(),
        'frontend_messages': {},
    }
    app = uiserver.UIServer(data_queues, None, False).make_web_app()

    state = {}
    ready = threading.Event()

    def _serve():
        asyncio.set_event_loop(asyncio.new_event_loop())
        sockets = tornado.netutil.bind_sockets(0, '127.0.0.1')
        state['port'] = sockets[0].getsockname()[1]
        server = tornado.httpserver.HTTPServer(app)
        server.add_sockets(sockets)
        state['loop'] = tornado.ioloop.IOLoop.current()
        ready.set()
        state['loop'].start()
        server.stop()

    thread = threading.Thread(target=_serve, name='test-uiserver', daemon=True)
    thread.start()
    assert ready.wait(30), 'web application never came up'

    try:
        yield '127.0.0.1', state['port']
    finally:
        state['loop'].add_callback(state['loop'].stop)
        thread.join(30)
        _drop_cached_config()
        if previous_home is None:
            os.environ.pop('HOME_DIR', None)
        else:
            os.environ['HOME_DIR'] = previous_home
        shutil.rmtree(home, ignore_errors=True)


def _http_get(live_server, path):
    """GET without following redirects, so the real status code survives"""
    host, port = live_server
    connection = http.client.HTTPConnection(host, port, timeout=30)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        response.read()
        return response.status, response.getheader('Location')
    finally:
        connection.close()


class TestTheOldUrlPrefixFailsCleanly:
    """
    The status code the old prefix actually returns, over a real socket.

    `make_web_app` used to end its constructor list with a catch-all redirect
    to the dashboard, which (see `live_server`) can never be displaced. So
    `/unmanic/api/v2/version/read` answered **301** to
    `/trawlarr/ui/dashboard/` rather than 404. `RedirectHandler` is permanent
    by default, so a browser or a `curl -L` caches that redirect and an API
    client asking for JSON silently receives an HTML page — strictly worse
    than a clean failure, and it outlives the fix in every client that saw it.

    Both the README and the smoke workflow claim this path 404s. These tests
    make the code answer for that claim.
    """

    def test_the_legacy_api_path_returns_404(self, live_server):
        status, _location = _http_get(live_server, '/unmanic/api/v2/version/read')

        assert status == 404

    def test_the_legacy_api_path_does_not_redirect(self, live_server):
        # Specifically not 301: a cached permanent redirect outlives the fix.
        status, location = _http_get(live_server, '/unmanic/api/v2/version/read')

        assert status not in (301, 302, 303, 307, 308)
        assert location is None

    def test_the_legacy_ui_path_returns_404(self, live_server):
        status, _location = _http_get(live_server, '/unmanic/ui/dashboard/')

        assert status == 404

    def test_an_unknown_path_returns_404(self, live_server):
        status, _location = _http_get(live_server, '/no/such/thing')

        assert status == 404

    def test_the_new_api_path_still_answers(self, live_server):
        status, _location = _http_get(live_server, '/trawlarr/api/v2/version/read')

        assert status == 200

    @pytest.mark.parametrize('path', ['/', '/trawlarr', '/trawlarr/'])
    def test_the_site_root_still_reaches_the_dashboard(self, live_server, path):
        # The catch-all had one legitimate job. Keep it, narrowed to the
        # paths that need it, and temporary rather than permanent.
        status, location = _http_get(live_server, path)

        assert status == 302
        assert location == '/trawlarr/ui/dashboard/'
