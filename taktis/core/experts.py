"""Expert registry – manages built-in and custom expert personas."""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Any

import yaml

from taktis import repository as repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter parser – avoids pulling in a heavy dependency just for this.
# ---------------------------------------------------------------------------

def _parse_expert_md(text: str) -> tuple[dict[str, str], str]:
    """Parse a markdown file with YAML frontmatter.

    Returns a ``(metadata, body)`` tuple where *metadata* is the parsed YAML
    dict and *body* is everything after the closing ``---``.
    """
    text = text.strip()
    if not text.startswith("---"):
        raise ValueError("Expert markdown file must start with YAML frontmatter (---)")

    # Find the closing ---
    try:
        end_idx = text.index("---", 3)
    except ValueError:
        raise ValueError("Expert markdown file missing closing '---' delimiter")
    frontmatter_raw = text[3:end_idx].strip()
    body = text[end_idx + 3:].strip()

    metadata = yaml.safe_load(frontmatter_raw)
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter must be a YAML mapping")

    return metadata, body


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ExpertRegistry:
    """Manages built-in and custom expert personas.

    Built-in experts are loaded from the ``taktis/experts/`` package
    directory (markdown files with YAML frontmatter).  Custom experts are
    stored in the database and can be created/deleted at runtime.
    """

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    # -- built-in loading ----------------------------------------------------

    async def load_builtins(self) -> None:
        """Load built-in experts from ``taktis/experts/*.md`` into the DB.

        Each markdown file is expected to have YAML frontmatter with ``name``,
        ``description``, and ``category`` keys.  The body of the file is used
        as the expert's system prompt.

        Experts that already exist in the database (matched by name) are
        skipped so that manual edits are preserved across restarts.
        """
        experts_package = importlib.resources.files("taktis") / "experts"

        md_files: list[Path] = []
        # importlib.resources may return a Traversable; iterate its children.
        for entry in experts_package.iterdir():
            if entry.name.endswith(".md"):
                md_files.append(entry)

        if not md_files:
            logger.warning("No built-in expert files found in taktis/experts/")
            return

        async with self._session_factory() as conn:
            for md_path in sorted(md_files, key=lambda p: p.name):
                try:
                    text = md_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.error("Skipping %s: %s", md_path.name, exc)
                    continue
                try:
                    metadata, body = _parse_expert_md(text)
                except (ValueError, yaml.YAMLError) as exc:
                    logger.error("Skipping %s: %s", md_path.name, exc)
                    continue

                name = metadata.get("name")
                expert_id = metadata.get("id")
                if not name:
                    logger.error("Skipping %s: missing 'name' in frontmatter", md_path.name)
                    continue

                # Check whether this expert already exists (by stable ID first, then name).
                existing = None
                if expert_id:
                    existing = await repo.get_expert_by_id(conn, expert_id)
                if existing is None:
                    existing = await repo.get_expert_by_name(conn, name)

                if existing is not None:
                    # Update if the .md file content has changed
                    new_desc = metadata.get("description", "")
                    new_cat = metadata.get("category", "")
                    new_role = metadata.get("role")
                    new_task_type = metadata.get("task_type")
                    new_pipeline_internal = bool(metadata.get("pipeline_internal", False))
                    new_is_default = bool(metadata.get("is_default", False))
                    needs_update = (
                        existing.get("system_prompt") != body
                        or existing.get("description") != new_desc
                        or existing.get("category") != new_cat
                        or existing.get("name") != name
                        or existing.get("role") != new_role
                        or existing.get("task_type") != new_task_type
                        or bool(existing.get("pipeline_internal")) != new_pipeline_internal
                        or bool(existing.get("is_default")) != new_is_default
                    )
                    # Migrate to stable ID if the DB row has a different ID
                    if expert_id and existing.get("id") != expert_id:
                        await repo.update_expert_id(conn, existing["id"], expert_id)
                        needs_update = True
                    if needs_update:
                        update_kwargs = dict(
                            system_prompt=body,
                            description=new_desc,
                            category=new_cat,
                            role=new_role,
                            task_type=new_task_type,
                            pipeline_internal=int(new_pipeline_internal),
                            is_default=int(new_is_default),
                        )
                        # Only pass name if it actually changed
                        lookup_name = existing.get("name", name)
                        if lookup_name != name:
                            update_kwargs["name"] = name
                        await repo.update_expert(conn, lookup_name, **update_kwargs)
                        logger.info("Updated built-in expert '%s'", name)
                    else:
                        logger.debug("Built-in expert '%s' unchanged – skipping", name)
                    continue

                await repo.create_expert(
                    conn,
                    id=expert_id,
                    name=name,
                    description=metadata.get("description", ""),
                    system_prompt=body,
                    category=metadata.get("category", ""),
                    is_builtin=True,
                    role=metadata.get("role"),
                    task_type=metadata.get("task_type"),
                    pipeline_internal=bool(metadata.get("pipeline_internal", False)),
                    is_default=bool(metadata.get("is_default", False)),
                )
                logger.info("Loaded built-in expert '%s' (id=%s)", name, expert_id)

    # -- public CRUD ---------------------------------------------------------

    async def get_expert(self, name: str) -> dict[str, Any] | None:
        """Return an expert dict by *name*, or ``None`` if not found."""
        async with self._session_factory() as conn:
            row = await repo.get_expert_by_name(conn, name)
            if row is None:
                return None
            return {
                "name": row["name"],
                "description": row.get("description"),
                "system_prompt": row.get("system_prompt"),
                "category": row.get("category"),
                "is_builtin": bool(row.get("is_builtin")),
            }

    async def list_experts(self) -> list[dict[str, Any]]:
        """Return all experts as a list of dicts."""
        async with self._session_factory() as conn:
            rows = await repo.list_experts(conn)
            return [
                {
                    "id": r.get("id"),
                    "name": r["name"],
                    "description": r.get("description"),
                    "system_prompt": r.get("system_prompt"),
                    "category": r.get("category"),
                    "is_builtin": bool(r.get("is_builtin")),
                    "role": r.get("role"),
                    "task_type": r.get("task_type"),
                    "pipeline_internal": bool(r.get("pipeline_internal")),
                    "is_default": bool(r.get("is_default")),
                }
                for r in rows
            ]

    async def create_expert(
        self,
        name: str,
        description: str,
        system_prompt: str,
        category: str,
    ) -> dict[str, Any]:
        """Create a custom (non-built-in) expert and return it as a dict."""
        async with self._session_factory() as conn:
            row = await repo.create_expert(
                conn,
                name=name,
                description=description,
                system_prompt=system_prompt,
                category=category,
                is_builtin=False,
            )
            return {
                "name": row["name"],
                "description": row.get("description"),
                "system_prompt": row.get("system_prompt"),
                "category": row.get("category"),
                "is_builtin": bool(row.get("is_builtin")),
            }

    async def delete_expert(self, name: str) -> bool:
        """Delete a custom expert by *name*.

        Returns ``True`` if the expert was deleted, ``False`` if it was not
        found.  Raises ``ValueError`` if the expert is a built-in.
        """
        async with self._session_factory() as conn:
            row = await repo.get_expert_by_name(conn, name)
            if row is None:
                return False
            if row.get("is_builtin"):
                raise ValueError(f"Cannot delete built-in expert '{name}'")
            return await repo.delete_expert(conn, name)


# ---------------------------------------------------------------------------
# Prompt helper – dynamic expert options for roadmapper / interview prompts
# ---------------------------------------------------------------------------


async def format_expert_options(session_factory) -> str:
    """Build an expert options string for prompts from registered experts.

    Excludes pipeline-internal experts. Marks the default expert.
    """
    async with session_factory() as conn:
        experts = await repo.list_experts(conn)
    lines = []
    for e in sorted(experts, key=lambda x: x["name"]):
        if e.get("pipeline_internal"):
            continue
        desc = e.get("description", "")
        default = " (DEFAULT — use for most tasks)" if e.get("is_default") else ""
        lines.append(f"- {e['name']}: {desc}{default}")
    return "Expert options:\n" + "\n".join(lines)
