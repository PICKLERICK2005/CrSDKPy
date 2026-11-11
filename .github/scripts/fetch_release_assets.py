"""Fetch only checksummed Python distributions attached to a published release."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def verify(directory, version):
    expected_names = {
        f"crsdkpy-{version}-py3-none-any.whl", f"crsdkpy-{version}.tar.gz"
    }
    checksums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        if name not in expected_names or name in checksums:
            raise ValueError("unexpected or repeated checksum filename")
        checksums[name] = digest
    if set(checksums) != expected_names:
        raise ValueError("both release distributions require checksums")
    for name, digest in checksums.items():
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"release checksum mismatch: {name}")
    # The checksum manifest is not itself a PyPI distribution.
    (directory / "SHA256SUMS").unlink()


def main():
    url = os.environ["RELEASE_URL"]
    if not url.startswith("https://api.github.com/repos/PICKLERICK2005/CrSDKPy/releases/"):
        raise ValueError("unexpected release endpoint")
    request = Request(url, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(request, timeout=60) as response:
        release = json.load(response)
    tag = os.environ["RELEASE_TAG"]
    if release["tag_name"] != tag or not tag.startswith("v"):
        raise ValueError("release tag mismatch")
    version = tag[1:]
    names = {f"crsdkpy-{version}-py3-none-any.whl",
             f"crsdkpy-{version}.tar.gz", "SHA256SUMS"}
    assets = {asset["name"]: asset for asset in release["assets"]}
    if not names <= assets.keys():
        raise ValueError("release is missing approved distributions/checksums")
    directory = Path("dist")
    directory.mkdir(exist_ok=False)
    for name in sorted(names):
        asset_url = assets[name]["browser_download_url"]
        if not asset_url.startswith(
            "https://github.com/PICKLERICK2005/CrSDKPy/releases/download/"
        ):
            raise ValueError("unexpected release asset URL")
        with urlopen(asset_url, timeout=60) as response:
            (directory / name).write_bytes(response.read())
    verify(directory, version)


if __name__ == "__main__":
    main()
