"""Restartable handlers for persisted background tasks."""

import os
import hashlib
import json
import uuid
from typing import Any, Dict

from flask import Flask

from ..models.project import ProjectManager, ProjectStatus
from ..models.task import TaskCancelledError, TaskManager
from .graph_builder import GraphBuilderService
from .report_agent import ReportAgent, ReportManager, ReportStatus
from .graph_tools import GraphToolsService
from .simulation_manager import SimulationManager, SimulationStatus
from .text_processor import TextProcessor


def register_background_task_handlers(runner):
    runner.register(
        "graph_build",
        run_graph_build,
        lambda metadata: bool(metadata.get("project_id") and metadata.get("checkpoint_version") == 1),
    )
    runner.register(
        "simulation_prepare",
        run_simulation_prepare,
        lambda metadata: bool(metadata.get("simulation_id") and metadata.get("project_id")),
    )
    runner.register(
        "report_generate",
        run_report_generation,
        lambda metadata: bool(metadata.get("report_id") and metadata.get("simulation_id")),
    )


def run_graph_build(
    app: Flask,
    task_id: str,
    metadata: Dict[str, Any],
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    task_manager = TaskManager()
    project_id = metadata["project_id"]
    project = ProjectManager.get_project(project_id)
    if not project:
        raise ValueError(f"Project does not exist: {project_id}")
    if not project.ontology:
        raise ValueError("Ontology definition not found")

    text = ProjectManager.get_extracted_text(project_id)
    if not text:
        raise ValueError("Extracted text not found")

    storage = app.extensions.get("neo4j_storage")
    if not storage:
        raise ValueError("GraphStorage is unavailable")

    graph_name = metadata.get("graph_name") or project.name or "MiroFish Graph"
    chunk_size = int(metadata.get("chunk_size") or project.chunk_size)
    chunk_overlap = int(metadata.get("chunk_overlap") or project.chunk_overlap)
    chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
    input_signature = hashlib.sha256(
        json.dumps(
            {
                "text": text,
                "ontology": project.ontology,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    checkpoint_signature = checkpoint.get("input_signature")
    if checkpoint_signature and checkpoint_signature != input_signature:
        raise ValueError("Graph build inputs changed after the last checkpoint; start a new graph build.")
    graph_id = checkpoint.get("graph_id") or str(
        uuid.uuid5(uuid.UUID(task_id), "graph")
    )
    builder = GraphBuilderService(storage=storage)

    try:
        task_manager.raise_if_cancelled(task_id)
        project.status = ProjectStatus.GRAPH_BUILDING
        project.graph_build_task_id = task_id
        project.graph_id = graph_id
        ProjectManager.save_project(project)

        storage.create_graph(
            name=graph_name,
            description="MiroFish Social Simulation Graph",
            graph_id=graph_id,
        )
        storage.set_ontology(graph_id, project.ontology)
        task_manager.update_task(
            task_id,
            progress=max(15, int(checkpoint.get("progress", 10))),
            message="Resuming graph ingestion from its saved chunk checkpoint",
            checkpoint={
                "stage": "ingesting",
                "graph_id": graph_id,
                "next_chunk": int(checkpoint.get("next_chunk", 0)),
                "input_signature": input_signature,
            },
        )

        next_chunk = int(checkpoint.get("next_chunk", 0))
        total_chunks = len(chunks)
        for chunk_index in range(next_chunk, total_chunks):
            task_manager.raise_if_cancelled(task_id)
            storage.add_text(
                graph_id,
                chunks[chunk_index],
                episode_id=str(uuid.uuid5(uuid.UUID(task_id), f"chunk:{chunk_index}")),
            )
            next_chunk = chunk_index + 1
            progress = 20 + int(next_chunk / max(total_chunks, 1) * 65)
            task_manager.update_task(
                task_id,
                progress=progress,
                message=f"Processed text chunk {next_chunk} of {total_chunks}",
                checkpoint={
                    "stage": "ingesting",
                    "graph_id": graph_id,
                    "next_chunk": next_chunk,
                    "progress": progress,
                    "input_signature": input_signature,
                },
            )

        task_manager.raise_if_cancelled(task_id)
        graph_data = builder.get_graph_data(graph_id)
        project.status = ProjectStatus.GRAPH_COMPLETED
        project.error = None
        ProjectManager.save_project(project)
        return {
            "project_id": project_id,
            "graph_id": graph_id,
            "node_count": graph_data.get("node_count", 0),
            "edge_count": graph_data.get("edge_count", 0),
            "chunk_count": total_chunks,
        }
    except TaskCancelledError:
        project.status = ProjectStatus.FAILED
        project.error = "Graph build cancelled"
        ProjectManager.save_project(project)
        raise
    except Exception as error:
        project.status = ProjectStatus.FAILED
        project.error = str(error)
        ProjectManager.save_project(project)
        raise


def run_simulation_prepare(
    app: Flask,
    task_id: str,
    metadata: Dict[str, Any],
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    task_manager = TaskManager()
    manager = SimulationManager()
    simulation_id = metadata["simulation_id"]
    state = manager.get_simulation(simulation_id)
    if not state:
        raise ValueError(f"Simulation does not exist: {simulation_id}")
    project = ProjectManager.get_project(state.project_id)
    if not project:
        raise ValueError(f"Project does not exist: {state.project_id}")

    simulation_dir = manager._get_simulation_dir(simulation_id)
    config_path = os.path.join(simulation_dir, "simulation_config.json")
    if state.status == SimulationStatus.READY and state.config_generated and os.path.exists(config_path):
        return state.to_simple_dict()

    storage = app.extensions.get("neo4j_storage")
    if not storage:
        raise ValueError("GraphStorage is unavailable")

    stage_weights = {
        "reading": (0, 20),
        "generating_profiles": (20, 70),
        "generating_config": (70, 90),
        "copying_scripts": (90, 100),
    }
    stage_names = {
        "reading": "Read knowledge graph entities",
        "generating_profiles": "Generate agent profiles",
        "generating_config": "Generate simulation configuration",
        "copying_scripts": "Prepare simulation scripts",
    }

    def progress_callback(stage, progress, message, **kwargs):
        task_manager.raise_if_cancelled(task_id)
        start, end = stage_weights.get(stage, (0, 100))
        current_progress = int(start + (end - start) * progress / 100)
        stage_index = list(stage_weights).index(stage) + 1 if stage in stage_weights else 1
        detail = {
            "current_stage": stage,
            "current_stage_name": stage_names.get(stage, stage),
            "stage_index": stage_index,
            "total_stages": len(stage_weights),
            "stage_progress": progress,
            "current_item": kwargs.get("current", 0),
            "total_items": kwargs.get("total", 0),
            "item_description": message,
        }
        saved_checkpoint = task_manager.get_task(task_id).checkpoint or {}
        checkpoint_stage = saved_checkpoint.get("stage")
        if progress >= 100:
            checkpoint_stage = stage
        task_manager.update_task(
            task_id,
            progress=current_progress,
            message=f"[{stage_index}/{len(stage_weights)}] {stage_names.get(stage, stage)}: {message}",
            progress_detail=detail,
            checkpoint={
                **saved_checkpoint,
                "stage": checkpoint_stage,
                "current_stage": stage,
                "stage_progress": progress,
            },
        )

    try:
        result_state = manager.prepare_simulation(
            simulation_id=simulation_id,
            simulation_requirement=project.simulation_requirement or "",
            document_text=ProjectManager.get_extracted_text(state.project_id) or "",
            defined_entity_types=metadata.get("entity_types"),
            use_llm_for_profiles=metadata.get("use_llm_for_profiles", True),
            progress_callback=progress_callback,
            parallel_profile_count=int(metadata.get("parallel_profile_count", 5)),
            storage=storage,
            resume_from=(checkpoint or {}).get("stage"),
        )
        if result_state.status != SimulationStatus.READY:
            raise RuntimeError(result_state.error or "Simulation preparation did not complete")
        return result_state.to_simple_dict()
    except TaskCancelledError:
        state = manager.get_simulation(simulation_id)
        if state:
            state.status = SimulationStatus.FAILED
            state.error = "Simulation preparation cancelled"
            manager._save_simulation_state(state)
        raise
    except Exception as error:
        state = manager.get_simulation(simulation_id)
        if state:
            state.status = SimulationStatus.FAILED
            state.error = str(error)
            manager._save_simulation_state(state)
        raise


def run_report_generation(
    app: Flask,
    task_id: str,
    metadata: Dict[str, Any],
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    task_manager = TaskManager()
    simulation_id = metadata["simulation_id"]
    simulation = SimulationManager().get_simulation(simulation_id)
    if not simulation:
        raise ValueError(f"Simulation does not exist: {simulation_id}")
    project = ProjectManager.get_project(simulation.project_id)
    if not project:
        raise ValueError(f"Project does not exist: {simulation.project_id}")

    report_id = metadata["report_id"]
    existing_report = ReportManager.get_report(report_id)
    if existing_report and existing_report.status == ReportStatus.COMPLETED:
        return {
            "report_id": report_id,
            "simulation_id": simulation_id,
            "status": "completed",
        }

    storage = app.extensions.get("neo4j_storage")
    if not storage:
        raise ValueError("GraphStorage is unavailable")
    graph_id = metadata.get("graph_id") or simulation.graph_id or project.graph_id
    if not graph_id:
        raise ValueError("Missing graph ID for report generation")

    agent = ReportAgent(
        graph_id=graph_id,
        simulation_id=simulation_id,
        simulation_requirement=project.simulation_requirement or "",
        graph_tools=GraphToolsService(storage=storage),
    )

    def progress_callback(stage, progress, message):
        task_manager.raise_if_cancelled(task_id)
        saved_checkpoint = task_manager.get_task(task_id).checkpoint or {}
        task_manager.update_task(
            task_id,
            progress=progress,
            message=f"[{stage}] {message}",
            checkpoint={
                **saved_checkpoint,
                "stage": stage,
                "progress": progress,
                "message": message,
            },
        )

    report = agent.generate_report(
        progress_callback=progress_callback,
        report_id=report_id,
    )
    if report.status != ReportStatus.COMPLETED:
        raise RuntimeError(report.error or "Report generation failed")
    return {
        "report_id": report.report_id,
        "simulation_id": simulation_id,
        "status": "completed",
    }
