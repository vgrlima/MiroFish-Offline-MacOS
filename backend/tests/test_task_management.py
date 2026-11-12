import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

task_module_path = Path(__file__).resolve().parents[1] / "app" / "models" / "task.py"
task_module_spec = importlib.util.spec_from_file_location("task_model_under_test", task_module_path)
task_module = importlib.util.module_from_spec(task_module_spec)
sys.modules[task_module_spec.name] = task_module
task_module_spec.loader.exec_module(task_module)

TaskCancelledError = task_module.TaskCancelledError
TaskManager = task_module.TaskManager
TaskStatus = task_module.TaskStatus


class TaskManagementTests(unittest.TestCase):
    def setUp(self):
        self.previous_instance = TaskManager._instance
        self.previous_storage_path = TaskManager._storage_path
        self.temp_dir = tempfile.TemporaryDirectory()
        TaskManager._storage_path = os.path.join(self.temp_dir.name, "tasks.sqlite3")
        TaskManager._instance = None
        self.task_manager = TaskManager()

    def tearDown(self):
        TaskManager._instance = self.previous_instance
        TaskManager._storage_path = self.previous_storage_path
        self.temp_dir.cleanup()

    def test_cancelled_task_stays_cancelled_when_worker_finishes(self):
        task_id = self.task_manager.create_task("graph_build")

        cancelled = self.task_manager.cancel_task(task_id)
        self.assertEqual(cancelled["status"], TaskStatus.CANCELLING.value)

        with self.assertRaises(TaskCancelledError):
            self.task_manager.raise_if_cancelled(task_id)

        self.task_manager.complete_task(task_id, {"graph_id": "graph-1"})

        self.assertEqual(
            self.task_manager.list_tasks()[0]["status"],
            TaskStatus.CANCELLED.value
        )

    def test_clear_history_removes_only_terminal_tasks(self):
        active_task_id = self.task_manager.create_task("simulation_prepare")
        completed_task_id = self.task_manager.create_task("report_generate")
        failed_task_id = self.task_manager.create_task("graph_build")
        cancelled_task_id = self.task_manager.create_task("graph_build")

        self.task_manager.complete_task(completed_task_id, {})
        self.task_manager.fail_task(failed_task_id, "failed")
        self.task_manager.cancel_task(cancelled_task_id)
        self.task_manager.mark_cancelled(cancelled_task_id)

        self.assertEqual(self.task_manager.clear_history(), 3)
        self.assertEqual(
            [task["task_id"] for task in self.task_manager.list_tasks()],
            [active_task_id]
        )

    def test_task_list_exposes_associated_worker_thread(self):
        task_id = self.task_manager.create_task("graph_build")
        worker_ready = threading.Event()
        release_worker = threading.Event()

        def wait_for_release():
            worker_ready.set()
            release_worker.wait()

        worker = threading.Thread(target=wait_for_release, name="graph-worker", daemon=True)
        self.task_manager.attach_thread(task_id, worker)
        worker.start()
        try:
            self.assertTrue(worker_ready.wait(timeout=2))
            task = self.task_manager.list_tasks()[0]

            self.assertEqual(task["thread_name"], "graph-worker")
            self.assertIsNotNone(task["thread_id"])
            self.assertTrue(task["thread_alive"])
        finally:
            release_worker.set()
            worker.join(timeout=2)

        self.assertFalse(self.task_manager.get_task_data(task_id)["thread_alive"])

    def test_tasks_survive_manager_restart_and_orphaned_workers_are_visible(self):
        active_task_id = self.task_manager.create_task("graph_build")
        completed_task_id = self.task_manager.create_task("report_generate")
        self.task_manager.complete_task(completed_task_id, {"report_id": "report-1"})

        with self.task_manager._task_lock:
            self.task_manager._task_owners[active_task_id] = 2_000_000_000
            self.task_manager._persist_task(self.task_manager._tasks[active_task_id])

        TaskManager._instance = None
        restored_manager = TaskManager()
        restored_tasks = {
            task["task_id"]: task for task in restored_manager.list_tasks()
        }

        self.assertEqual(restored_tasks[completed_task_id]["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(restored_tasks[completed_task_id]["result"], {"report_id": "report-1"})
        self.assertEqual(restored_tasks[active_task_id]["status"], TaskStatus.INTERRUPTED.value)

    def test_checkpoint_survives_restart_and_can_be_requeued(self):
        task_id = self.task_manager.create_task(
            "graph_build",
            metadata={"project_id": "project-1", "checkpoint_version": 1},
        )
        self.task_manager.update_task(
            task_id,
            checkpoint={"stage": "ingesting", "next_chunk": 7},
            progress=62,
        )
        with self.task_manager._task_lock:
            self.task_manager._task_owners[task_id] = 2_000_000_000
            self.task_manager._persist_task(self.task_manager._tasks[task_id])

        TaskManager._instance = None
        restored_manager = TaskManager()
        task = restored_manager.get_task_data(task_id)

        self.assertEqual(task["status"], TaskStatus.INTERRUPTED.value)
        self.assertEqual(task["checkpoint"], {"stage": "ingesting", "next_chunk": 7})
        self.assertEqual(task["progress"], 62)

        resumed = restored_manager.requeue_task(task_id)
        self.assertEqual(resumed["status"], TaskStatus.PENDING.value)
        self.assertEqual(resumed["checkpoint"], task["checkpoint"])
        self.assertTrue(restored_manager.claim_task(task_id))
        self.assertFalse(restored_manager.claim_task(task_id))
