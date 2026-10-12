"""Opt in an existing Workspace to a Blueprint's evidence assessment producer.

Dry run by default. --apply changes only the named Workspace's metric producer;
it does not seed scores, start business Tasks, or execute external actions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.workspace import Workspace
from packages.core.stats.review_setup import configure_review
from packages.core.stats.service import list_stats
from packages.core.blueprints.payload import _validate_stat_assessment_producers


async def main(args):
    recipe = json.loads(Path(args.blueprint).read_text())["recipe"]
    _validate_stat_assessment_producers(recipe)
    definitions = [item for item in recipe.get("stats", []) if item.get("collector_type") == "agent_review"]
    if not definitions:
        raise ValueError("Blueprint has no evidence assessment metrics")
    slugs = {item["collector_config"]["workflow_slug"] for item in definitions}
    if len(slugs) != 1:
        raise ValueError("Configure one assessment batch at a time")
    workflow = next(item for item in recipe["workflows"] if item["slug"] in slugs)
    producers = [step for step in workflow["steps"] if step.get("config", {}).get("stat_assessment_keys")]
    if len(producers) != 1:
        raise ValueError("Expected one assessment agent")
    rubrics = {item["key"]: item["collector_config"]["assessment"]["rubric"] for item in definitions}
    service_key = producers[0]["config"]["service_key"]
    async with async_session() as db:
        workspace = (await db.execute(select(Workspace).where(
            Workspace.id == args.workspace_id, Workspace.entity_id == args.entity_id,
            Workspace.deleted_at.is_(None),
        ))).scalar_one()
        current = {stat.key: stat for stat in await list_stats(db, entity_id=args.entity_id, workspace_id=workspace.id)}
        if not set(rubrics) <= current.keys():
            raise ValueError("Workspace is missing required metrics; refusing partial setup")
        preview = {"workspace_id": workspace.id, "service_key": service_key,
                   "changes": [{"key": key, "from": current[key].collector_type, "to": "agent_review"} for key in rubrics],
                   "schedule": "one weekly Workflow Automation", "scores_written": 0}
        if args.apply:
            preview["configured"] = await configure_review(db, workspace=workspace, user_id=args.user_id,
                                                           service_key=service_key, rubrics=rubrics)
            await db.commit()
        else:
            preview["dry_run"] = True
        print(json.dumps(preview, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args()))
