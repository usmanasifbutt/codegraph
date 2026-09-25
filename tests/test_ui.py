"""UI behaviour through streamlit's AppTest, with FakeServices injected (no DB, no LLM)."""

import io
import wave
from pathlib import Path

import pytest
from fakes import FakeChatValue, FakeExecutor, FakeQueryLLM, FakeServices, make_summary, q
from streamlit.testing.v1 import AppTest

from codegraph.repos import RepoError
from codegraph.speech import AudioError, NoSpeechError
from codegraph.store.readonly import QueryRejected, Rows
from codegraph.ui import services as services_module

APP = str(Path(__file__).parent.parent / "src" / "codegraph" / "ui" / "app.py")
REPOS = [
    {"name": "alpha", "status": "complete", "root": "/r/alpha", "source": "local"},
    {"name": "beta", "status": "indexing", "root": "/r/beta", "source": "local"},
]
GOOD = "MATCH (c:Class {repo: $repo}) RETURN c.name AS name"


@pytest.fixture
def make_app():
    def make(fake: FakeServices) -> AppTest:
        services_module.OVERRIDE = fake
        return AppTest.from_file(APP, default_timeout=30).run()

    yield make
    services_module.OVERRIDE = None


def texts(elements):
    return " ".join(str(e.value) for e in elements)


