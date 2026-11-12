"""Durable dispatch for resumable background tasks."""

import logging
import threading
from typing import Any, Callable, Dict, Optional

from flask import Flask

from ..models.task import TaskCancelledError, TaskManager, TaskStatus

logger = logging.getLogger("mirofish.background_tasks")

TaskHandler = Callable[[Flask, str, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
ResumePredicate = Callable[[Dict[str, Any]], bool]


class BackgroundTaskRunner:
    """Runs persisted tasks and recovers interrupted work when the app starts."""

    def __init__(self, app: Flask):
        self.app = app
        self.task_manager = TaskManager()
        self._handlers: Dict[str, TaskHandler] = {}
        self._resume_predicates: Dict[str, ResumePredicate] = {}
        self._lock = threading.Lock()

    def register(
        self,
        task_type: str,
        handler: TaskHandler,
        can_resume: Optional[ResumePredicate] = None,
    ):
        self._handlers[task_type] = handler
        self._resume_predicates[task_type] = can_resume or (lambda metadata: True)

    def can_resume(self, task_type: str, metadata: Dict[str, Any]) -> bool:
        predicate = self._resume_predicates.get(task_type)
        return bool(predicate and predicate(metadata))

    def submit(self, task_type: str, metadata: Dict[str, Any]) -> str:
        if task_type not in self._handlers:
            raise ValueError(f"No background handler registered for task type: {task_type}")
        task_id = self.task_manager.create_task(task_type, metadata)
        self.start(task_id)
        return task_id

    def start(self, task_id: str):
        task = self.task_manager.get_task(task_id)
        if not task or task.task_type not in self._handlers:
            raise ValueError(f"Task cannot be started: {task_id}")
        if task.status != TaskStatus.PENDING:
            raise ValueError(f"Task is not queued: {task_id}")
        self._launch(task_id)

    def resume(self, task_id: str) -> Dict[str, Any]:
        task = self.task_manager.get_task(task_id)
        if not task:
            raise ValueError(f"Task does not exist: {task_id}")
        if task.task_type not in self._handlers or not self.can_resume(task.task_type, task.metadata):
            raise ValueError(f"Task cannot be resumed: {task_id}")

        queued = self.task_manager.requeue_task(task_id)
        if not queued:
            raise ValueError(f"Task is not interrupted or failed: {task_id}")
        self._launch(task_id)
        return queued

    def resume_interrupted(self) -> int:
        resumed_count = 0
        for task in self.task_manager.list_tasks():
            if task["status"] != TaskStatus.INTERRUPTED.value:
                continue
            if not self.can_resume(task["task_type"], task.get("metadata") or {}):
                continue
            if self.task_manager.requeue_task(task["task_id"]):
                self._launch(task["task_id"])
                resumed_count += 1
        return resumed_count

    def _launch(self, task_id: str):
        with self._lock:
            thread = threading.Thread(
                target=self._run,
                args=(task_id,),
                daemon=True,
                name=f"background-task-{task_id[:8]}",
            )
            self.task_manager.attach_thread(task_id, thread)
            thread.start()

    def _run(self, task_id: str):
        if not self.task_manager.claim_task(task_id):
            task = self.task_manager.get_task(task_id)
            if task and task.status == TaskStatus.CANCELLING:
                self.task_manager.mark_cancelled(task_id)
            return

        try:
            with self.app.app_context():
                task = self.task_manager.get_task(task_id)
                if not task:
                    raise ValueError(f"Task disappeared before execution: {task_id}")
                handler = self._handlers.get(task.task_type)
                if not handler:
                    raise ValueError(f"No background handler registered for task type: {task.task_type}")

                result = handler(
                    self.app,
                    task_id,
                    task.metadata,
                    task.checkpoint,
                )
                self.task_manager.raise_if_cancelled(task_id)
                self.task_manager.complete_task(task_id, result or {})
        except TaskCancelledError:
            self.task_manager.mark_cancelled(task_id)
        except Exception as error:
            logger.exception("Background task failed: %s", task_id)
            self.task_manager.fail_task(task_id, str(error))
