import json

from packages.core.ai.runtime.output_policy import runtime_public_tool_result


def test_failed_media_jobs_keep_actionable_ids_without_provider_details() -> None:
    raw = json.dumps({
        "kind": "media_jobs",
        "status": "failed",
        "jobs": [
            {
                "job_id": "job-failed",
                "kind": "video",
                "status": "failed",
                "error": "Provider request 123: secret diagnostic",
                "result_url": "https://example.invalid/clip?X-Amz-Signature=secret",
            },
            {
                "job_id": "job-complete",
                "kind": "video",
                "status": "completed",
                "document_id": "document-1",
            },
        ],
        "failed_job_ids": ["job-failed"],
        "missing_job_ids": [],
        "pending_job_ids": [],
    })

    projected = runtime_public_tool_result(raw)
    assert json.loads(projected) == {
        "kind": "media_jobs",
        "status": "failed",
        "jobs": [
            {"job_id": "job-failed", "kind": "video", "status": "failed"},
            {
                "job_id": "job-complete",
                "kind": "video",
                "status": "completed",
                "document_id": "document-1",
            },
        ],
        "failed_job_ids": ["job-failed"],
        "missing_job_ids": [],
        "pending_job_ids": [],
        "retry_guidance": "Retry failed clips with another generation method or use an existing still image.",
    }
    assert runtime_public_tool_result(projected) == projected
    assert "secret" not in projected