def wav(seconds=1.0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


# -- launch ---------------------------------------------------------------------------
def test_neo4j_down(make_app):
    fake = FakeServices(neo4j_error="Neo4j at bolt://localhost:7687 not reachable after 10s")
    at = make_app(fake)
    assert "unreachable" in texts(at.error) and "bolt://localhost:7687" in texts(at.error)
    assert fake.chat_input_calls == [] and not at.exception


def test_start_on_host_shows_panel_and_question_box(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    assert not at.exception
    assert at.button(key="index_local") and at.button(key="index_git")
    assert fake.chat_input_calls[-1]["disabled"] is False


def test_llm_key_missing(make_app, tmp_path):
    fake = FakeServices(repos=REPOS, llm_error="OPENAI_API_KEY is not set")
    at = make_app(fake)
    assert "OPENAI_API_KEY" in texts(at.warning)
    assert fake.chat_input_calls[-1]["disabled"] is True
    at.text_input(key="local_path").input(str(tmp_path)).run()
    at.button(key="index_local").click().run()
    assert fake.index_calls and fake.index_calls[0][0] == "local"  # indexing still works


# -- connect & index --------------------------------------------------------------------
def test_host_folder_indexed_and_selected(make_app, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.text_input(key="local_path").input(str(folder)).run()
    at.text_area(key="local_ex").input("migrations/**\n\nbuild/**").run()
    at.button(key="index_local").click().run()
    assert not at.exception
    kind, name, kw = fake.index_calls[0]
    assert (kind, name) == ("local", "proj") and kw["excludes"] == ["migrations/**", "build/**"]
    assert "Indexed 'proj'" in texts(at.success)
    assert at.session_state["repo"] == "proj"


def test_container_root_choices_and_rejection(make_app, tmp_path):
    root = tmp_path / "repos"
    (root / "ai-lab").mkdir(parents=True)
    (root / "codegraph").mkdir()
    (tmp_path / "etc").mkdir()
    fake = FakeServices(repos=REPOS, repos_root=root)
    at = make_app(fake)
    options = at.selectbox(key="local_pick").options
    assert any(o.endswith("ai-lab") for o in options) and any(
        o.endswith("codegraph") for o in options
    )
    at.text_input(key="local_path").input(str(root / ".." / "etc")).run()
    assert "outside the repos root" in texts(at.error)
    assert at.button(key="index_local").disabled


def test_missing_folder(make_app, tmp_path):
    at = make_app(FakeServices(repos=REPOS))
    at.text_input(key="local_path").input(str(tmp_path / "nope")).run()
    assert "Folder not found" in texts(at.error) and at.button(key="index_local").disabled


@pytest.mark.parametrize(
    "url", ["git@github.com:a/b.git", "file:///etc", "https://u:t@github.com/a/b"]
)
def test_disallowed_url(make_app, url):
    at = make_app(FakeServices(repos=REPOS))
    at.text_input(key="git_url").input(url).run()
    assert at.error and at.button(key="index_git").disabled


def test_git_url_indexed_with_default_name(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.text_input(key="git_url").input("https://github.com/pallets/itsdangerous.git").run()
    at.text_input(key="git_branch").input("main").run()
    at.button(key="index_git").click().run()
    kind, name, kw = fake.index_calls[0]
    assert (kind, name, kw["branch"]) == ("git", "itsdangerous", "main")


def test_name_already_used_needs_confirmation(make_app, tmp_path):
    folder = tmp_path / "alpha"
    folder.mkdir()
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.text_input(key="local_path").input(str(folder)).run()
    assert at.button(key="index_local").disabled
    at.checkbox(key="local_replace").check().run()
    at.button(key="index_local").click().run()
    assert fake.index_calls[0][1] == "alpha"


def test_parse_errors_shown(make_app, tmp_path):
    fake = FakeServices(repos=REPOS)
    fake.index_outcome = make_summary(
        "proj", parse_errors=[{"path": "pkg/broken.py", "error": "SyntaxError at line 3"}]
    )
    at = make_app(fake)
    at.text_input(key="local_path").input(str(tmp_path)).run()
    at.button(key="index_local").click().run()
    assert "Parse errors (1)" in [e.label for e in at.expander]
    assert "pkg/broken.py" in texts(at.markdown)


def test_failed_clone_shows_error(make_app):
    fake = FakeServices(repos=REPOS)
    fake.index_outcome = RepoError("Could not clone x: repository not found or private.")
    at = make_app(fake)
    at.text_input(key="git_url").input("https://github.com/a/private").run()
    at.button(key="index_git").click().run()
    assert "repository not found or private" in texts(at.error)


def test_controls_disabled_while_indexing(make_app, tmp_path):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.text_input(key="local_path").input(str(tmp_path)).run()
    fake.chat_input_calls.clear()
    at.button(key="index_local").click().run()
    # the run that executes the job renders the question box disabled first
    assert fake.index_calls and any(c["disabled"] for c in fake.chat_input_calls)


def test_reindex_selected_repo(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.button(key="reindex").click().run()
    assert fake.index_calls[0][:2] == ("reindex", "alpha")


# -- picker ---------------------------------------------------------------------------
def test_nothing_indexed(make_app):
    fake = FakeServices(repos=[])
    at = make_app(fake)
    assert "Connect and index a repository" in texts(at.info)
    assert fake.chat_input_calls[-1]["disabled"] is True


def test_pick_a_repository(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    at.selectbox(key="repo").select("beta").run()
    fake.next_input = "which classes?"
    at.run()
    assert fake.executor.calls[-1][1] == {"repo": "beta"}
    assert "incomplete" in at.selectbox(key="repo").format_func("beta")


# -- questions ------------------------------------------------------------------------
def ask(at, fake, question):
    fake.next_input = question
    return at.run()


def test_typed_question_result_view_and_no_rerun_calls(make_app):
    llm = FakeQueryLLM([q(GOOD, "Lists classes.")], answer="There are two classes.")
    ex = FakeExecutor({GOOD: Rows(["name"], [{"name": "A"}, {"name": "B"}], False)})
    fake = FakeServices(repos=REPOS, llm=llm, executor=ex)
    at = make_app(fake)
    ask(at, fake, "which classes?")
    assert not at.exception
    assert "which classes?" in texts(at.markdown) and "There are two classes." in texts(at.markdown)
    assert GOOD in [c.value for c in at.code]
    assert len(at.dataframe[-1].value) == 2
    assert "2 rows" in texts(at.caption)
    calls = len(llm.generate_calls)
    at.run()  # plain rerun: results are stored, nothing is recomputed
    assert len(llm.generate_calls) == calls and len(at.session_state["history"]) == 1


def test_failed_question(make_app):
    delete = "MATCH (n {repo: $repo}) DELETE n"
    ex = FakeExecutor({delete: QueryRejected("not read-only (Neo4j classifies it as write)")})
    fake = FakeServices(repos=REPOS, llm=FakeQueryLLM([q(delete)]), executor=ex)
    at = make_app(fake)
    ask(at, fake, "delete it all")
    assert "not read-only" in texts(at.error) and delete in [c.value for c in at.code]
    ask(at, fake, "and again")  # conversation still usable
    assert len(at.session_state["history"]) == 2


def test_edited_query(make_app):
    fake = FakeServices(repos=REPOS, llm=FakeQueryLLM([q(GOOD + " LIMIT 10")]))
    at = make_app(fake)
    ask(at, fake, "classes?")
    at.text_area(key="edit_0").input(GOOD + " LIMIT 5").run()
    at.button(key="run_0").click().run()
    assert not at.exception
    assert fake.executor.calls[-1][0] == GOOD + " LIMIT 5"
    assert fake.executor.calls[-1][2] == "user-edited"
    assert len(at.session_state["history"]) == 2


def test_edited_write_query(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    ask(at, fake, "classes?")
    at.text_area(key="edit_0").input("MATCH (n) DELETE n").run()
    at.button(key="run_0").click().run()
    assert "rejected" in texts(at.error)


def test_clear_history(make_app):
    fake = FakeServices(repos=REPOS)
    at = make_app(fake)
    ask(at, fake, "classes?")
    next(b for b in at.button if b.label == "Clear conversation").click().run()
    assert at.session_state["history"] == []


# -- voice ----------------------------------------------------------------------------
def test_spoken_question(make_app):
    fake = FakeServices(repos=REPOS, transcript="which modules import requests")
    at = make_app(fake)
    assert fake.chat_input_calls[-1]["accept_audio"] is True
    ask(at, fake, FakeChatValue(audio=wav()))
    assert fake.transcribed and fake.llm.generate_calls[-1][0] == "which modules import requests"
    assert "(spoken)" in texts(at.markdown)
    assert at.session_state["history"][0]["spoken"] is True


@pytest.mark.parametrize(
    "exc, message",
    [
        (NoSpeechError(), "No speech detected"),
        (AudioError("Transcription failed: ValueError: bad"), "Transcription failed"),
        (AudioError("Recordings are limited to 60 seconds."), "60 seconds"),
    ],
)
def test_voice_failures_submit_nothing(make_app, exc, message):
    fake = FakeServices(repos=REPOS, transcript=exc)
    at = make_app(fake)
    ask(at, fake, FakeChatValue(audio=wav()))
    assert message in texts(at.error)
    assert fake.llm.generate_calls == [] and at.session_state["history"] == []


def test_model_download_fails_disables_mic(make_app):
    fake = FakeServices(repos=REPOS, voice_error="Voice input is unavailable: no network")
    at = make_app(fake)
    assert "Voice input is unavailable" in texts(at.warning)
    assert fake.chat_input_calls[-1]["accept_audio"] is False
    assert fake.chat_input_calls[-1]["disabled"] is False  # typing still works
