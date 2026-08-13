# CrSDKPy

CrSDKPy is intended to become a cross-platform Python interface/wrapper for
Sony Camera Remote SDK (CRSDK). It is an independent community project and is
currently at a very early bootstrap stage: version `0.0.1a1` exposes only the
package version and does not yet provide camera-control functionality.

## Sony Camera Remote SDK dependency

Sony Camera Remote SDK is an external dependency. Users are responsible for
obtaining it from Sony, accepting its terms, and supplying an appropriate SDK
installation for their platform when future CrSDKPy versions require it.

This repository and its distributions do **not** redistribute Sony headers,
libraries, DLLs, samples, documentation, or any other Sony SDK files.

CrSDKPy is not affiliated with, endorsed by, or sponsored by Sony Corporation
or any of its affiliates. Sony and Camera Remote SDK are trademarks or names
of their respective owners.

## Installation

```console
python -m pip install CrSDKPy
```

The bootstrap release can be inspected with:

```python
import crsdkpy

print(crsdkpy.__version__)
```

## Development

```console
python -m venv .venv
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m build
```

## License

CrSDKPy is distributed under the MIT License. See [LICENSE](LICENSE).
