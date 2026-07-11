from packages.core.ai.runtime.capabilities import BusinessCapability


def test_capability_has_optional_output_shape_field():
    cap = BusinessCapability(
        id="file.write",
        name="x",
        description="d",
        tool_names=(),
        profiles=(),
        risk_level="write",
        required_approval=False,
        metadata={},
        output_shape="ArtifactResult",
    )
    assert cap.output_shape == "ArtifactResult"


def test_capability_output_shape_defaults_none():
    cap = BusinessCapability(
        id="x",
        name="x",
        description="d",
        tool_names=(),
        profiles=(),
        risk_level="safe",
        required_approval=False,
        metadata={},
    )
    assert cap.output_shape is None
