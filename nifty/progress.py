"""One line per finished piece of work: how far along, what it has cost, how long is left."""
import threading
import time
from contextlib import contextmanager

SAVED_SECONDS = 0.2  # quicker than any model: the result was already on disk


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class Progress:
    """Results already saved finish without a line of their own, so a resumed run does not scroll
    through what it has done. The estimate counts only pieces that did real work."""

    def __init__(self, stage: str, total: int, client):
        self.stage, self.total, self.client = stage, total, client
        self.done = self.saved = 0
        self.lock = threading.Lock()
        self.started = None
        print(f"[{stage}] {total} to do", flush=True)

    def line(self, label: str) -> str:
        percent = self.done * 100 // self.total if self.total else 100
        parts = [f"[{self.stage}] {self.done}/{self.total} ({percent}%) {label}", f"${self.client.spent:.2f} so far"]
        worked = self.done - self.saved
        if worked and self.started and self.done < self.total:
            elapsed = time.monotonic() - self.started
            parts.append(f"about {duration(elapsed / worked * (self.total - self.done))} left")
        if self.saved:
            parts.append(f"{self.saved} already saved")
        return " · ".join(parts)

    @contextmanager
    def item(self, label: str):
        began = time.monotonic()
        yield
        with self.lock:
            self.done += 1
            if time.monotonic() - began < SAVED_SECONDS:
                self.saved += 1
                if self.done < self.total:
                    return
            elif self.started is None:
                self.started = began
            print(self.line(label), flush=True)


@contextmanager
def single(stage: str, what: str, client):
    """For one long call, which would otherwise look like a hang."""
    began = time.monotonic()
    print(f"[{stage}] {what}...", flush=True)
    yield
    print(f"[{stage}] done in {duration(time.monotonic() - began)} · ${client.spent:.2f} so far", flush=True)
