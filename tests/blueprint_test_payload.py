"""Test-only Blueprint components, independent of the published catalogue."""


def load_upgrade_blueprint(slug: str) -> dict:
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "slug": slug,
            "title": "Workspace Upgrade Test",
            "kind": "test",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {"tools": [], "mcp_servers": [], "skills": [], "agents": []},
        },
        "embedded": {
            "skills": [{
                "slug": "upgrade-test-skill",
                "name": "Upgrade Test Skill",
                "system_prompt": (
                    "Summarize the supplied test notes and identify missing evidence. "
                    "Keep source references with each conclusion so the reviewer can "
                    "verify the complete procedure beyond its opening sentence. "
                    "Return a concise review with the unresolved questions."
                ),
                "tools": [],
                "version": "1.0.0",
            }],
            "agents": [{
                "slug": "upgrade-test-agent",
                "name": "Upgrade Test Agent",
                "system_prompt": "Review the supplied notes.",
                "skill_bindings": ["upgrade-test-skill"],
                "tool_bindings": [],
                "mcp_bindings": [],
            }],
            "knowledge_packs": [{
                "slug": "upgrade-test-notes",
                "title": "Upgrade Test Notes",
                "mode": "inline_text",
                "folder_structure": [],
                "starter_documents": [{
                    "path": "notes.md",
                    "body_md": "# Test notes\nA deterministic upgrade fixture.",
                }],
            }],
        },
        "recipe": {
            "operating_model": {"kind": "test", "primary_work": "Review test notes."},
            "strategist": None,
            "prompts": [],
            "subscriptions": [{
                "agent_slug": "upgrade-test-agent",
                "service_key": "test.review",
                "config": {},
            }],
            "scheduled_jobs": [],
            "workflows": [{
                "slug": "upgrade-test-workflow",
                "name": "Upgrade Test Workflow",
                "version": 1,
                "trigger_type": "manual",
                "trigger_config": {},
                "variables": [],
                "steps": [{
                    "id": "review",
                    "agent_slug": "upgrade-test-agent",
                    "input": "Review the supplied test notes.",
                    "next": [],
                }],
            }],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }
