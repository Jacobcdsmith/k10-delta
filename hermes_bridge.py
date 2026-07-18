import subprocess
import logging
import threading
import time

log = logging.getLogger("k10d.hermes")

class HermesBridge:
    def __init__(self):
        self.available = self._check_availability()
        self._active_proc = None
        self._last_query: dict = {}
        self._lock = threading.Lock()

    def _check_availability(self):
        try:
            # Check if hermes is in path and working
            res = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                log.info("Hermes Bridge connected: %s", res.stdout.strip())
                return True
        except Exception as e:
            log.warning("Hermes not found or failed to run: %s", e)
        return False

    def status(self) -> dict:
        with self._lock:
            proc = self._active_proc
            last = dict(self._last_query)
        in_flight = proc is not None and proc.poll() is None
        preview = (last.get("prompt") or "")[:80]
        return {
            "available": self.available,
            "in_flight": in_flight,
            "last_query_preview": preview,
            "last_duration_ms": last.get("duration_ms"),
        }

    def cancel(self) -> dict:
        with self._lock:
            proc = self._active_proc
        if proc is None or proc.poll() is not None:
            return {"ok": True, "cancelled": False}
        try:
            proc.kill()
            proc.wait(timeout=5)
            log.info("Cancelled in-flight Hermes query")
            return {"ok": True, "cancelled": True}
        except Exception as e:
            log.error("Failed to cancel Hermes query: %s", e)
            return {"ok": False, "cancelled": False, "error": str(e)}
        finally:
            with self._lock:
                if self._active_proc is proc:
                    self._active_proc = None

    def query(self, prompt: str, timeout: int = 120) -> dict:
        if not self.available:
            return {"success": False, "error": "Hermes is not available on this system."}

        log.info("Delegating query to Hermes (timeout=%d): %s", timeout, prompt[:50] + "..." if len(prompt) > 50 else prompt)
        started = time.time()
        with self._lock:
            self._last_query = {"prompt": prompt, "started_at": started, "timeout": timeout}
        proc = subprocess.Popen(
            ["hermes", "-z", prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._lock:
            self._active_proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            duration_ms = round((time.time() - started) * 1000, 1)
            with self._lock:
                self._last_query["duration_ms"] = duration_ms
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    log.error("Hermes process unresponsive after kill+terminate")
            log.error("Hermes query timed out after %d seconds", timeout)
            with self._lock:
                self._last_query["duration_ms"] = round((time.time() - started) * 1000, 1)
            return {"success": False, "error": f"Hermes query timed out after {timeout} seconds."}
        except Exception as e:
            log.error("Hermes query failed: %s", e)
            with self._lock:
                self._last_query["duration_ms"] = round((time.time() - started) * 1000, 1)
            return {"success": False, "error": str(e)}
        finally:
            with self._lock:
                if self._active_proc is proc:
                    self._active_proc = None