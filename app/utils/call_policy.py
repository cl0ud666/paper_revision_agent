"""可选的运行级调用策略；CLI 无策略，Web Worker 注入额度和全局限流。"""

from contextlib import contextmanager
from threading import Lock

_policies = {}
_lock = Lock()


def get_policy(run_id):
    with _lock:
        return _policies.get(run_id)


@contextmanager
def install_policy(run_id, policy):
    with _lock:
        if run_id in _policies:
            raise RuntimeError("同一运行不能同时安装两个模型调用策略")
        _policies[run_id] = policy
    try:
        yield
    finally:
        with _lock:
            _policies.pop(run_id, None)
