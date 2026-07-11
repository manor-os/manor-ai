def test_hitl_requests_from_data_preserves_operation_and_token_id() -> None:
    from packages.core.services.hitl_requests import hitl_requests_from_data

    requests = hitl_requests_from_data(
        {
            "approval_token": "hitl_1",
            "hitl": {
                "type": "approval",
                "prompt": "Approve command?",
                "tool": "bash",
            },
            "operation": {
                "tool": "bash",
                "action_key": "workspace.file.modify",
            },
        }
    )

    assert requests == [
        {
            "id": "hitl_1",
            "type": "approval",
            "prompt": "Approve command?",
            "tool": "bash",
            "operation": {
                "tool": "bash",
                "action_key": "workspace.file.modify",
            },
        }
    ]
