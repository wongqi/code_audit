"""Cooperative cancellation; close active connections before releasing the task lock."""

import threading


class CollectionCancelled(Exception):
    pass


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.callbacks = set()

    def check(self):
        if self.event.is_set():
            raise CollectionCancelled("采集已停止")

    def watch(self, close):
        with self.lock:
            cancelled = self.event.is_set()
            if not cancelled:
                self.callbacks.add(close)
        if cancelled:
            close()
            self.check()

        def remove():
            with self.lock:
                self.callbacks.discard(close)

        return remove

    def cancel(self):
        with self.lock:
            self.event.set()
            callbacks = list(self.callbacks)
            self.callbacks.clear()
        for close in callbacks:
            try:
                close()
            except Exception:
                pass
