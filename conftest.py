"""Pytest configuration for PyAutoRoute."""


def pytest_addoption(parser):
    parser.addoption(
        "--slow",
        action="store_true",
        default=False,
        help="also run slow tests (e.g. routing the large boards in TestProjects)",
    )
