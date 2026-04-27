"""Taktis facade -- single entry point combining all core components.

After the Phase 2 decomposition, this module is a thin delegation layer.
Business logic lives in :mod:`project_service` and :mod:`execution_service`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from taktis import repository as repo
from taktis.config import Settings
from taktis.core.events import EventBus
from taktis.exceptions import TaktisError
from taktis.core.execution_service import ExecutionService
from taktis.core.agent_templates import AgentTemplateRegistry
from taktis.core.experts import ExpertRegistry
from taktis.core.manager import ProcessManager
from taktis.core.project_service import ProjectService
from taktis.core.scheduler import WaveScheduler
from taktis.core.state import StateTracker
from taktis.db import close_pool, get_session, init_db, init_pool

logger = logging.getLogger(__name__)


class Taktis:
    """Main facade combining all core components.

    All interaction from the Web UI (or any other interface layer) should go
    through this class.  Call :meth:`initialize` before using any other method,
    and :meth:`shutdown` when done.
    """

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or Settings()
        self.event_bus = EventBus()
        self.process_manager: ProcessManager | None = None
        self.scheduler: WaveScheduler | None = None
        self.state_tracker: StateTracker | None = None
        self.expert_registry: ExpertRegistry | None = None
        self.agent_template_registry: AgentTemplateRegistry | None = None
        self._initialized = False
        self._active_flow_executors: dict[str, Any] = {}
        # Internal services (created in initialize)
        self._project_service: ProjectService | None = None
        self._execution_service: ExecutionService | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize all components.  Must be called before use."""
        if self._initialized:
            return

        # Database
        await init_db()
        await init_pool()

        # Process manager
        self.process_manager = ProcessManager(
            event_bus=self.event_bus,
            max_concurrent=self.config.max_concurrent_tasks,
            claude_command=self.config.claude_command,
        )

        # State tracker
        self.state_tracker = StateTracker(
            db_session_factory=get_session,
            event_bus=self.event_bus,
        )
        await self.state_tracker.start()

        # Scheduler
        self.scheduler = WaveScheduler(
            process_manager=self.process_manager,
            event_bus=self.event_bus,
            state_tracker=self.state_tracker,
            db_session_factory=get_session,
        )

        # Expert registry
        self.expert_registry = ExpertRegistry(db_session_factory=get_session)
        await self.expert_registry.load_builtins()

        # Agent template registry
        self.agent_template_registry = AgentTemplateRegistry(db_session_factory=get_session)
        await self.agent_template_registry.load_builtins()

        # --- Create internal services ---
        self._project_service = ProjectService(
            db_session_factory=get_session,
            expert_registry=self.expert_registry,
            event_bus=self.event_bus,
        )

        # Verify that `self` satisfies the ExecutionOrchestrator Protocol
        # at runtime — catches signature mismatches early.
        from taktis.core.execution_service import ExecutionOrchestrator
        if not isinstance(self, ExecutionOrchestrator):
            logger.warning(
                "Taktis does not satisfy ExecutionOrchestrator protocol; "
                "pipeline features may fail at runtime"
            )

        self._execution_service = ExecutionService(
            process_manager=self.process_manager,
            scheduler=self.scheduler,
            event_bus=self.event_bus,
            db_session_factory=get_session,
            project_service=self._project_service,
            engine=self,
        )

        # Wire scheduler → execution service callbacks (typed kwargs)
        self.scheduler.set_task_prep_callback(
            on_task_prep_complete=self._execution_service._handle_pipeline_task_complete,
        )

        # Wire project service → execution service callbacks (typed kwargs)
        self._project_service.set_execution_callbacks(
            start_task=self._execution_service.start_task,
            stop_project_tasks=self._stop_project_tasks,
            get_running_count=lambda: self.process_manager.get_running_count(),
        )

        # Clean up stale tasks from previous sessions
        await self._execution_service._recover_stale_tasks()

        # Cron scheduler for scheduled pipeline runs
        from taktis.core.cron_scheduler import CronScheduler
        self._cron_scheduler = CronScheduler(self, get_session)
        await self._cron_scheduler.start()

        # Stale task watchdog -- detects stuck running tasks and fails them
        from taktis.core.stale_task_watchdog import StaleTaskWatchdog
        self._stale_watchdog = StaleTaskWatchdog(self.event_bus, get_session, self.process_manager)
        await self._stale_watchdog.start()

        self._initialized = True
        logger.info("Taktis initialized")

        # Report any work that was interrupted before the previous shutdown.
        await self._execution_service._report_interrupted_work()

        # Re-trigger fix loops for reviews with unprocessed CRITICALs.
        _recover_task = asyncio.create_task(
            self._execution_service._recover_unprocessed_reviews(),
            name="recover-unprocessed-reviews",
        )
        _recover_task.add_done_callback(
            self._execution_service._make_task_done_callback(
                "recover-unprocessed-reviews"
            ),
        )

    async def _stop_project_tasks(self, task_ids: list[str]) -> None:
        """Stop specific tasks by ID (used by ProjectService.delete_project)."""
        if self.process_manager is None:
            raise TaktisError("ProcessManager not initialized")
        for tid in task_ids:
            await self.process_manager.stop_task(tid)

    async def shutdown(self) -> None:
        """Graceful shutdown -- stop all tasks, stop state tracker."""
        if not self._initialized:
            return

        logger.info("Taktis shutting down")

        # Stop stale task watchdog
        if hasattr(self, "_stale_watchdog"):
            await self._stale_watchdog.stop()

        # Stop cron scheduler
        if hasattr(self, "_cron_scheduler"):
            await self._cron_scheduler.stop()

        # Stop all running processes
        if self.process_manager is not None:
            await self.process_manager.stop_all()

        # Stop state tracker
        if self.state_tracker is not None:
            await self.state_tracker.stop()

        # Close DB connection pool
        await close_pool()

        self._initialized = False
        self.event_bus.clear()
        logger.info("Taktis shut down")

    async def __aenter__(self) -> "Taktis":
        await self.initialize()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.shutdown()

    def _assert_ready(self) -> None:
        if not self._initialized:
            raise TaktisError("Taktis not initialized — call initialize() first")

    # ------------------------------------------------------------------
    # Project CRUD  (→ ProjectService)
    # ------------------------------------------------------------------

    async def create_project(
        self, name: str, working_dir: str, description: str = "",
        model: str | None = None, permission_mode: str | None = None,
        create_dir: bool = False, clean_existing: bool = False,
    ) -> dict:
        self._assert_ready()
        return await self._project_service.create_project(
            name, working_dir, description,
            model=model or self.config.default_model,
            permission_mode=permission_mode or self.config.default_permission_mode,
            create_dir=create_dir,
            clean_existing=clean_existing,
        )

    async def list_projects(self) -> list[dict]:
        self._assert_ready()
        return await self._project_service.list_projects()

    async def get_project(self, name: str) -> dict | None:
        self._assert_ready()
        return await self._project_service.get_project(name)

    async def delete_project(self, name: str) -> bool:
        self._assert_ready()
        return await self._project_service.delete_project(name)

    async def update_project(self, name: str, **kwargs) -> dict | None:
        self._assert_ready()
        return await self._project_service.update_project(name, **kwargs)

    # ------------------------------------------------------------------
    # Phase CRUD  (→ ProjectService)
    # ------------------------------------------------------------------

    async def create_phase(
        self, project_name: str, name: str,
        goal: str = "", description: str = "",
        success_criteria: list | None = None,
        context_config: str | None = None,
    ) -> dict:
        self._assert_ready()
        return await self._project_service.create_phase(
            project_name, name, goal=goal, description=description,
            success_criteria=success_criteria, context_config=context_config,
        )

    async def list_phases(self, project_name: str) -> list[dict]:
        self._assert_ready()
        return await self._project_service.list_phases(project_name)

    async def get_phase(self, project_name: str, phase_number: int) -> dict | None:
        self._assert_ready()
        return await self._project_service.get_phase(project_name, phase_number)

    async def add_criterion(
        self, project_name: str, phase_number: int, criterion: str,
    ) -> bool:
        self._assert_ready()
        return await self._project_service.add_criterion(
            project_name, phase_number, criterion,
        )

    async def delete_phase(self, project_name: str, phase_number: int) -> bool:
        self._assert_ready()
        return await self._project_service.delete_phase(project_name, phase_number)

    # ------------------------------------------------------------------
    # Task CRUD  (→ ProjectService)
    # ------------------------------------------------------------------

    async def create_task(
        self, project_name: str, prompt: str, phase_number: int,
        name: str = "", expert: str | None = None, expert_id: str | None = None,
        wave: int = 1, interactive: bool = False, model: str | None = None,
        task_type: str | None = None, retry_policy: str | None = None,
        system_prompt: str = "",
    ) -> dict:
        self._assert_ready()
        return await self._project_service.create_task(
            project_name, prompt, phase_number,
            name=name, expert=expert, expert_id=expert_id, wave=wave,
            interactive=interactive, model=model, task_type=task_type,
            retry_policy=retry_policy, system_prompt=system_prompt,
        )

    async def list_tasks(
        self, project_name: str, phase_number: int | None = None,
    ) -> list[dict]:
        self._assert_ready()
        return await self._project_service.list_tasks(project_name, phase_number)

    async def get_active_tasks_all(self) -> list[dict]:
        """Get running/pending/awaiting_input tasks across all projects (single query)."""
        self._assert_ready()
        return await self._project_service.get_active_tasks_all()

    async def get_recent_task_transitions(self, limit: int = 8) -> list[dict]:
        """Recent task starts/completions for the dashboard feed."""
        self._assert_ready()
        return await self._project_service.get_recent_task_transitions(limit=limit)

    async def get_task(self, task_id: str) -> dict | None:
        self._assert_ready()
        return await self._project_service.get_task(task_id)

    async def discuss_task(self, target_task_id: str) -> dict | None:
        self._assert_ready()
        return await self._project_service.discuss_task(target_task_id)

    async def research_task(self, target_task_id: str) -> dict | None:
        self._assert_ready()
        return await self._project_service.research_task(target_task_id)

    # ------------------------------------------------------------------
    # Execution & Control  (→ ExecutionService)
    # ------------------------------------------------------------------

    async def start_task(self, task_id: str) -> None:
        self._assert_ready()
        await self._execution_service.start_task(task_id)

    async def stop_task(self, task_id: str) -> None:
        self._assert_ready()
        await self._execution_service.stop_task(task_id)

    async def continue_task(self, task_id: str, message: str) -> None:
        self._assert_ready()
        await self._execution_service.continue_task(task_id, message)

    async def send_input(self, task_id: str, text: str) -> None:
        self._assert_ready()
        await self._execution_service.send_input(task_id, text)

    async def approve_checkpoint(self, task_id: str) -> None:
        self._assert_ready()
        await self._execution_service.approve_checkpoint(task_id)

    async def deny_tool(
        self, task_id: str, message: str = "User denied this action",
    ) -> None:
        self._assert_ready()
        await self._execution_service.deny_tool(task_id, message)

    async def decide_checkpoint(self, task_id: str, option: str) -> None:
        self._assert_ready()
        await self._execution_service.decide_checkpoint(task_id, option)

    async def get_pending_approval(self, task_id: str) -> dict | None:
        self._assert_ready()
        return await self._execution_service.get_pending_approval(task_id)

    # ------------------------------------------------------------------
    # Phase & Project Execution  (→ ExecutionService)
    # ------------------------------------------------------------------

    async def run_phase(self, project_name: str, phase_number: int) -> None:
        self._assert_ready()
        return await self._execution_service.run_phase(project_name, phase_number)

    async def run_project(self, project_name: str) -> None:
        self._assert_ready()
        return await self._execution_service.run_project(project_name)

    async def resume_phase(self, phase_id: str) -> None:
        self._assert_ready()
        return await self._execution_service.resume_phase(phase_id)

    async def stop_all(self, project_name: str | None = None) -> int:
        self._assert_ready()
        # Cancel active flow executors so gates/plan approvals unblock
        for executor in list(self._active_flow_executors.values()):
            try:
                executor.cancel()
            except Exception:
                pass
        return await self._execution_service.stop_all(project_name)

    # ------------------------------------------------------------------
    # Flow execution  (→ GraphExecutor)
    # ------------------------------------------------------------------

    async def execute_flow(
        self, project_name: str, flow_json: dict | str,
        template_name: str = "Custom Flow",
    ) -> str | list[str]:
        """Execute a pipeline template graph against a project.

        Returns a single phase_id for single-module flows, or a list of
        phase_ids for multi-module flows.
        """
        self._assert_ready()
        from taktis.core.graph_executor import GraphExecutor
        executor = GraphExecutor(
            self, project_name, flow_json, template_name=template_name,
        )
        # Track active executor so approve_plan() can reach it
        project = await self.get_project(project_name)
        if project:
            self._active_flow_executors[project["id"]] = executor
        try:
            result = await executor.execute_multi()
        finally:
            if project:
                self._active_flow_executors.pop(project["id"], None)
        return result

    def approve_flow_plan(self, project_id: str) -> bool:
        """Approve a pending plan in an active graph executor."""
        executor = self._active_flow_executors.get(project_id)
        if executor is not None:
            return executor.approve_plan()
        return False

    def approve_gate(self, project_id: str, node_id: str) -> bool:
        """Approve a pending human gate in an active graph executor."""
        executor = self._active_flow_executors.get(project_id)
        if executor is not None:
            return executor.approve_gate(node_id)
        return False

    def reject_gate(self, project_id: str, node_id: str) -> bool:
        """Reject a pending human gate in an active graph executor."""
        executor = self._active_flow_executors.get(project_id)
        if executor is not None:
            return executor.reject_gate(node_id)
        return False

    def cancel_flow(self, project_id: str) -> None:
        """Cancel an active graph executor for the project."""
        executor = self._active_flow_executors.get(project_id)
        if executor is not None:
            executor.cancel()

    async def resume_flow(
        self, project_name: str, flow_json: dict | str, phase_id: str,
        template_name: str = "Resumed Flow",
    ) -> str:
        """Resume a dead pipeline from where it left off.

        Reconstructs results from completed DB tasks, re-runs instant nodes,
        and starts remaining LLM tasks. Auto-approves plan_applier.
        """
        self._assert_ready()
        from taktis.core.graph_executor import GraphExecutor
        executor = GraphExecutor(
            self, project_name, flow_json, template_name=template_name,
        )
        project = await self.get_project(project_name)
        if project:
            self._active_flow_executors[project["id"]] = executor
        try:
            result = await executor.resume_flow(phase_id)
        finally:
            if project:
                self._active_flow_executors.pop(project["id"], None)
        return result

    # ------------------------------------------------------------------
    # Monitoring & Queries  (→ ProjectService)
    # ------------------------------------------------------------------

    async def get_status(self) -> dict:
        self._assert_ready()
        return await self._project_service.get_status()

    async def get_interrupted_work(self) -> dict:
        self._assert_ready()
        return await self._project_service.get_interrupted_work()

    async def get_task_output(
        self, task_id: str, tail: int | None = None,
        event_types: list[str] | None = None,
    ) -> list[dict]:
        self._assert_ready()
        return await self._project_service.get_task_output(
            task_id, tail=tail, event_types=event_types,
        )

    async def watch_task(self, task_id: str) -> AsyncIterator[dict]:
        self._assert_ready()
        return self._project_service.watch_task(task_id)

    # ------------------------------------------------------------------
    # Expert Management  (→ ProjectService)
    # ------------------------------------------------------------------

    async def list_experts(self) -> list[dict]:
        self._assert_ready()
        return await self._project_service.list_experts()

    async def publish_event(self, event: str, data: dict) -> None:
        """Publish an event on the EventBus (satisfies PlanApplier protocol)."""
        await self.event_bus.publish(event, data)

    async def get_expert(self, name: str) -> dict | None:
        self._assert_ready()
        return await self._project_service.get_expert(name)

    async def create_expert(
        self, name: str, file_content: str = "",
        description: str = "", system_prompt: str = "",
        category: str = "",
    ) -> dict:
        self._assert_ready()
        return await self._project_service.create_expert(
            name, file_content=file_content, description=description,
            system_prompt=system_prompt, category=category,
        )

    async def update_expert(self, name: str, **kwargs) -> dict | None:
        self._assert_ready()
        return await self._project_service.update_expert(name, **kwargs)

    async def delete_expert(self, name: str) -> bool:
        self._assert_ready()
        return await self._project_service.delete_expert(name)

    # ------------------------------------------------------------------
    # Agent Templates  (→ AgentTemplateRegistry)
    # ------------------------------------------------------------------

    async def list_agent_templates(self) -> list[dict]:
        self._assert_ready()
        return await self.agent_template_registry.list_templates()

    async def get_agent_template(self, slug: str) -> dict | None:
        self._assert_ready()
        return await self.agent_template_registry.get_template(slug)

    async def create_agent_template(
        self, slug: str, name: str, description: str = "",
        prompt_text: str = "", auto_variables: list[str] | None = None,
        internal_variables: list[str] | None = None,
    ) -> dict:
        self._assert_ready()
        return await self.agent_template_registry.create_template(
            slug=slug, name=name, description=description,
            prompt_text=prompt_text, auto_variables=auto_variables,
            internal_variables=internal_variables,
        )

    async def update_agent_template(self, slug: str, **kwargs) -> dict | None:
        self._assert_ready()
        async with get_session() as conn:
            return await repo.update_agent_template(conn, slug, **kwargs)

    async def delete_agent_template(self, slug: str) -> bool:
        self._assert_ready()
        return await self.agent_template_registry.delete_template(slug)


