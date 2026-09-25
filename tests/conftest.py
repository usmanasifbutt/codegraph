from pathlib import Path

import pytest

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="session")
def sample_repo() -> Path:
    return FIXTURE_REPO


PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def neo4j_driver():
    """Driver for the compose Neo4j (NEO4J_* from env or the project .env); skip if absent."""
    import dataclasses

    from codegraph.config import ConfigError, ConnectError, connect, load_settings
    from codegraph.store.schema import ensure_schema

    try:
        settings = dataclasses.replace(load_settings(dotenv_dir=PROJECT_ROOT), connect_timeout=3)
        driver = connect(settings)
    except (ConfigError, ConnectError) as exc:
        pytest.skip(f"Neo4j not available: {exc}")
    ensure_schema(driver)
    yield driver
    driver.close()


@pytest.fixture
def repo_names(neo4j_driver):
    """Unique repo names for one test; their data is removed afterwards."""
    import uuid

    created: list[str] = []

    def make(prefix: str = "cgtest") -> str:
        name = f"{prefix}-{uuid.uuid4().hex[:8]}"
        created.append(name)
        return name

    yield make
    with neo4j_driver.session() as session:
        for name in created:
            session.run("MATCH (n {repo: $r}) DETACH DELETE n", r=name).consume()
