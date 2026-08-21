import threading
import time

from app.providers.concurrency import run_bounded


def test_run_bounded_preserves_order():
    tasks = [lambda i=i: i for i in range(10)]
    results = run_bounded(tasks, limit=3)
    assert results == list(range(10))


def test_run_bounded_respects_concurrency_limit():
    in_flight = {"current": 0, "max_seen": 0}
    lock = threading.Lock()

    def task():
        with lock:
            in_flight["current"] += 1
            in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        time.sleep(0.05)
        with lock:
            in_flight["current"] -= 1
        return True

    run_bounded([task] * 10, limit=2)
    assert in_flight["max_seen"] <= 2


def test_run_bounded_empty_tasks_returns_empty_list():
    assert run_bounded([], limit=5) == []


def test_run_bounded_single_task_no_pool_overhead_issue():
    assert run_bounded([lambda: "x"], limit=5) == ["x"]
