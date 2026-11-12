"""
Task Status Management
Tracks long-running tasks (like graph building)
"""

import uuid
import threading
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field


class TaskStatus(str, Enum):
    """Task status enumeration"""
    PENDING = "pending"          # Pending
    PROCESSING = "processing"    # Processing
    CANCELLING = "cancelling"    # Cancellation requested
    CANCELLED = "cancelled"      # Cancelled
    INTERRUPTED = "interrupted"  # Worker did not survive a backend restart
    COMPLETED = "completed"      # Completed
    FAILED = "failed"            # Failed


@dataclass
class Task:
    """Task data class"""
    task_id: str
    task_type: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    progress: int = 0              # Overall progress percentage 0-100
    message: str = ""              # Status message
    result: Optional[Dict] = None  # Task result
    error: Optional[str] = None    # Error message
    metadata: Dict = field(default_factory=dict)  # Additional metadata
    progress_detail: Dict = field(default_factory=dict)  # Detailed progress information
    checkpoint: Dict = field(default_factory=dict)  # Durable restart checkpoint

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "progress": self.progress,
            "message": self.message,
            "progress_detail": self.progress_detail,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
            "checkpoint": self.checkpoint,
        }


class TaskManager:
    """
    Task Manager
    Thread-safe task status management
    """

    _instance = None
    _lock = threading.Lock()
    _storage_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "background_tasks.sqlite3")
    )

    def __new__(cls):
        """Singleton pattern"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks: Dict[str, Task] = {}
                    cls._instance._task_lock = threading.Lock()
                    cls._instance._cancellation_events: Dict[str, threading.Event] = {}
                    cls._instance._worker_threads: Dict[str, threading.Thread] = {}
                    cls._instance._worker_metadata: Dict[str, Dict[str, Any]] = {}
                    cls._instance._task_owners: Dict[str, Optional[int]] = {}
                    cls._instance._initialize_storage()
        return cls._instance

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self._storage_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_storage(self):
        os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS background_tasks (
                    task_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    thread_name TEXT,
                    thread_id INTEGER,
                    thread_alive INTEGER NOT NULL DEFAULT 0,
                    owner_pid INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS background_tasks_created_at "
                "ON background_tasks(created_at)"
            )
        self._refresh_from_storage(recover_interrupted=True)

    @staticmethod
    def _deserialize_task(payload: str, status: str) -> Task:
        data = json.loads(payload)
        return Task(
            task_id=data["task_id"],
            task_type=data["task_type"],
            status=TaskStatus(status),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            progress=data.get("progress", 0),
            message=data.get("message", ""),
            result=data.get("result"),
            error=data.get("error"),
            metadata=data.get("metadata") or {},
            progress_detail=data.get("progress_detail") or {},
            checkpoint=data.get("checkpoint") or {},
        )

    @staticmethod
    def _process_is_alive(process_id: Optional[int]) -> bool:
        if not process_id:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _worker_is_alive(self, row: sqlite3.Row) -> bool:
        owner_pid = row["owner_pid"]
        if owner_pid is None:
            return False
        if owner_pid != os.getpid():
            return self._process_is_alive(owner_pid)
        if row["thread_id"] is None:
            return self._process_is_alive(owner_pid)
        return any(
            thread.ident == row["thread_id"] and thread.is_alive()
            for thread in threading.enumerate()
        )

    def _refresh_from_storage(self, task_id: Optional[str] = None, recover_interrupted: bool = False):
        with self._task_lock:
            with self._connect() as connection:
                if task_id:
                    rows = connection.execute(
                        "SELECT * FROM background_tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT * FROM background_tasks ORDER BY created_at DESC"
                    ).fetchall()

            stored_ids = set()
            for row in rows:
                stored_ids.add(row["task_id"])
                task = self._deserialize_task(row["payload"], row["status"])
                self._tasks[task.task_id] = task
                self._task_owners[task.task_id] = row["owner_pid"]
                self._worker_metadata[task.task_id] = {
                    "thread_name": row["thread_name"],
                    "thread_id": row["thread_id"],
                    "thread_alive": bool(row["thread_alive"]),
                }

                cancellation_event = self._cancellation_events.setdefault(
                    task.task_id,
                    threading.Event(),
                )
                if row["cancel_requested"]:
                    cancellation_event.set()

                if (
                    recover_interrupted
                    and task.status in [TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.CANCELLING]
                    and not self._worker_is_alive(row)
                ):
                    task.status = TaskStatus.INTERRUPTED
                    task.message = "Worker stopped before the task finished"
                    task.error = "The backend restarted while this task was active."
                    task.updated_at = datetime.now()
                    cancellation_event.clear()
                    self._persist_task(task)

            if not task_id:
                for missing_id in self._tasks.keys() - stored_ids:
                    self._tasks.pop(missing_id, None)
                    self._cancellation_events.pop(missing_id, None)
                    self._worker_threads.pop(missing_id, None)
                    self._worker_metadata.pop(missing_id, None)
                    self._task_owners.pop(missing_id, None)

    def _persist_task(self, task: Task):
        thread = self._worker_threads.get(task.task_id)
        metadata = self._worker_metadata.get(task.task_id, {})
        thread_name = thread.name if thread else metadata.get("thread_name")
        thread_id = thread.ident if thread else metadata.get("thread_id")
        thread_alive = thread.is_alive() if thread else bool(metadata.get("thread_alive"))
        owner_pid = self._task_owners.get(task.task_id, os.getpid())
        cancellation_event = self._cancellation_events.get(task.task_id)
        cancel_requested = cancellation_event.is_set() if cancellation_event else False

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT cancel_requested FROM background_tasks WHERE task_id = ?",
                (task.task_id,),
            ).fetchone()
            if existing and existing["cancel_requested"]:
                cancel_requested = True
                if task.status in [TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.CANCELLING]:
                    task.status = TaskStatus.CANCELLING
                    task.message = "Cancellation requested"
                elif task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                    task.status = TaskStatus.CANCELLED
                    task.message = "Task cancelled"

            payload = json.dumps(task.to_dict(), ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO background_tasks (
                    task_id, created_at, updated_at, status, payload,
                    cancel_requested, thread_name, thread_id, thread_alive, owner_pid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    status = excluded.status,
                    payload = excluded.payload,
                    cancel_requested = excluded.cancel_requested,
                    thread_name = excluded.thread_name,
                    thread_id = excluded.thread_id,
                    thread_alive = excluded.thread_alive,
                    owner_pid = excluded.owner_pid
                """,
                (
                    task.task_id,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.status.value,
                    payload,
                    int(cancel_requested),
                    thread_name,
                    thread_id,
                    int(thread_alive),
                    owner_pid,
                ),
            )

        if cancellation_event and cancel_requested:
            cancellation_event.set()

    def create_task(self, task_type: str, metadata: Optional[Dict] = None) -> str:
        """
        Create new task

        Args:
            task_type: Task type
            metadata: Additional metadata

        Returns:
            Task ID
        """
        task_id = str(uuid.uuid4())
        now = datetime.now()

        task = Task(
            task_id=task_id,
            task_type=task_type,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            metadata=metadata or {}
        )

        with self._task_lock:
            self._tasks[task_id] = task
            self._cancellation_events[task_id] = threading.Event()
            self._task_owners[task_id] = os.getpid()
            self._worker_metadata[task_id] = {}
            self._persist_task(task)

        return task_id

    def attach_thread(self, task_id: str, thread: threading.Thread):
        """Associate a task with its worker thread before the thread starts."""
        with self._task_lock:
            if task_id in self._tasks:
                self._worker_threads[task_id] = thread
                self._worker_metadata[task_id] = {
                    "thread_name": thread.name,
                    "thread_id": thread.ident,
                    "thread_alive": thread.is_alive(),
                }
                self._persist_task(self._tasks[task_id])

    def claim_task(self, task_id: str) -> bool:
        """Atomically claim a queued task so only one backend worker executes it."""
        with self._task_lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM background_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if not row or row["status"] != TaskStatus.PENDING.value:
                    return False

                task = self._deserialize_task(row["payload"], row["status"])
                task.status = TaskStatus.PROCESSING
                task.updated_at = datetime.now()
                task.message = "Worker started"
                thread = self._worker_threads.get(task_id)
                metadata = self._worker_metadata.get(task_id, {})
                thread_name = thread.name if thread else metadata.get("thread_name")
                thread_id = thread.ident if thread else metadata.get("thread_id")
                thread_alive = thread.is_alive() if thread else False
                owner_pid = os.getpid()
                connection.execute(
                    """
                    UPDATE background_tasks
                    SET updated_at = ?, status = ?, payload = ?, thread_name = ?,
                        thread_id = ?, thread_alive = ?, owner_pid = ?, cancel_requested = 0
                    WHERE task_id = ?
                    """,
                    (
                        task.updated_at.isoformat(),
                        task.status.value,
                        json.dumps(task.to_dict(), ensure_ascii=False),
                        thread_name,
                        thread_id,
                        int(thread_alive),
                        owner_pid,
                        task_id,
                    ),
                )

            self._tasks[task_id] = task
            self._task_owners[task_id] = owner_pid
            self._worker_metadata[task_id] = {
                "thread_name": thread_name,
                "thread_id": thread_id,
                "thread_alive": thread_alive,
            }
            self._cancellation_events.setdefault(task_id, threading.Event()).clear()
            return True

    def is_cancel_requested(self, task_id: str) -> bool:
        self._refresh_from_storage(task_id)
        with self._task_lock:
            event = self._cancellation_events.get(task_id)
            return event.is_set() if event else False

    def raise_if_cancelled(self, task_id: str):
        if self.is_cancel_requested(task_id):
            raise TaskCancelledError(f"Task {task_id} was cancelled")

    def cancel_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Request cooperative cancellation for an active task."""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            if task.status not in [TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.CANCELLING]:
                return self._serialize_task(task)

            self._cancellation_events[task_id].set()
            task.status = TaskStatus.CANCELLING
            task.message = "Cancellation requested"
            task.updated_at = datetime.now()
            self._persist_task(task)
            return self._serialize_task(task)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task"""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            return self._tasks.get(task_id)

    def get_task_data(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a task snapshot including its associated worker thread."""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            return self._serialize_task(task) if task else None

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        progress_detail: Optional[Dict] = None,
        checkpoint: Optional[Dict] = None,
    ):
        """
        Update task status

        Args:
            task_id: Task ID
            status: New status
            progress: Progress
            message: Message
            result: Result
            error: Error message
            progress_detail: Detailed progress information
        """
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task:
                if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                    return
                cancellation_event = self._cancellation_events.get(task_id)
                if cancellation_event and cancellation_event.is_set():
                    task.status = TaskStatus.CANCELLING
                    task.message = "Cancellation requested"
                    task.updated_at = datetime.now()
                    return
                task.updated_at = datetime.now()
                if status is not None:
                    task.status = status
                if progress is not None:
                    task.progress = progress
                if message is not None:
                    task.message = message
                if result is not None:
                    task.result = result
                if error is not None:
                    task.error = error
                if progress_detail is not None:
                    task.progress_detail = progress_detail
                if checkpoint is not None:
                    task.checkpoint = checkpoint
                self._persist_task(task)

    def requeue_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Queue an interrupted or failed task to resume from its last checkpoint."""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in [TaskStatus.INTERRUPTED, TaskStatus.FAILED]:
                return None

            task.status = TaskStatus.PENDING
            task.message = "Queued to resume from the last checkpoint"
            task.error = None
            task.updated_at = datetime.now()
            self._cancellation_events.setdefault(task_id, threading.Event()).clear()
            self._task_owners[task_id] = os.getpid()
            self._worker_metadata[task_id] = {}
            self._persist_task(task)
            return self._serialize_task(task)

    def complete_task(self, task_id: str, result: Dict):
        """Mark task as completed"""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task or task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                return
            cancellation_event = self._cancellation_events.get(task_id)
            if cancellation_event and cancellation_event.is_set():
                task.status = TaskStatus.CANCELLED
                task.message = "Task cancelled"
            else:
                task.status = TaskStatus.COMPLETED
                task.progress = 100
                task.message = "Task completed"
                task.result = result
            task.updated_at = datetime.now()
            self._persist_task(task)

    def fail_task(self, task_id: str, error: str):
        """Mark task as failed"""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if not task or task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                return
            cancellation_event = self._cancellation_events.get(task_id)
            if cancellation_event and cancellation_event.is_set():
                task.status = TaskStatus.CANCELLED
                task.message = error or "Task cancelled"
            else:
                task.status = TaskStatus.FAILED
                task.message = "Task failed"
                task.error = error
            task.updated_at = datetime.now()
            self._persist_task(task)

    def mark_cancelled(self, task_id: str, message: str = "Task cancelled"):
        """Mark a task as cancelled after its worker reaches a safe checkpoint."""
        self._refresh_from_storage(task_id)
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task and task.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                task.status = TaskStatus.CANCELLED
                task.message = message
                task.updated_at = datetime.now()
                self._persist_task(task)

    def clear_history(self) -> int:
        """Remove terminal tasks while preserving active work."""
        terminal_statuses = [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        ]
        with self._task_lock:
            old_ids = [
                task_id for task_id, task in self._tasks.items()
                if task.status in terminal_statuses
            ]
            for task_id in old_ids:
                del self._tasks[task_id]
                self._cancellation_events.pop(task_id, None)
                self._worker_threads.pop(task_id, None)
                self._worker_metadata.pop(task_id, None)
                self._task_owners.pop(task_id, None)
            with self._connect() as connection:
                placeholders = ", ".join("?" for _ in old_ids)
                if old_ids:
                    connection.execute(
                        f"DELETE FROM background_tasks WHERE task_id IN ({placeholders})",
                        old_ids,
                    )
            return len(old_ids)

    def _serialize_task(self, task: Task) -> Dict[str, Any]:
        data = task.to_dict()
        thread = self._worker_threads.get(task.task_id)
        metadata = self._worker_metadata.get(task.task_id, {})
        data.update({
            "thread_name": thread.name if thread else metadata.get("thread_name"),
            "thread_id": thread.ident if thread else metadata.get("thread_id"),
            "thread_alive": (
                thread.is_alive()
                if thread
                else bool(metadata.get("thread_alive")) and task.status in [
                    TaskStatus.PENDING,
                    TaskStatus.PROCESSING,
                    TaskStatus.CANCELLING,
                ]
            ),
            "worker_pid": self._task_owners.get(task.task_id),
        })
        return data

    def list_tasks(self, task_type: Optional[str] = None) -> list:
        """List tasks"""
        self._refresh_from_storage(recover_interrupted=True)
        with self._task_lock:
            tasks = list(self._tasks.values())
            if task_type:
                tasks = [t for t in tasks if t.task_type == task_type]
            return [self._serialize_task(t) for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)]

    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """Clean up old tasks"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        with self._task_lock:
            old_ids = [
                tid for tid, task in self._tasks.items()
                if task.created_at < cutoff and task.status in [
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.INTERRUPTED,
                ]
            ]
            for tid in old_ids:
                del self._tasks[tid]
                self._cancellation_events.pop(tid, None)
                self._worker_threads.pop(tid, None)
                self._worker_metadata.pop(tid, None)
                self._task_owners.pop(tid, None)
            if old_ids:
                with self._connect() as connection:
                    placeholders = ", ".join("?" for _ in old_ids)
                    connection.execute(
                        f"DELETE FROM background_tasks WHERE task_id IN ({placeholders})",
                        old_ids,
                    )


class TaskCancelledError(Exception):
    """Raised by a worker when it reaches a cooperative cancellation checkpoint."""

