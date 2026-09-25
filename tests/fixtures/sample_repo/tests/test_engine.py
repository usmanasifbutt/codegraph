from mypkg.core.engine import Service


def make_fixture():
    return Service()


def test_run():
    assert make_fixture().run(2) == 4


class TestService:
    def test_start(self):
        assert True

    def helper(self):
        return 1
