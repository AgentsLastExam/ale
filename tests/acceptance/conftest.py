"""Live acceptance tests read provider credentials from the checkout's .env, as ale does."""

from ale.run.secrets import load_env

# Loaded on import, before the test modules read their knobs into module constants.
load_env()
