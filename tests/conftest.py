from pathlib import Path

import pytest


class Secret:
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "Secret(********)"

    def __str___(self):
        return "*******"


def pytest_addoption(parser):
    parser.addoption(
        "--profile",
        action="store",
        default=None,
        help="AWS profile to use for integration tests",
    )


@pytest.fixture(scope="session")
def aws_profile(request):
    return request.config.getoption("--profile")


def pytest_configure(config):
    if hasattr(config.option, "self_contained_html"):
        project_root = Path(__file__).parent.parent
        css_file = project_root / "resources" / "pytest-html.css"
        config.option.css = [str(css_file)]
        if not config.option.self_contained_html:
            config.option.self_contained_html = True
