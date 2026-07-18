import json, time, threading, logging, traceback, textwrap
from pathlib import Path

log = logging.getLogger("k10d.chrono")

class ChronoEngine:
    def __init__(self, schedule_path: Path, soul: dict, state_lock: threading.Lock,
                 globals_dict: dict = None, default_timeout: int = 30):
        self.schedule_path = schedule_path
        self.soul = soul
        self.state_lock = state_lock
        self.globals_dict = globals_dict or {}
        self.default_timeout = default_timeout
        self._lock = threading.Lock()
        with self._lock:
            self.tasks = self._load_schedule()
        self._stop_event = threading.Event()
        self._thread = None

    def _load_schedule(self):
        if self.schedule_path.exists():
            try:
                return json.loads(self.schedule_path.read_text())
            except Exception as e:
                log.error("Failed to load schedule: %s", e)
        return {}

    def _save_schedule(self):
        try:
            self.schedule_path.write_text(json.dumps(self.tasks, indent=2))
        except Exception as e:
            log.error("Failed to save schedule: %s", e)

    def schedule(self, task_id: str, interval: int, code: str, timeout: int = None):
        with self._lock:
            self.tasks[task_id] = {
                "interval": interval,
                "code": code,
                "last_run": 0,
                "timeout": timeout if timeout is not None else self.default_timeout,
            }
            self._save_schedule()
        return {"ok": True, "id": task_id}

    def list_tasks(self):
        with self._lock:
            return dict(self.tasks)

    def remove_task(self, task_id: str):
        with self._lock:
            if task_id in self.tasks:
                del self.tasks[task_id]
                self._save_schedule()
                return {"ok": True}
        return {"ok": False, "error": "Task not found"}

    def reset_last_run(self, ts: float | None = None):
        """Push all task last_run forward so boot does not fire every overdue job."""
        now = ts if ts is not None else time.time()
        with self._lock:
            for task in self.tasks.values():
                task["last_run"] = now
            self._save_schedule()
        log.info("Chrono schedule reset — next runs deferred by interval")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ChronoEngine")
        self._thread.start()
        log.info("ChronoEngine started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join()

    def _run(self):
        while not self._stop_event.is_set():
            now = time.time()
            with self._lock:
                tasks_to_run = list(self.tasks.items())
            
            for task_id, task in tasks_to_run:
                if now - task.get("last_run", 0) >= task["interval"]:
                    self._execute_task(task_id, task)
                    with self._lock:
                        if task_id in self.tasks:
                            self.tasks[task_id]["last_run"] = now
                            self._save_schedule()
            
            self._stop_event.wait(1) # Check every second

    def _execute_task(self, task_id, task):
        log.info("Executing scheduled task: %s", task_id)
        code = textwrap.dedent(task["code"])
        result = [None]
        exc_info = [None]

        def _target():
            try:
                g = self.globals_dict.copy()
                g.update({
                    "soul": self.soul,
                    "state_lock": self.state_lock,
                    "task_id": task_id
                })
                exec(compile(code, f"<chrono-{task_id}>", "exec"), g)
            except Exception as e:
                exc_info[0] = e

        timeout = task.get("timeout", self.default_timeout)
        t = threading.Thread(target=_target, daemon=True, name=f"chrono-{task_id}")
        t.start()
        t.join(timeout)
        if t.is_alive():
            log.error("Task %s timed out after %ds — discarding", task_id, timeout)
        elif exc_info[0] is not None:
            log.error("Error executing task %s:\n%s", task_id, traceback.format_exc())
