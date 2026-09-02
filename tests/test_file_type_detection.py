from __future__ import annotations

import json

import pytest

from packages.core.services.file_type_detection import detect_file_type, mime_for_extension


def test_detect_file_type_preserves_declared_code_extensions(tmp_path):
    samples = {
        "styles.css": "body { color: #123; }\n",
        "app.js": "console.log('ready');\n",
        "main.tsx": "export function App() { return <main />; }\n",
        "config.yaml": "name: demo\n",
        "notes.md": "Plain markdown without a heading.\n",
    }

    for filename, content in samples.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        detected = detect_file_type(str(path), declared_name=filename)
        assert detected.extension == filename.rsplit(".", 1)[1]
        assert detected.display_name == filename
        assert detected.mismatch is False


def test_detect_file_type_still_upgrades_txt_html(tmp_path):
    path = tmp_path / "page.txt"
    path.write_text("<!doctype html><html><body>Hello</body></html>", encoding="utf-8")

    detected = detect_file_type(str(path), declared_name="page.txt")

    assert detected.extension == "html"
    assert detected.display_name == "page.html"
    assert detected.mismatch is True
    assert mime_for_extension("css") == "text/css"


def test_detect_file_type_preserves_editable_diagram_identity(tmp_path):
    path = tmp_path / "workspace-flow.diagram.json"
    path.write_text(
        '{"version":"editable_diagram_v1","elements":[]}',
        encoding="utf-8",
    )

    detected = detect_file_type(
        str(path),
        declared_name="workspace-flow.diagram.json",
    )

    assert detected.extension == "diagram.json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == "workspace-flow.diagram.json"
    assert detected.mismatch is False


@pytest.mark.parametrize("extension", ["diagram.json", "diagram"])
def test_detect_file_type_preserves_diagram_identity_with_code_fence(tmp_path, extension):
    filename = f"workspace-flow.{extension}"
    path = tmp_path / filename
    path.write_text(
        '{"version":"editable_diagram_v1","prompt":"```mermaid flowchart TD```","elements":[]}',
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == extension
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    "content",
    [
        '{"notes":"```mermaid flowchart TD```"}',
        '["```mermaid flowchart TD```"]',
        '["```mermaid flowchart TD```","' + ("x" * 5000) + '"]',
    ],
)
def test_detect_file_type_preserves_json_identity_with_code_fence(tmp_path, content):
    filename = "payload.json"
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize("tail", ["# Heading", "```mermaid"])
def test_detect_file_type_preserves_markdown_markers_inside_truncated_json_string(
    tmp_path,
    tail,
):
    filename = "payload.json"
    path = tmp_path / filename
    content = '["' + ("x" * 4094) + tail + '"]'
    json.loads(content)
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


def test_detect_file_type_rejects_complete_json_prefix_with_later_markdown(tmp_path):
    filename = "notes.md"
    path = tmp_path / filename
    path.write_text(
        "[1]" + (" " * (4096 - len("[1]"))) + "\n# Heading\n```python\nprint('ok')\n```",
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == filename
    assert detected.mismatch is False


def test_detect_file_type_accepts_complete_json_with_long_whitespace_tail(tmp_path):
    path = tmp_path / "payload.txt"
    path.write_text("[1]" + (" " * 5000), encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=path.name)

    assert detected.extension == "json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == "payload.json"
    assert detected.mismatch is True


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("payload.json", "payload.txt"),
        ("payload.diagram.json", "payload.diagram.txt"),
        ("payload.diagram", "payload.txt"),
    ],
)
def test_detect_file_type_rejects_second_json_value_after_sniff_boundary(
    tmp_path,
    filename,
    expected_name,
):
    path = tmp_path / filename
    content = "[1]" + (" " * (4096 - len("[1]"))) + '"second"'
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        json.loads(content)
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "txt"
    assert detected.mime_type == "text/plain"
    assert detected.display_name == expected_name
    assert detected.mismatch is True


def test_detect_file_type_rejects_invalid_truncated_json_object(tmp_path):
    filename = "notes.md"
    path = tmp_path / filename
    path.write_text(
        '{"status": pending}\n```text\nnot json\n```\n' + ("x" * 5000),
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize("suffix", ["", "\nplain notes, never closed"])
def test_detect_file_type_rejects_unterminated_long_json_string(tmp_path, suffix):
    filename = "notes.md"
    path = tmp_path / filename
    path.write_text(
        '{"notes":"' + ("x" * 5000) + suffix,
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("payload.json", "payload.txt"),
        ("payload.diagram.json", "payload.diagram.txt"),
        ("payload.diagram", "payload.txt"),
    ],
)
@pytest.mark.parametrize(
    "content",
    [
        '{"status": pending}',
        '{"notes":"' + ("x" * 5000),
    ],
    ids=["short-invalid-token", "long-unterminated-string"],
)
def test_detect_file_type_rejects_confirmed_invalid_declared_json(
    tmp_path,
    filename,
    expected_name,
    content,
):
    path = tmp_path / filename
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "txt"
    assert detected.mime_type == "text/plain"
    assert detected.display_name == expected_name
    assert detected.mismatch is True


