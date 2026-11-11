import hashlib
import importlib.util
from pathlib import Path

import pytest


def verifier():
    spec = importlib.util.spec_from_file_location(
        "release_assets", Path(__file__).with_name("fetch_release_assets.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify


def fixtures(directory):
    names = ["crsdkpy-0.1.0b4-py3-none-any.whl", "crsdkpy-0.1.0b4.tar.gz"]
    for name in names:
        (directory / name).write_bytes(name.encode())
    (directory / "SHA256SUMS").write_text("\n".join(
        f"{hashlib.sha256(name.encode()).hexdigest()}  {name}" for name in names
    ))
    return names


def test_exact_distributions_verified(tmp_path):
    fixtures(tmp_path)
    verifier()(tmp_path, "0.1.0b4")
    assert not (tmp_path / "SHA256SUMS").exists()


def test_changed_distribution_refused(tmp_path):
    names = fixtures(tmp_path)
    (tmp_path / names[0]).write_bytes(b"different build")
    with pytest.raises(ValueError, match="checksum mismatch"):
        verifier()(tmp_path, "0.1.0b4")


@pytest.mark.parametrize("contents", ["", "abc ../outside", "abc x\nabc x"])
def test_incomplete_or_unexpected_checksums_refused(tmp_path, contents):
    (tmp_path / "SHA256SUMS").write_text(contents)
    with pytest.raises(ValueError):
        verifier()(tmp_path, "0.1.0b4")
