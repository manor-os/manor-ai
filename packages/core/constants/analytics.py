"""Shared event vocabulary for durable product analytics facts."""
from enum import StrEnum


class ProductGrowthMilestone(StrEnum):
    """Durable product facts used by the activation funnel."""

    AUTOMATION_CREATED = "automation_created"
    AUTOMATION_SUCCEEDED = "automation_succeeded"
