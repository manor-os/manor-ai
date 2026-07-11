import json

from packages.core.services.chat_artifacts import chat_attachments_from_tool_results


def test_chat_attachments_from_generated_media_tool_results():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "image",
                    "name": "hero.png",
                    "document_id": "doc_img",
                    "result_url": "/api/v1/fs/ent/images/hero.png",
                    "mime_type": "image/png",
                }
            ),
        },
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "video",
                    "name": "intro.mp4",
                    "document_id": "doc_vid",
                    "result_url": "/api/v1/fs/ent/videos/intro.mp4",
                    "mime_type": "video/mp4",
                }
            ),
        },
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "pdf",
                    "document": {
                        "id": "doc_pdf",
                        "name": "brief.pdf",
                        "fs_path": "documents/brief.pdf",
                        "file_type": "pdf",
                        "mime_type": "application/pdf",
                    },
                }
            ),
        },
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "hero.png",
            "id": "doc_img",
            "type": "knowledge",
            "fileType": "png",
            "mimeType": "image/png",
            "previewUrl": "/api/v1/fs/ent/images/hero.png",
        },
        {
            "name": "intro.mp4",
            "id": "doc_vid",
            "type": "knowledge",
            "fileType": "mp4",
            "mimeType": "video/mp4",
            "previewUrl": "/api/v1/fs/ent/videos/intro.mp4",
        },
        {
            "name": "brief.pdf",
            "id": "doc_pdf",
            "type": "knowledge",
            "fileType": "pdf",
            "mimeType": "application/pdf",
            "previewUrl": "/api/v1/fs/ent/documents/brief.pdf",
        },
    ]


def test_chat_attachments_from_generated_code_bundle_files():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": {
                "created": True,
                "kind": "code",
                "files": [
                    {
                        "path": "code/site/index.html",
                        "url": "/api/v1/fs/ent/code/site/index.html",
                        "document_id": "doc_html",
                    },
                    {
                        "path": "code/site/styles.css",
                        "url": "/api/v1/fs/ent/code/site/styles.css",
                        "document_id": "doc_css",
                    },
                ],
            },
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "index.html",
            "id": "doc_html",
            "type": "knowledge",
            "fileType": "html",
            "mimeType": "text/html",
            "previewUrl": "/api/v1/fs/ent/code/site/index.html",
        },
        {
            "name": "styles.css",
            "id": "doc_css",
            "type": "knowledge",
            "fileType": "css",
            "mimeType": "text/css",
            "previewUrl": "/api/v1/fs/ent/code/site/styles.css",
        },
    ]


def test_chat_attachments_ignore_input_upload_references():
    tool_results = [
        {
            "name": "read_file",
            "raw_result": {
                "path": "uploads/chat/source.png",
                "url": "/api/v1/fs/ent/uploads/chat/source.png",
                "name": "source.png",
            },
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_sandbox_save_result_is_not_chat_attachment_by_default():
    tool_results = [
        {
            "name": "sandbox_save_result",
            "raw_result": json.dumps(
                {
                    "saved": True,
                    "saved_to_knowledge": True,
                    "document_id": "doc_txt",
                    "name": "file.txt",
                    "fs_path": "file.txt",
                    "result_url": "/api/v1/fs/ent/file.txt",
                    "mime_type": "text/plain",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_sandbox_save_result_can_opt_into_final_chat_attachment():
    tool_results = [
        {
            "name": "sandbox_save_result",
            "raw_result": json.dumps(
                {
                    "saved": True,
                    "saved_to_knowledge": True,
                    "display_as_artifact": True,
                    "artifact_role": "final",
                    "document_id": "doc_pdf",
                    "name": "final-report.pdf",
                    "fs_path": "reports/final-report.pdf",
                    "result_url": "/api/v1/fs/ent/reports/final-report.pdf",
                    "mime_type": "application/pdf",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == [
        {
            "name": "final-report.pdf",
            "id": "doc_pdf",
            "type": "knowledge",
            "fileType": "pdf",
            "mimeType": "application/pdf",
            "previewUrl": "/api/v1/fs/ent/reports/final-report.pdf",
        }
    ]


def test_chat_attachments_ignore_document_id_without_filesystem_reference():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "pdf",
                    "document_id": "doc_pdf",
                    "name": "draft.pdf",
                    "mime_type": "application/pdf",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_chat_attachments_ignore_external_url_without_filesystem_reference():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "created": True,
                    "kind": "image",
                    "name": "remote.png",
                    "result_url": "https://cdn.example.com/remote.png",
                    "mime_type": "image/png",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []


def test_chat_attachments_ignore_non_terminal_generated_files():
    tool_results = [
        {
            "name": "generate_file",
            "raw_result": json.dumps(
                {
                    "status": "processing",
                    "created": True,
                    "kind": "video",
                    "name": "clip.mp4",
                    "document_id": "doc_video",
                    "fs_path": "videos/clip.mp4",
                    "result_url": "/api/v1/fs/ent/videos/clip.mp4",
                    "mime_type": "video/mp4",
                }
            ),
        }
    ]

    assert chat_attachments_from_tool_results(tool_results) == []
