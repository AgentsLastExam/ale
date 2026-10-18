"""Live acceptance tests read provider credentials from the checkout's .env, as ale does."""

import pytest

from ale.run.secrets import load_env


@pytest.fixture(autouse=True, scope="session")
def checkout_env() -> None:
    load_env()
