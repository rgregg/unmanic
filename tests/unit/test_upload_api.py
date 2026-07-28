import asyncio
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from unmanic.libs.task import TaskCreationOwnershipUncertain
from unmanic.webserver.api_v2.base_api_handler import (
    ApiErrorCode,
    BaseApiError,
)
from unmanic.webserver.api_v2.upload_api import ApiUploadHandler


def _set_owned_upload(handler, cache_directory, upload_path):
    directory_stat = cache_directory.stat()
    handler.cache_directory = str(cache_directory)
    handler._owned_cache_directory = str(cache_directory.resolve())
    handler._cache_directory_identity = (
        directory_stat.st_dev, directory_stat.st_ino)
    handler._owns_cache_directory = True
    handler._active_upload_marker_fd = None
    handler._request_cleanup_complete = False
    handler._preserve_pending_upload = False
    handler._upload_error = None
    handler.meta['pathname'] = str(upload_path)


def test_upload_returns_stable_error_and_cleans_files_when_remote_task_creation_fails(
        monkeypatch, tmp_path):
    cache_directory = tmp_path / 'remote_library' / 'unmanic_remote_pending_library-request'
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    upload_path.write_bytes(b'uploaded')
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {
        'header': b'header',
        'boundary': b'boundary',
        'filename': 'video.mkv',
    }
    handler.request = SimpleNamespace(headers={
        'Content-Length': str(len(b'header') + len(b'boundary') + 10),
    })
    handler.fp = mock.Mock()
    _set_owned_upload(handler, cache_directory, upload_path)
    handler.frontend_messages = mock.Mock()
    handler.set_status = mock.Mock()
    handler.write_error = mock.Mock()
    handler.write_success = mock.Mock()
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.pending_tasks.add_remote_tasks',
        mock.Mock(return_value=None),
    )
    checksum = mock.Mock()
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.common.get_file_checksum',
        checksum,
    )

    asyncio.run(handler.upload_file_to_pending_tasks())

    handler.set_status.assert_called_once_with(
        handler.STATUS_ERROR_INTERNAL,
        reason='Failed to add uploaded file to pending tasks',
    )
    handler.write_error.assert_called_once_with()
    handler.write_success.assert_not_called()
    checksum.assert_not_called()
    handler.frontend_messages.remove_item.assert_called_once_with(
        'receivingRemoteFile',
    )
    assert not upload_path.exists()
    assert not cache_directory.exists()
    assert cache_directory.parent.exists()


def test_upload_cleans_files_when_remote_task_creation_raises(monkeypatch, tmp_path):
    cache_directory = tmp_path / 'remote_library' / 'unmanic_remote_pending_library-request'
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    upload_path.write_bytes(b'uploaded-data')
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {
        'header': b'header',
        'boundary': b'boundary',
        'filename': upload_path.name,
    }
    handler.request = SimpleNamespace(headers={
        'Content-Length': str(len(b'header') + len(b'boundary') + upload_path.stat().st_size),
    })
    handler.fp = upload_path.open('r+b')
    _set_owned_upload(handler, cache_directory, upload_path)
    handler.frontend_messages = mock.Mock()
    handler.set_status = mock.Mock()
    handler.write_error = mock.Mock()
    handler.write_success = mock.Mock()
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.pending_tasks.add_remote_tasks',
        mock.Mock(side_effect=RuntimeError('registration failed')),
    )

    asyncio.run(handler.upload_file_to_pending_tasks())

    assert handler.fp.closed
    assert not upload_path.exists()
    assert not cache_directory.exists()
    assert cache_directory.parent.exists()
    handler.set_status.assert_called_once_with(
        handler.STATUS_ERROR_INTERNAL,
        reason='registration failed',
    )
    handler.write_error.assert_called_once_with()
    handler.write_success.assert_not_called()


def test_upload_does_not_clean_registered_task_after_later_exception(monkeypatch, tmp_path):
    cache_directory = tmp_path / 'remote_library' / 'unmanic_remote_pending_library-request'
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    upload_path.write_bytes(b'uploaded-data')
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {
        'header': b'header',
        'boundary': b'boundary',
        'filename': upload_path.name,
    }
    handler.request = SimpleNamespace(headers={
        'Content-Length': str(len(b'header') + len(b'boundary') + upload_path.stat().st_size),
    })
    handler.fp = upload_path.open('r+b')
    _set_owned_upload(handler, cache_directory, upload_path)
    handler.frontend_messages = mock.Mock()
    handler.set_status = mock.Mock()
    handler.write_error = mock.Mock()
    handler.write_success = mock.Mock()
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.pending_tasks.add_remote_tasks',
        mock.Mock(return_value={'abspath': str(upload_path)}),
    )
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.common.get_file_checksum',
        mock.Mock(side_effect=RuntimeError('checksum failed')),
    )

    asyncio.run(handler.upload_file_to_pending_tasks())

    assert upload_path.exists()
    assert cache_directory.exists()
    handler.write_error.assert_called_once_with()
    handler.write_success.assert_not_called()


