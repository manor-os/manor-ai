"""Bundled template recipes.

Each recipe imports + calls ``packages.core.templates.registry.register``
at module load time, so simply importing this package wires every
recipe into the global REGISTRY.
"""
from packages.core.templates.recipes import (  # noqa: F401
    daily_briefing,
    email_triage,
    twitter_growth,
    solo_content_creator,
    solo_services,
    solo_ecommerce,
)
