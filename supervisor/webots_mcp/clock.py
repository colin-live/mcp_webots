"""Simulation clock: the only place that decides whether the world advances.

The Rust host owns the *policy* (start / pause / step / reset requests); this
module executes the matching Supervisor calls on the Webots main thread.

Why a clock at all: Webots only advances when every synchronized controller
calls ``step()``.  After ``simulationSetMode(PAUSE)`` a ``step(dt)`` blocks until
the simulation is resumed, but ``step(0)`` returns at once and still lets the
Supervisor modify the scene (Webots reference, ``wb_supervisor_simulation_set_mode``).
So while paused the main loop calls ``step(0)``: scene tools keep working, and
the host can pause / resume / single-step without the controller deadlocking.
"""
import queue
import threading
import time
from concurrent.futures import Future

PAUSED = "paused"
RUNNING = "running"
STEPPING = "stepping"

MAX_STEP_COUNT = 1_000_000


class Clock:
    def __init__(self, supervisor, time_step_ms, log, on_reset=None,
                 idle_sleep=0.005, sync_gui=False):
        self.sup = supervisor
        self.dt = int(time_step_ms)
        self.log = log
        self.on_reset = on_reset
        self.idle_sleep = idle_sleep
        self.sync_gui = sync_gui

        self.state = PAUSED
        self.mode = "pause"            # last mode requested from Webots
        self.run_mode = "real_time"    # mode used when running
        self.steps_remaining = 0
        self.steps_total = 0
        self.on_state = None           # callback(snapshot) -> None

        self._ops = queue.Queue()
        self._lock = threading.Lock()
        self._snap = self._build_snapshot()
        self._last_published = None
        self._step_future = None
        self._ignore_sync = 0
        # Armed by resume(pause_on_task_done=True): pause in the very step an
        # observation task finishes, so a fast run_simulation cannot overshoot.
        self._pause_on_task_done = False
        self._task_seen = False

    # ---------------- thread-safe API ----------------

    def submit(self, op, params=None):
        """Queue a control op; the returned Future completes on the main thread."""
        fut = Future()
        self._ops.put((op, params or {}, fut))
        return fut

    def snapshot(self):
        with self._lock:
            return dict(self._snap)

    # ---------------- main thread ----------------

    def start(self):
        """Pause the world right away; returns False if Webots is shutting down."""
        self._set_mode("pause")
        self.state = PAUSED
        self._publish()
        return self.sup.step(0) != -1

    def tick(self):
        """One main-loop iteration. Returns False when Webots asks to stop."""
        self._drain_ops()
        if self.sync_gui:
            self._sync_from_gui()

        if self.state == PAUSED:
            if self.sup.step(0) == -1:
                return False
            time.sleep(self.idle_sleep)
        else:
            if self.sup.step(self.dt) == -1:
                return False
            if self._ignore_sync:
                self._ignore_sync -= 1
            if self.state == STEPPING:
                self.steps_remaining -= 1
                if self.steps_remaining <= 0:
                    self._finish_step()

        self._publish()
        return True

    # ---------------- internals ----------------

    def _const(self, mode):
        names = {
            "pause": ("SIMULATION_MODE_PAUSE",),
            "real_time": ("SIMULATION_MODE_REAL_TIME", "SIMULATION_MODE_RUN"),
            "fast": ("SIMULATION_MODE_FAST",),
        }
        for attr in names[mode]:
            value = getattr(self.sup, attr, None)
            if value is not None:
                return value
        raise RuntimeError(f"Simulation mode {mode!r} is not supported by this Webots version")

    def _set_mode(self, mode):
        self.sup.simulationSetMode(self._const(mode))
        self.mode = mode
        self._ignore_sync = 3

    def _status(self, **extra):
        data = self._build_snapshot()
        data.update(extra)
        return data

    def _build_snapshot(self):
        return {
            "state": self.state,
            "mode": self.mode,
            "sim_time": self.sup.getTime(),
            "time_step_ms": self.dt,
            "steps_remaining": self.steps_remaining,
        }

    def _publish(self):
        snap = self._build_snapshot()
        with self._lock:
            self._snap = snap
        key = (snap["state"], snap["mode"])
        if key != self._last_published:
            self._last_published = key
            if self.on_state is not None:
                try:
                    self.on_state(dict(snap))
                except Exception:
                    self.log.debug("on_state callback failed", exc_info=True)

    def _sync_from_gui(self):
        """Follow Play/Pause clicked in the Webots GUI (opt-in, see config)."""
        if self._ignore_sync > 0 or self.state == STEPPING:
            return
        observed = self.sup.simulationGetMode()
        pause = self._const("pause")
        if self.state == PAUSED and observed != pause:
            self.log.info("Webots GUI resumed the simulation")
            self.state, self.mode = RUNNING, "real_time"
        elif self.state == RUNNING and observed == pause:
            self.log.info("Webots GUI paused the simulation")
            self.state, self.mode = PAUSED, "pause"

    def _drain_ops(self):
        while True:
            try:
                op, params, fut = self._ops.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                handler = getattr(self, f"_op_{op}", None)
                if handler is None:
                    raise ValueError(f"unknown sim.control op: {op!r}")
                result = handler(params, fut)
                if result is not None:
                    fut.set_result(result)
            except Exception as exc:
                self.log.warning("sim.control %s failed: %s", op, exc)
                if not fut.done():
                    fut.set_exception(exc)

    @staticmethod
    def _mode_arg(params, default):
        mode = params.get("mode") or default
        if mode not in ("real_time", "fast"):
            raise ValueError("mode must be 'real_time' or 'fast'")
        return mode

    # ---------------- ops (all run on the main thread) ----------------

    def _op_status(self, params, fut):
        return self._status()

    def _op_pause(self, params, fut):
        self._set_mode("pause")
        self.state = PAUSED
        self._pause_on_task_done = False
        self._abort_step("interrupted by pause")
        return self._status()

    def _op_resume(self, params, fut):
        if self.state == STEPPING:
            raise RuntimeError("a sim.step is in progress; wait for it or call pause")
        mode = self._mode_arg(params, self.run_mode)
        self.run_mode = mode
        self._set_mode(mode)
        self.state = RUNNING
        self._pause_on_task_done = bool(params.get("pause_on_task_done"))
        self._task_seen = False
        return self._status()

    def after_tick(self, task_running):
        """Main loop calls this after update_simulation_task().

        If armed (resume with pause_on_task_done) and the observation task that
        started has now finished, pause immediately.
        """
        if not self._pause_on_task_done:
            return
        if task_running:
            self._task_seen = True
        elif self._task_seen:
            self.log.info("observation task finished: pausing the simulation")
            self._set_mode("pause")
            self.state = PAUSED
            self._pause_on_task_done = False
            self._publish()

    def _op_step(self, params, fut):
        count = params.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) \
                or not 1 <= count <= MAX_STEP_COUNT:
            raise ValueError(f"count must be an integer in [1, {MAX_STEP_COUNT}]")
        if self.state != PAUSED:
            raise RuntimeError("sim.step needs a paused simulation; call pause first")
        mode = self._mode_arg(params, "fast")
        self._set_mode(mode)
        self.state = STEPPING
        self.steps_total = self.steps_remaining = count
        self._step_future = fut
        return None  # resolved by _finish_step

    def _finish_step(self):
        self._set_mode("pause")
        self.state = PAUSED
        done, fut = self.steps_total, self._step_future
        self._step_future = None
        self.steps_remaining = 0
        if fut is not None and not fut.done():
            fut.set_result(self._status(steps_done=done))

    def _abort_step(self, why):
        fut, self._step_future = self._step_future, None
        self.steps_remaining = 0
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError(why))

    def _op_reset_physics(self, params, fut):
        self.sup.simulationResetPhysics()
        return self._status()

    def _op_reset(self, params, fut):
        """Reset time and scene to the saved world; controllers keep running.

        Webots applies the reset at the end of a step, so one step is run (the
        world is un-paused for exactly that step) and the world is paused again.
        Nodes created at run time are removed.
        """
        if self.state == STEPPING:
            raise RuntimeError("a sim.step is in progress; call pause first")
        self.sup.simulationReset()
        self._set_mode("fast")
        if self.sup.step(self.dt) == -1:
            raise RuntimeError("Webots is shutting down")
        if self.on_reset is not None:
            self.on_reset()
        self._set_mode("pause")
        self.state = PAUSED
        if self.sup.step(0) == -1:
            raise RuntimeError("Webots is shutting down")
        return self._status(note="runtime-created nodes were removed by the reset")

    def _op_reload(self, params, fut):
        """Reload the world: Webots kills and restarts this controller."""
        fut.set_result(self._status(note="reloading; the supervisor will reconnect"))
        time.sleep(0.1)  # let the IPC thread flush the reply
        self.sup.worldReload()
        return None

    def _op_quit(self, params, fut):
        fut.set_result(self._status(note="quitting Webots"))
        time.sleep(0.1)
        self.sup.simulationQuit(0)
        return None
