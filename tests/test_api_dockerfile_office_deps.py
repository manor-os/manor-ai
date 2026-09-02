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


def test_api_image_installs_same_office_fonts_used_by_generated_artifacts():
    dockerfile = (ROOT / "docker/Dockerfile.api").read_text()

    required_fonts = {
        "fonts-crosextra-caladea",
        "fonts-crosextra-carlito",
        "fonts-lato",
        "fonts-liberation",
        "fonts-noto-core",
        "fonts-noto-cjk",
        "fonts-roboto",
    }

    assert {package for package in required_fonts if package not in dockerfile} == set()


def test_api_image_maps_aptos_to_metric_compatible_office_font():
    dockerfile = (ROOT / "docker/Dockerfile.api").read_text()
    fontconfig = (ROOT / "docker/fontconfig/70-manor-office-fonts.conf").read_text()

    assert "70-manor-office-fonts.conf" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert "Aptos" in fontconfig
    assert "Carlito" in fontconfig
