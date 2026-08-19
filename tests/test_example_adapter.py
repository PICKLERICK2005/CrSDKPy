"""The integration example must keep working.

An example that has quietly stopped running is worse than none: it is what an
integrator copies. This drives it against the simulator and, more importantly,
checks that it needs nothing Sony-specific to do its job.
"""

from __future__ import annotations

import ast
import os
import sys

import pytest

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"
)
sys.path.insert(0, EXAMPLES)

from camera_adapter import SonyCameraAdapter, main  # noqa: E402


def test_the_example_runs(capsys: pytest.CaptureFixture) -> None:
    main()
    output = capsys.readouterr().out
    assert "capabilities:" in output
    assert "battery" in output


def test_the_adapter_captures_and_previews() -> None:
    camera = SonyCameraAdapter("simulator")
    assert camera.connect(prefer_postview=True)
    try:
        shot = camera.autofocus_and_capture()
        assert shot.ok
        assert shot.image and shot.image[:2] == b"\xff\xd8"
        assert shot.width and shot.height
        assert shot.latency_ms is not None
    finally:
        camera.disconnect()


def test_the_adapter_is_idempotent_about_disconnecting() -> None:
    camera = SonyCameraAdapter("simulator")
    camera.connect()
    camera.disconnect()
    camera.disconnect()


def test_the_adapter_mentions_no_vendor_machinery() -> None:
    """The point of the example: an integrator needs none of this.

    Sony's shutter stages, command enumerations, control-mode internals and
    numeric property codes stay behind the library. If one of them appears
    here, the abstraction has a hole in it.
    """
    with open(os.path.join(EXAMPLES, "camera_adapter.py"), encoding="utf-8") as handle:
        source = handle.read()

    # Prose is allowed to name what the code avoids, so comments and
    # docstrings are stripped before looking.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        text = ast.get_docstring(node) if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef)
        ) else None
        if text:
            source = source.replace(text, "")
    source = "\n".join(
        line.split("#", 1)[0] for line in source.splitlines()
    )

    for forbidden in (
        "session.raw",        # the vendor escape hatch
        "S1",                 # half-press stage
        "CommandParameter",   # vendor command parameters
        "Command.",           # vendor command ids
        "0x0",                # any numeric property code
        "remote_transfer",    # control-mode internals
        "_backend.",          # reaching past the public API
        "_id",                # session identifiers are not public
    ):
        assert forbidden not in source, f"the example leaks {forbidden!r}"
