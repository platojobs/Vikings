"""登录失败限流：进程内按「客户端 IP + 账号」计数，达到阈值后短暂锁定。

实现取舍：
- 内存字典 + 单调时钟，零外部依赖，适合单进程网关（本项目默认部署形态）；
- 多进程 / 多副本部署时各进程计数独立，实际阈值会被放大，且重启后计数清零；
  生产若需要全局一致的限流，应在反向代理层（如 nginx `limit_req`）或 Redis 中实现。
- 不读取 `X-Forwarded-For`：默认不信任客户端可伪造的请求头；若网关在可信反代之后，
  需要在反代层做限流，或显式配置可信代理后再取真实 IP。
"""

import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

AttemptKey = Tuple[str, str]


@dataclass
class _AttemptState:
    failures: int = 0
    window_started_at: float = 0.0
    locked_until: float = 0.0


class LoginRateLimiter:
    """固定窗口 + 锁定的失败计数；成功登录由调用方主动 reset。"""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 300,
        lockout_seconds: int = 900,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max(1, max_failures)
        self.window_seconds = max(1, window_seconds)
        self.lockout_seconds = max(1, lockout_seconds)
        self._clock = clock
        self._states: Dict[AttemptKey, _AttemptState] = {}

    def retry_after(self, key: AttemptKey) -> int:
        """返回剩余锁定秒数；0 表示当前可尝试登录。"""
        state = self._states.get(key)
        if state is None:
            return 0
        now = self._clock()
        if state.locked_until > now:
            return max(1, math.ceil(state.locked_until - now))
        # 锁定已结束且窗口也过期时，顺手移除，避免字典无限增长
        if now - state.window_started_at >= self.window_seconds:
            self._states.pop(key, None)
        return 0

    def record_failure(self, key: AttemptKey) -> None:
        state = self._states.get(key)
        now = self._clock()
        if state is None or now - state.window_started_at >= self.window_seconds:
            state = _AttemptState(failures=0, window_started_at=now)
            self._states[key] = state
        state.failures += 1
        if state.failures >= self.max_failures:
            state.locked_until = now + self.lockout_seconds

    def reset(self, key: AttemptKey) -> None:
        self._states.pop(key, None)

    def clear(self) -> None:
        """测试与热加载使用；线上清空会立即解除所有锁定。"""
        self._states.clear()

    def failure_count(self, key: AttemptKey) -> int:
        state = self._states.get(key)
        return state.failures if state else 0

    def locked(self, key: AttemptKey) -> bool:
        state = self._states.get(key)
        return bool(state and state.locked_until > self._clock())


__all__ = ["LoginRateLimiter", "AttemptKey"]