def test_upload_preserves_file_when_task_ownership_is_uncertain(monkeypatch, tmp_path):
    cache_directory = tmp_path / 'remote_library' / 'unmanic_remote_pending_library-request'
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    upload_path.write_bytes(b'uploaded-data')
    os.utime(cache_directory, (1, 1))
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {
        'header': b'header',
        'boundary': b'boundary',
        'filename': upload_path.name,
    }
    handler.request = SimpleNamespace(headers={
        'Content-Length': str(len(b'header') + len(b'boundary') + upload_path.stat().st_size),
    })
    handler.fp = upload_path.open('r+b')
    _set_owned_upload(handler, cache_directory, upload_path)
    handler.frontend_messages = mock.Mock()
    handler.set_status = mock.Mock()
    handler.write_error = mock.Mock()
    handler.write_success = mock.Mock()
    monkeypatch.setattr(
        'unmanic.webserver.api_v2.upload_api.pending_tasks.add_remote_tasks',
        mock.Mock(side_effect=TaskCreationOwnershipUncertain('insert timed out')),
    )

    asyncio.run(handler.upload_file_to_pending_tasks())

    assert handler.fp.closed
    assert upload_path.exists()
    assert cache_directory.exists()
    assert cache_directory.stat().st_mtime > 1
    handler.set_status.assert_called_once_with(
        handler.STATUS_ERROR_INTERNAL,
        reason='Failed to add uploaded file to pending tasks',
    )
    handler.write_error.assert_called_once_with()
    handler.write_success.assert_not_called()


@pytest.mark.parametrize('filename', [
    '../outside.mkv',
    '..\\outside.mkv',
    '/outside.mkv',
    'C:\\outside.mkv',
    'C:outside.mkv',
    '.',
    '..',
    'bad\x00name.mkv',
])
def test_unsafe_upload_filename_is_rejected_before_open(
        filename, tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    outside = tmp_path / 'outside.mkv'
    outside.write_bytes(b'outside')
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(
        handler, cache_directory, cache_directory / 'placeholder.mkv')

    with pytest.raises(BaseApiError) as exc_info:
        handler._open_upload_file(filename)

    error = exc_info.value
    assert error.status_code == 400
    assert error.error_code == ApiErrorCode.VALIDATION_ERROR
    assert list(error.messages) == ['filename']
    assert outside.read_bytes() == b'outside'
    assert set(path.name for path in cache_directory.iterdir()) == set()


def test_upload_rejects_symlink_destination_without_changing_target(tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    outside = tmp_path / 'outside.mkv'
    outside.write_bytes(b'outside')
    upload_path = cache_directory / 'video.mkv'
    upload_path.symlink_to(outside)
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(handler, cache_directory, upload_path)

    with pytest.raises(BaseApiError) as exc_info:
        handler._open_upload_file(upload_path.name)

    assert exc_info.value.status_code == 400
    assert outside.read_bytes() == b'outside'
    assert upload_path.is_symlink()


@pytest.mark.parametrize('path_kind', ['absolute', 'traversal'])
def test_outside_target_is_unchanged_for_escaping_filename(
        path_kind, tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    outside = tmp_path / 'outside.mkv'
    outside.write_bytes(b'outside')
    filename = (
        str(outside)
        if path_kind == 'absolute'
        else os.path.relpath(outside, cache_directory)
    )
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(
        handler, cache_directory, cache_directory / 'placeholder.mkv')

    with pytest.raises(BaseApiError):
        handler._open_upload_file(filename)

    assert outside.read_bytes() == b'outside'
    assert list(cache_directory.iterdir()) == []


def test_upload_rejects_duplicate_without_overwriting(tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    upload_path.write_bytes(b'existing')
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(handler, cache_directory, upload_path)

    with pytest.raises(BaseApiError) as exc_info:
        handler._open_upload_file(upload_path.name)

    assert exc_info.value.messages == {
        'filename': ['A file with this name already exists.'],
    }
    assert upload_path.read_bytes() == b'existing'


def test_data_json_upload_is_allowed_and_metadata_directory_is_reserved(
        tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(
        handler, cache_directory, cache_directory / 'placeholder')

    handler._open_upload_file('data.json')
    handler.fp.write(b'legitimate-output')
    handler.fp.close()

    assert (cache_directory / 'data.json').read_bytes() == b'legitimate-output'
    with pytest.raises(BaseApiError) as exc_info:
        handler._open_upload_file('.unmanic')
    assert exc_info.value.status_code == 400
    assert exc_info.value.messages == {
        'filename': ['This filename is reserved for remote task metadata.'],
    }
    assert (cache_directory / 'data.json').read_bytes() == b'legitimate-output'


def test_active_marker_spans_stream_until_registered_request_finishes(tmp_path):
    cache_directory = (
        tmp_path / 'cache' / 'remote_library'
        / 'unmanic_remote_pending_library-request')
    cache_directory.mkdir(parents=True)
    upload_path = cache_directory / 'video.mkv'
    handler = ApiUploadHandler.__new__(ApiUploadHandler)
    handler.meta = {}
    _set_owned_upload(handler, cache_directory, upload_path)
    handler.meta.pop('pathname')
    handler.frontend_messages = None
    handler._create_active_upload_marker()
    marker = cache_directory / '.upload-active'
    receiver = handler.get_receiver('pending')
    body = (
        b'--boundary\r\n'
        b'Content-Disposition: form-data; name="file"; filename="video.mkv"\r\n'
        b'Content-Type: video/x-matroska\r\n\r\n'
        b'content'
    )

    assert marker.exists()
    receiver(body)
    assert marker.exists()
    assert not handler.fp.closed

    handler._preserve_pending_upload = True
    handler.on_finish()

    assert not marker.exists()
    assert upload_path.read_bytes() == b'content'
    assert handler.fp.closed
