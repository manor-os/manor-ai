from types import SimpleNamespace

from packages.core.services.workspace_access import (
    WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE,
    WORKSPACE_ACCESS_MODE_KEY,
    WORKSPACE_ACCESS_MODE_MEMBERS_ONLY,
    settings_with_default_workspace_access,
    workspace_access_mode,
)


def test_workspace_access_mode_missing_settings_defaults_to_members_only():
    assert workspace_access_mode(SimpleNamespace(settings=None)) == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert workspace_access_mode(SimpleNamespace(settings={})) == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert (
        workspace_access_mode(SimpleNamespace(settings={WORKSPACE_ACCESS_MODE_KEY: "unexpected"}))
        == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    )


def test_workspace_access_mode_preserves_explicit_entity_visible():
    workspace = SimpleNamespace(settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE})

    assert workspace_access_mode(workspace) == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE


def test_workspace_access_settings_helper_sets_secure_default():
    assert settings_with_default_workspace_access({})[WORKSPACE_ACCESS_MODE_KEY] == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert (
        settings_with_default_workspace_access({WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE})[
            WORKSPACE_ACCESS_MODE_KEY
        ]
        == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
    )