def test_detect_file_type_preserves_unvalidated_large_declared_json(tmp_path):
    filename = "payload.json"
    path = tmp_path / filename
    path.write_text('["' + ("x" * (8 * 1024 * 1024)) + '"]', encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    ("filename", "expected_extension"),
    [
        ("payload.json", "json"),
        ("workspace-flow.diagram.json", "diagram.json"),
        ("workspace-flow.diagram", "diagram"),
    ],
)
def test_detect_file_type_preserves_large_json_with_markdown_inside_string(
    tmp_path,
    filename,
    expected_extension,
):
    path = tmp_path / filename
    content = (
        '{"prompt":"```mermaid flowchart TD```","data":"'
        + ("x" * (8 * 1024 * 1024))
        + '"}'
    )
    json.loads(content)
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == expected_extension
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    "error_type",
    [ValueError, RecursionError],
    ids=["integer-limit", "recursion-limit"],
)
def test_detect_file_type_preserves_declared_json_when_validation_hits_runtime_limit(
    tmp_path,
    monkeypatch,
    error_type,
):
    filename = "payload.json"
    path = tmp_path / filename
    path.write_text("[" + ("1" * 5000) + "]", encoding="utf-8")

    def raise_parser_limit(_file):
        raise error_type("runtime parser limit")

    monkeypatch.setattr(
        "packages.core.services.file_type_detection.json.load",
        raise_parser_limit,
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "json"
    assert detected.mime_type == "application/json"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    ("extension", "expected_name"),
    [("diagram.json", "workspace-flow.diagram.md"), ("diagram", "workspace-flow.md")],
)
def test_detect_file_type_rejects_markdown_after_complete_json_prefix(
    tmp_path,
    extension,
    expected_name,
):
    filename = f"workspace-flow.{extension}"
    path = tmp_path / filename
    path.write_text(
        "[1]" + (" " * (4096 - len("[1]"))) + "\n# Heading\n```mermaid\nflowchart TD\n```",
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == expected_name
    assert detected.mismatch is True


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("notes.md", "notes.md"),
        ("workspace-flow.diagram.json", "workspace-flow.diagram.md"),
        ("workspace-flow.diagram", "workspace-flow.md"),
    ],
)
@pytest.mark.parametrize(
    "prefix",
    [
        "[" + (" " * 4095),
        '["' + ("x" * 4094),
    ],
    ids=["array-waiting-value", "unterminated-string"],
)
def test_detect_file_type_rejects_markdown_after_incomplete_json_prefix(
    tmp_path,
    filename,
    expected_name,
    prefix,
):
    path = tmp_path / filename
    path.write_text(
        prefix + "\n# Heading\n```mermaid\nflowchart TD\n```",
        encoding="utf-8",
    )

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == expected_name
    assert detected.mismatch is (filename != "notes.md")


@pytest.mark.parametrize(
    "content",
    [
        "[Documentation](https://example.com)\n\n```python\nprint('ok')\n```",
        "[true](https://example.com)\n\n```python\nprint('ok')\n```",
        "[1] Reference section\n\n```python\nprint('ok')\n```",
    ],
)
def test_detect_file_type_preserves_markdown_brackets_before_code_fence(
    tmp_path,
    content,
):
    filename = "notes.md"
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == filename
    assert detected.mismatch is False


@pytest.mark.parametrize(
    ("extension", "expected_name"),
    [("diagram.json", "workspace-flow.diagram.md"), ("diagram", "workspace-flow.md")],
)
def test_detect_file_type_rejects_markdown_disguised_as_diagram(
    tmp_path,
    extension,
    expected_name,
):
    filename = f"workspace-flow.{extension}"
    path = tmp_path / filename
    path.write_text("# Notes\n```mermaid\nflowchart TD\n```", encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == "md"
    assert detected.mime_type == "text/markdown"
    assert detected.display_name == expected_name
    assert detected.mismatch is True


def test_detect_file_type_preserves_legacy_editable_diagram_identity(tmp_path):
    path = tmp_path / "workspace-flow.diagram"
    path.write_text(
        '{"version":"editable_diagram_v1","elements":[]}',
        encoding="utf-8",
    )

    detected = detect_file_type(
        str(path),
        declared_name="workspace-flow.diagram",
    )

    assert detected.extension == "diagram"
    assert detected.mime_type == "application/json"
    assert detected.display_name == "workspace-flow.diagram"
    assert detected.mismatch is False


@pytest.mark.parametrize("content", ["", " \n\t"])
def test_detect_file_type_preserves_blank_legacy_editable_diagram_identity(
    tmp_path,
    content,
):
    path = tmp_path / "workspace-flow.diagram"
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(
        str(path),
        declared_name="workspace-flow.diagram",
    )

    assert detected.extension == "diagram"
    assert detected.mime_type == "application/json"
    assert detected.display_name == "workspace-flow.diagram"
    assert detected.mismatch is False


@pytest.mark.parametrize(
    ("filename", "content", "expected_mime"),
    [
        ("workspace-flow.mmd", "flowchart TD\nA --> B\n", "text/plain"),
        ("workspace-flow.mermaid", "flowchart LR\nA --> B\n", "text/plain"),
        (
            "workspace-flow.drawio",
            "<mxfile><diagram><mxGraphModel><root /></mxGraphModel></diagram></mxfile>",
            "application/xml",
        ),
    ],
)
def test_detect_file_type_preserves_raw_diagram_identity(
    tmp_path,
    filename,
    content,
    expected_mime,
):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    detected = detect_file_type(str(path), declared_name=filename)

    assert detected.extension == filename.rsplit(".", 1)[1]
    assert detected.mime_type == expected_mime
    assert detected.display_name == filename
    assert detected.mismatch is False
