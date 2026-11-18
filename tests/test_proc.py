import subprocess
import sys

from clipper.util.proc import no_window


def test_no_window_flags() -> None:
    if sys.platform == "win32":
        assert no_window() == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert no_window() == {}
