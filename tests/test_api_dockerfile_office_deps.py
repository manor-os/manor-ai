from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_api_image_installs_libreoffice_components_for_thumbnail_types():
    documents_source = (ROOT / "apps/api/routers/documents.py").read_text()
    dockerfile = (ROOT / "docker/Dockerfile.api").read_text()

    required_packages = {
        ".pptx": "libreoffice-impress",
        ".docx": "libreoffice-writer",
        ".xlsx": "libreoffice-calc",
        ".pdf": "poppler-utils",
    }

    missing = [
        package
        for ext, package in required_packages.items()
        if ext in documents_source and package not in dockerfile
    ]

    assert missing == []
