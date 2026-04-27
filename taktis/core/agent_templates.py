"""Agent template registry – manages built-in and custom prompt templates."""

from __future__ import annotations

import importlib.resources
import json
import logging
from typing import Any

import yaml

from taktis import repository as repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter parser (shared pattern with experts.py)
# ---------------------------------------------------------------------------

def _parse_template_md(text: str) -> tuple[dict[str, Any], str]:
    """Parse a markdown file with YAML frontmatter.

    Returns a ``(metadata, body)`` tuple where *metadata* is the parsed YAML
    dict and *body* is everything after the closing ``---``.
    """
    text = text.strip()
    if not text.startswith("---"):
        raise ValueError("Template markdown file must start with YAML frontmatter (---)")
    try:
        end_idx = text.index("---", 3)
    except ValueError:
        raise ValueError("Template markdown file missing closing '---' delimiter")
    frontmatter_raw = text[3:end_idx].strip()
    body = text[end_idx + 3:].strip()
    metadata = yaml.safe_load(frontmatter_raw)
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter must be a YAML mapping")
    return metadata, body


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class AgentTemplateRegistry:
    """Manages built-in and custom agent prompt templates.

    Built-in templates are loaded from ``taktis/agent_templates/*.md``
    (markdown files with YAML frontmatter).  Custom templates are stored in
    the database and can be created/deleted at runtime.
    """

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    # -- built-in loading ----------------------------------------------------

    async def load_builtins(self) -> None:
        """Load built-in templates from ``taktis/agent_templates/*.md``.

        Each file has YAML frontmatter with ``slug``, ``name``, ``description``,
        ``auto_variables``, ``internal_variables``.  The body is the prompt text.

        Templates matched by slug are updated if content changed; new ones are
        inserted.
        """
        templates_package = importlib.resources.files("taktis") / "agent_templates"

        md_files = []
        for entry in templates_package.iterdir():
            if entry.name.endswith(".md"):
                md_files.append(entry)

        if not md_files:
            logger.warning("No built-in template files found in taktis/agent_templates/")
            return

        async with self._session_factory() as conn:
            for md_path in sorted(md_files, key=lambda p: p.name):
                try:
                    text = md_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.error("Skipping %s: %s", md_path.name, exc)
                    continue
                try:
                    metadata, body = _parse_template_md(text)
                except (ValueError, yaml.YAMLError) as exc:
                    logger.error("Skipping %s: %s", md_path.name, exc)
                    continue

                slug = metadata.get("slug")
                template_id = metadata.get("id")
                if not slug:
                    logger.error("Skipping %s: missing 'slug' in frontmatter", md_path.name)
                    continue

                # Check if already exists by ID first, then by slug
                existing = None
                if template_id:
                    existing = await repo.get_agent_template_by_id(conn, template_id)
                if existing is None:
                    existing = await repo.get_agent_template_by_slug(conn, slug)

                auto_vars = metadata.get("auto_variables", [])
                internal_vars = metadata.get("internal_variables", [])

                if existing is not None:
                    # Compare and update if changed
                    new_name = metadata.get("name", slug)
                    new_desc = metadata.get("description", "")
                    new_auto = json.dumps(auto_vars) if isinstance(auto_vars, list) else (auto_vars or "[]")
                    new_internal = json.dumps(internal_vars) if isinstance(internal_vars, list) else (internal_vars or "[]")

                    existing_auto = existing.get("auto_variables") or "[]"
                    existing_internal = existing.get("internal_variables") or "[]"

                    needs_update = (
                        existing.get("prompt_text") != body
                        or existing.get("name") != new_name
                        or existing.get("description") != new_desc
                        or existing_auto != new_auto
                        or existing_internal != new_internal
                    )

                    # Migrate ID if needed
                    if template_id and existing.get("id") != template_id:
                        await repo.update_agent_template_id(conn, existing["id"], template_id)
                        needs_update = True

                    if needs_update:
                        await repo.update_agent_template(
                            conn,
                            existing.get("slug", slug),
                            name=new_name,
                            description=new_desc,
                            prompt_text=body,
                            auto_variables=auto_vars,
                            internal_variables=internal_vars,
                        )
                        logger.info("Updated built-in template '%s'", slug)
                    else:
                        logger.debug("Built-in template '%s' unchanged – skipping", slug)
                    continue

                # Insert new
                await repo.create_agent_template(
                    conn,
                    id=template_id,
                    slug=slug,
                    name=metadata.get("name", slug),
                    description=metadata.get("description", ""),
                    prompt_text=body,
                    auto_variables=auto_vars,
                    internal_variables=internal_vars,
                    is_builtin=True,
                )
                logger.info("Loaded built-in template '%s' (id=%s)", slug, template_id)

    # -- public CRUD ---------------------------------------------------------

    async def get_template(self, slug: str) -> dict[str, Any] | None:
        """Return a template dict by *slug*, or ``None``."""
        async with self._session_factory() as conn:
            return await repo.get_agent_template_by_slug(conn, slug)

    async def list_templates(self) -> list[dict[str, Any]]:
        """Return all templates as a list of dicts."""
        async with self._session_factory() as conn:
            return await repo.list_agent_templates(conn)

    async def create_template(
        self,
        slug: str,
        name: str,
        description: str = "",
        prompt_text: str = "",
        auto_variables: list[str] | None = None,
        internal_variables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a custom (non-built-in) template."""
        async with self._session_factory() as conn:
            return await repo.create_agent_template(
                conn,
                slug=slug,
                name=name,
                description=description,
                prompt_text=prompt_text,
                auto_variables=auto_variables or [],
                internal_variables=internal_variables or [],
                is_builtin=False,
            )

    async def delete_template(self, slug: str) -> bool:
        """Delete a custom template. Raises ValueError for builtins."""
        async with self._session_factory() as conn:
            row = await repo.get_agent_template_by_slug(conn, slug)
            if row is None:
                return False
            if row.get("is_builtin"):
                raise ValueError(f"Cannot delete built-in template '{slug}'")
            return await repo.delete_agent_template(conn, slug)
