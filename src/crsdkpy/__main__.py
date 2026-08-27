"""``python -m crsdkpy`` -- report what is installed and what is missing.

The shortest thing a user can run when something will not start. It loads no
vendor code, so it works when the vendor runtime is exactly the problem.
"""

from __future__ import annotations

import sys

from .diagnostics import main

if __name__ == "__main__":
    sys.exit(main())
