from pathlib import Path

import pytest

from crsdkpy import transfers
from test_download_safety import content


@pytest.mark.parametrize("units", [1, 124, 238, 239])
def test_windows_budget_accepts_short_and_boundary(monkeypatch, units):
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    transfers._validate_download_path("x" * units)


@pytest.mark.parametrize("path", ["x" * 240, "x" * 265, "\U0001f4f7" * 120])
def test_windows_budget_rejects_utf16_units(monkeypatch, path):
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    with pytest.raises(ValueError, match="shorter destination directory.*UTF-16"):
        transfers._validate_download_path(path)


def test_non_bmp_boundary_counts_surrogate_pairs(monkeypatch):
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    transfers._validate_download_path("x" + "\U0001f4f7" * 119)


def test_other_platforms_do_not_inherit_windows_budget(monkeypatch):
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", False)
    transfers._validate_download_path("x" * 1000)


@pytest.mark.parametrize("directory_destination", [False, True])
def test_refusal_cleans_staging_without_starting_writer(
    tmp_path, transfer_session, monkeypatch, directory_destination
):
    source = content(transfer_session)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATH_LIMIT", 1)
    calls = []
    monkeypatch.setattr(
        transfer_session._backend, "start_owned_content_download",
        lambda *args: calls.append(args),
    )
    target = tmp_path if directory_destination else tmp_path / "original.ARW"
    with pytest.raises(ValueError, match="no transfer was started"):
        transfer_session.content.download(source, target)
    assert calls == []
    assert list(tmp_path.iterdir()) == []
    assert transfer_session.content._active_download is None


def test_short_path_preserves_normal_publication(
    tmp_path, transfer_session, monkeypatch
):
    source = content(transfer_session)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATH_LIMIT", 1000)
    operation = transfer_session.content.download(source, tmp_path)
    assert operation.wait() == tmp_path / source.filename
    assert operation.successful and not operation._staging_dir.exists()


def test_preflight_cleanup_failure_preserves_refusal(
    tmp_path, transfer_session, monkeypatch
):
    source = content(transfer_session)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATHS", True)
    monkeypatch.setattr(transfers, "_WINDOWS_DOWNLOAD_PATH_LIMIT", 1)
    original = Path.rmdir

    def fail_staging_cleanup(path):
        if path.name.startswith(".crsdkpy-download-"):
            raise PermissionError("synthetic cleanup failure")
        return original(path)

    monkeypatch.setattr(Path, "rmdir", fail_staging_cleanup)
    with pytest.raises(ValueError, match="no transfer was started") as caught:
        transfer_session.content.download(source, tmp_path)
    assert isinstance(caught.value.preflight_cleanup_error, PermissionError)
    assert caught.value.staging_path.is_dir()
    assert transfer_session.content._active_download is None
