"""Stand-in for Webots' `controller` module, used only by the end-to-end tests.

It follows the documented pause semantics: after simulationSetMode(PAUSE),
step(0) returns immediately but step(dt) would block forever -- so the stub
raises instead, which makes a deadlock bug in the supervisor fail loudly.
Anything it does not implement returns a MagicMock.
"""
import os
import time
from unittest.mock import MagicMock

_STATE_DIR = os.environ.get("FAKE_WEBOTS_STATE_DIR", ".")


class Supervisor:
    SIMULATION_MODE_PAUSE = 0
    SIMULATION_MODE_REAL_TIME = 1
    SIMULATION_MODE_FAST = 2

    def __init__(self):
        self._time = 0.0
        self._mode = self.SIMULATION_MODE_PAUSE
        self._pending = None
        self._reset = False
        self._end = False

    # --- implemented semantics ---
    def getBasicTimeStep(self):
        return 16.0

    def getTime(self):
        return self._time

    def getWorldPath(self):
        return "/fake/worlds/test.wbt"

    def getProjectPath(self):
        return "/fake"

    def simulationSetMode(self, mode):
        self._pending = mode

    def simulationGetMode(self):
        return self._mode

    def simulationReset(self):
        self._reset = True

    def simulationResetPhysics(self):
        pass

    def worldReload(self):
        open(os.path.join(_STATE_DIR, "reload.flag"), "w").close()
        self._end = True

    def simulationQuit(self, status):
        open(os.path.join(_STATE_DIR, "quit.flag"), "w").close()
        self._end = True

    def step(self, ms):
        if self._end:
            return -1
        if self._pending is not None:
            self._mode, self._pending = self._pending, None
        if ms == 0:
            return 0
        if self._mode == self.SIMULATION_MODE_PAUSE:
            raise RuntimeError("DEADLOCK: step(%d) while paused" % ms)
        self._time += ms / 1000.0
        time.sleep(ms / 1000.0 if self._mode == self.SIMULATION_MODE_REAL_TIME else 0.0005)
        if self._reset:
            self._time, self._reset = 0.0, False
        return -1 if self._end else 0

    # --- everything else: a permissive mock ---
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        mock = MagicMock(name=name)
        setattr(self, name, mock)
        return mock
