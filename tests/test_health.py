import os
import runpy
import time

import pytest

from relaypost.health import clear_readiness, healthy, heartbeat_path, mark_ready


def test_readiness_lifecycle():
    assert not healthy()
    mark_ready()
    assert healthy()
    assert heartbeat_path().stat().st_mode & 0o777 == 0o600
    old = time.time() - 120
    os.utime(heartbeat_path(), (old, old))
    assert not healthy()
    clear_readiness()
    assert not healthy()
    clear_readiness()


@pytest.mark.parametrize("ready,code", [(True, 0), (False, 1)])
def test_health_entrypoint(ready, code):
    if ready:
        mark_ready()
    with pytest.raises(SystemExit) as error:
        runpy.run_path("relaypost/health.py", run_name="__main__")
    assert error.value.code == code
