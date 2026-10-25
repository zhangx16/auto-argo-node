"""Shared panel-side client budget; no resident clients on node servers."""
from contextlib import contextmanager
import threading

SLOTS=threading.BoundedSemaphore(4)
LOCK=threading.Lock()
ACTIVE=0


class Deferred(Exception):pass


@contextmanager
def slot(timeout=15):
    global ACTIVE
    if not SLOTS.acquire(timeout=timeout):raise Deferred('检测客户端繁忙，等待下一轮调度')
    with LOCK:ACTIVE+=1
    try:yield
    finally:
        with LOCK:ACTIVE-=1
        SLOTS.release()


def status():
    with LOCK:return {'active':ACTIVE,'limit':4}
