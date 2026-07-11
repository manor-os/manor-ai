from __future__ import annotations

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
