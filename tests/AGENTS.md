# Test agent guide

Read [`README.md`](README.md) before adding tests.

- Unit tests are in-process and carry `pytest.mark.unit` through their package setup.
- Conformance tests exercise a public implementation contract.
- Integration tests provision real infrastructure and use the matching `needs_*` marker.
- Acceptance tests qualify live provider/model/hardware behavior; never make them part of
  the unmarked fast suite.
- Prefer an existing fixture in `tests/conftest.py`, `tests/integration/conftest.py` or
  `tests/support.py` over another local fake.
- Test the smallest public behavior that detects the regression. Do not mirror private
  implementation structure with one test file per helper.
- Keep fixture credentials fake and never print checkout `.env` or `.ale/auth` content.

Run the narrow test while editing, then `just test` and the affected integration marker.
