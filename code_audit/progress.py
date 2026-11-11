"""Synchronized progress on stderr; results remain on stdout."""

import sys
import threading
import time

from .control import Cancellation
from .storage import redact


class Progress:
    def __init__(self, secrets=(), quiet=False, stream=None, interval=5):
        self.cancellation = Cancellation()
        self.secrets, self.quiet = secrets, quiet
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.lock = threading.RLock()
        self.active = {}
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _line(self, label, message):
        # JSON-encode control characters so remote paths cannot inject log lines.
        text = str(redact("[" + str(label) + "] " + str(message), self.secrets))
        text = "".join(
            ch if ord(ch) >= 32 and ord(ch) != 127 else repr(ch)[1:-1] for ch in text
        )
        self.stream.write(text + "\n")
        self.stream.flush()

    def __call__(self, label, message, error=False):
        with self.lock:
            self.active[label] = (str(message), time.monotonic(), time.monotonic())
            if not self.quiet or error:
                self._line(label, message)

    def prompt(self, message):
        """Keep progress output and the terminal quiet until input is complete."""
        if not sys.stdin.isatty():
            raise RuntimeError("未知主机密钥需在交互终端确认，或在网页中确认")
        with self.lock:
            started = time.monotonic()
            try:
                self.stream.write(str(redact(message, self.secrets)))
                self.stream.flush()
                return input()
            finally:
                self.stream.write("\n")
                self.stream.flush()
                elapsed = time.monotonic() - started
                for label, (text, began, last) in list(self.active.items()):
                    self.active[label] = (text, began + elapsed, last + elapsed)

    def done(self, label):
        with self.lock:
            self.active.pop(label, None)

    def _heartbeat(self):
        while not self.stop.wait(min(0.25, self.interval)):
            with self.lock:
                for label, (message, started, last) in list(self.active.items()):
                    current = time.monotonic()
                    if not self.quiet and current - last >= self.interval:
                        self._line(
                            label,
                            message
                            + "；已等待 "
                            + str(int(current - started))
                            + " 秒……",
                        )
                        self.active[label] = (message, started, current)
