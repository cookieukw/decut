"""Cancellable subprocess ownership shared by the GUI worker and FFmpeg helpers."""
from contextlib import contextmanager
import subprocess
import threading


class AnalysisCancelled(Exception):
    pass


class JobControl:
    def __init__(self, progress=None):
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes = set()
        self._progress = progress

    def check(self):
        if self._cancelled.is_set():
            raise AnalysisCancelled("Analysis cancelled.")

    def report(self, stage, done=0, total=0):
        self.check()
        if self._progress:
            self._progress(stage, done, total)

    def attach(self, process):
        with self._lock:
            self._processes.add(process)
            if self._cancelled.is_set() and process.poll() is None:
                process.kill()
        self.check()

    def detach(self, process):
        with self._lock:
            self._processes.discard(process)

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            for process in self._processes:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass


@contextmanager
def managed_process(command, control=None, **kwargs):
    if control:
        control.check()
    process = subprocess.Popen(command, **kwargs)
    try:
        if control:
            control.attach(process)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        if control:
            control.detach(process)
