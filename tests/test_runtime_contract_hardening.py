from __future__ import annotations

import pytest

from packages.core.ai.runtime.skill_identity import (
    SkillCatalogKey,
    SkillInvocationFactory,
)
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolAuthorization,
    RuntimeToolClassification,
    RuntimeToolEffect,
)
from packages.core.ai.runtime.video_routing import (
    ExistingVideoEditStrategy,
    VideoRequest,
    VideoRouteFactory,
    VideoGenerationMode,
    VideoOutputType,
)
from packages.core.services.runtime_authorization.domain import (
    RuntimeAuthorizationAccess,
)


def test_runtime_tool_classification_enforces_effect_access_pairing() -> None:
    authorization = RuntimeToolAuthorization(
        action_key="runtime.control.submit_result",
        capability_id=None,
        access=RuntimeAuthorizationAccess.READ,
    )

    with pytest.raises(ValueError, match="control classification requires control access"):
        RuntimeToolClassification.control("runtime-owned control", authorization)


def test_video_request_normalizes_direct_constructor_values() -> None:
    request = VideoRequest(
        generation_mode="ai_video",
        output_type="edit_existing",
    )

    strategy = VideoRouteFactory.resolve(request)

    assert isinstance(strategy, ExistingVideoEditStrategy)
    assert request.generation_mode is VideoGenerationMode.AI_VIDEO
    assert request.output_type is VideoOutputType.EDIT_EXISTING


def test_skill_invocation_factory_snapshots_resolved_ids() -> None:
    skill_ids = {
        SkillCatalogKey.CODING_BASED_VIDEO.value: "skill_video_01",
    }
    factory = SkillInvocationFactory(skill_ids)

    skill_ids[SkillCatalogKey.CODING_BASED_VIDEO.value] = "skill_video_02"

    call = factory.build(SkillCatalogKey.CODING_BASED_VIDEO, "Render the complete video.")

    assert call["arguments"]["skill_id"] == "skill_video_01"
    with pytest.raises(TypeError):
        factory.skill_ids_by_catalog_key[
            SkillCatalogKey.CODING_BASED_VIDEO.value
        ] = "skill_video_03"
