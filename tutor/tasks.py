"""The service's background tasks: the turns, and the clean-up at start.

Lifted from gaggiclanker (the maintainer's project, AGPL like this one),
gaggiclanker/infra/tasks.py (TaskRegistry), with the standard library's logging and without the
cancel-by-name its sync engine needs. A turn is a task of the service, never of the request
that started it: it goes on when the browser goes away. A task that raises is logged with its
traceback instead of dying silently in a future nobody awaits.
"""
import asyncio
import logging

log = logging.getLogger("tutor")


class TaskRegistry:
    def __init__(self):
        self._tasks = {}

    def spawn(self, name, coro):
        """Start `coro` as a named task. A duplicate name is a programming error."""
        if name in self._tasks and not self._tasks[name].done():
            coro.close()
            raise RuntimeError(f"background task {name!r} is already running")
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks[name] = task
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task):
        name = task.get_name()
        # Only if the name still points at this task: a late callback must not release a new claim.
        if self._tasks.get(name) is task:
            del self._tasks[name]
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("background task %s failed", name, exc_info=error)

    async def cancel_all(self, timeout=10.0):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self._tasks.clear()
