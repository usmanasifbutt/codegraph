import pytest
from neo4j.exceptions import AuthError, ServiceUnavailable

from codegraph.config import ConfigError, ConnectError, Settings, connect, load_settings


def test_defaults_and_required_password():
    s = load_settings({"NEO4J_PASSWORD": "pw"})
    assert (s.uri, s.user, s.password, s.connect_timeout) == (
        "bolt://localhost:7687",
        "neo4j",
        "pw",
        60.0,
    )


def test_missing_password_names_variable():
    with pytest.raises(ConfigError, match="NEO4J_PASSWORD"):
        load_settings({})


def test_env_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("NEO4J_PASSWORD=from-dotenv\nNEO4J_USER=dotenv-user\n")
    monkeypatch.setenv("NEO4J_PASSWORD", "from-env")
    # setenv first so monkeypatch restores NEO4J_USER after load_dotenv writes it.
    monkeypatch.setenv("NEO4J_USER", "placeholder")
    monkeypatch.delenv("NEO4J_USER")
    s = load_settings(dotenv_dir=tmp_path)
    assert s.password == "from-env"
    assert s.user == "dotenv-user"


class FakeDriver:
    def __init__(self, failures, exc=ServiceUnavailable):
        self.failures, self.exc, self.calls, self.closed = failures, exc, 0, False

    def verify_connectivity(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc("not yet")

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


SETTINGS = Settings("bolt://x:7687", "neo4j", "pw", connect_timeout=60)


def test_retry_until_database_is_up():
    driver, clock = FakeDriver(failures=6), FakeClock()
    assert connect(SETTINGS, lambda *a, **k: driver, clock.sleep, clock) is driver
    assert driver.calls == 7 and 0 < clock.now < 60


def test_retry_gives_up_after_timeout():
    driver, clock = FakeDriver(failures=10**6), FakeClock()
    with pytest.raises(ConnectError, match="not reachable after 60s"):
        connect(SETTINGS, lambda *a, **k: driver, clock.sleep, clock)
    assert driver.closed and clock.now == pytest.approx(60)


def test_auth_error_not_retried():
    driver, clock = FakeDriver(failures=1, exc=AuthError), FakeClock()
    with pytest.raises(ConnectError, match="credentials"):
        connect(SETTINGS, lambda *a, **k: driver, clock.sleep, clock)
    assert driver.calls == 1


def _server_error(code):
    from neo4j.exceptions import Neo4jError

    return lambda msg: Neo4jError._hydrate_neo4j(code=code, message=msg)


def test_auth_rate_limit_is_clean_auth_failure():
    # Seen when .env's password no longer matches the database (NEO4J_AUTH only applies once).
    driver = FakeDriver(
        failures=1, exc=_server_error("Neo.ClientError.Security.AuthenticationRateLimit")
    )
    clock = FakeClock()
    with pytest.raises(ConnectError, match="AuthenticationRateLimit.*ALTER CURRENT USER"):
        connect(SETTINGS, lambda *a, **k: driver, clock.sleep, clock)
    assert driver.calls == 1 and driver.closed


def test_other_client_error_is_clean_failure():
    driver = FakeDriver(failures=1, exc=_server_error("Neo.ClientError.Request.Invalid"))
    clock = FakeClock()
    with pytest.raises(ConnectError, match="refused"):
        connect(SETTINGS, lambda *a, **k: driver, clock.sleep, clock)
