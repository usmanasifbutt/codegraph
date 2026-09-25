# query-ui Specification

## Purpose
The browser interface where a user connects a repository (a local folder or a public Git URL), indexes it, picks an indexed repository, and asks questions by typing or speaking. For each question it shows the answer, the Cypher behind it and the raw results.

## Requirements

### Requirement: Launch
The UI SHALL start with `codegraph ui` on the host (default port 8501, overridable with `--port`) and as the `app` container's default command. On startup it MUST check that Neo4j is reachable. If it is not, it MUST show an error page naming the configured URI and nothing else. If the LLM configuration is invalid, connecting and indexing repositories MUST still work, and the question box MUST be disabled with a message naming the configuration problem.

#### Scenario: Start on host
- **WHEN** a user runs `codegraph ui` with a valid `.env` and a running Neo4j
- **THEN** the UI is served at http://localhost:8501 and shows the repository panel and the question box

#### Scenario: Neo4j down
- **WHEN** the UI starts and Neo4j is unreachable
- **THEN** the page shows an error saying Neo4j at the configured URI is unreachable

#### Scenario: LLM key missing
- **WHEN** the UI starts with a reachable Neo4j and no key for the configured LLM provider
- **THEN** repositories can still be connected and indexed, and the question box is disabled with a message naming the missing variable

### Requirement: Connect a local folder
The UI SHALL let the user connect a local folder by entering its path. When the UI runs on the host, any existing directory is accepted. When `CODEGRAPH_REPOS_ROOT` is set (as it is in the container, to `/repos`), the UI MUST offer the immediate subfolders of that root as choices. It MUST reject any path that, once resolved, falls outside the root. A path that does not exist or is not a directory MUST be rejected with a message, and nothing is indexed.

#### Scenario: Host folder
- **WHEN** the UI runs on the host and the user enters `D:\Projects\langchain_basics`
- **THEN** that folder is accepted for indexing

#### Scenario: Container root
- **WHEN** the UI runs in the container with `/repos` holding folders `ai-lab` and `codegraph`
- **THEN** both are offered as choices, and entering `/etc` or `/repos/../etc` is rejected as outside the repos root

#### Scenario: Missing folder
- **WHEN** the user enters a path that does not exist
- **THEN** the UI shows "folder not found" and nothing is indexed

### Requirement: Connect a public Git URL
The UI SHALL let the user connect a public Git repository by entering an `https://` URL and an optional branch. The repository MUST be shallow-cloned (latest commit only, no submodules) into a folder under the clone workspace `CODEGRAPH_WORKSPACE`, with a timeout of `CODEGRAPH_CLONE_TIMEOUT_SECONDS` (default 180). The UI MUST reject URLs that use another scheme, including `ssh`, `git`, `file` and `http`, and URLs with embedded credentials. Git MUST NOT prompt for credentials, so a private or missing repository fails right away with a message saying that only public repositories are supported. A clone that times out or fails MUST leave no partial folder behind.

#### Scenario: Public GitHub repository
- **WHEN** the user enters `https://github.com/pallets/itsdangerous` and connects it
- **THEN** it is cloned into the workspace and becomes available for indexing

#### Scenario: Private or missing repository
- **WHEN** the user enters an `https` URL that needs authentication or does not exist
- **THEN** the UI shows that the repository could not be cloned and that only public repositories are supported, and no folder is left in the workspace

#### Scenario: Disallowed URL
- **WHEN** the user enters `git@github.com:org/repo.git`, `file:///etc`, or `https://user:token@github.com/org/repo`
- **THEN** the URL is rejected before any clone is attempted

### Requirement: Index from the UI
After a folder or Git URL is connected, the UI SHALL index it with the same pipeline and replacement rules as `codegraph index`:
- **Name and options:** the repository name defaults to the folder name or the URL's last path segment without `.git`, and can be edited. The user can enter exclude globs.
- **Existing name:** the UI MUST ask for confirmation before replacing that repository's graph.
- **Progress:** the UI MUST show progress for each step (cloning, parsing, writing). While indexing runs, the connect, index and question controls MUST be disabled.
- **Success:** the UI shows the same summary as the CLI (counts per node label and relationship type, parse errors with their paths, warnings, elapsed time) and selects the repository for questions.
- **Failure:** the UI shows the error, and the repository's status follows the `index-cli` failure rules.

#### Scenario: Index a new folder
- **WHEN** the user connects `D:\Projects\langchain_basics` and clicks Index
- **THEN** progress is shown, the summary lists the node and relationship counts, and `langchain_basics` becomes the selected repository

#### Scenario: Name already used
- **WHEN** the user indexes a folder under a name that is already indexed
- **THEN** the UI asks for confirmation, and replaces that repository's graph only if the user confirms

#### Scenario: Parse errors shown
- **WHEN** the indexed folder contains a file with a syntax error
- **THEN** the summary lists that file and its error, and the repository is still indexed and selectable

### Requirement: Re-index a repository
The UI SHALL offer re-indexing of an indexed repository that was connected from the UI or from the CLI. A local folder is re-read from its recorded path. A Git repository is re-cloned from its recorded URL and branch, then re-indexed. If the recorded path no longer exists, the UI MUST say so and leave the existing graph unchanged.

#### Scenario: Re-index after changes
- **WHEN** a function was added to a connected local folder and the user clicks Re-index
- **THEN** after indexing, questions about that repository can find the new function

#### Scenario: Git repository updated
- **WHEN** the user re-indexes a repository connected from a Git URL
- **THEN** the latest commit of the recorded branch is cloned and indexed

### Requirement: Repository picker
The UI SHALL list the indexed repositories, meaning `Repo` nodes, with their status, index time and source (local path or Git URL), and let the user pick the one that questions apply to. Repositories whose status is not `complete` MUST be marked as incomplete. If no repository is indexed, the UI MUST show the connect panel and a prompt to connect a first repository, and the question box stays disabled.

#### Scenario: Pick a repository
- **WHEN** two repositories are indexed and the user selects `langchain_basics`
- **THEN** later questions run with `$repo = 'langchain_basics'`

#### Scenario: Nothing indexed
- **WHEN** the database has no `Repo` nodes
- **THEN** the UI prompts the user to connect and index a repository, and the question box is disabled

### Requirement: Asking a question
Once a repository is selected, the UI SHALL accept a typed question, or a spoken one as described by `voice-input`, and SHALL show progress while the answer is being produced. Submitting an empty question MUST do nothing.

#### Scenario: Typed question
- **WHEN** the user types "who imports mypkg.models?" and submits it
- **THEN** a progress indicator is shown until the result appears

### Requirement: Result view
For each question, the UI SHALL show:
- the question, marked as spoken when it came from voice input
- the answer
- the generated Cypher, in a code block with its explanation
- the results as a table, with the row count and a truncation notice when applicable
- the number of repair attempts and the elapsed time

An error result MUST show the error message and the last attempted Cypher instead of a table.

#### Scenario: Successful question
- **WHEN** a question is answered successfully
- **THEN** its answer, Cypher code block and results table are all visible for that question

#### Scenario: Failed question
- **WHEN** generation fails after all repairs
- **THEN** the UI shows the error and the last attempted Cypher, and the rest of the conversation stays usable

### Requirement: Edit and re-run Cypher
The user SHALL be able to edit the Cypher of any result and run the edited version. Edited queries MUST pass the same `cypher-safety` rules, are logged with origin `user-edited`, and produce a new result entry. A new answer is generated from the new rows.

#### Scenario: Edited query
- **WHEN** the user changes `LIMIT 10` to `LIMIT 5` and runs the edited query
- **THEN** a new result entry appears with at most 5 rows and an answer based on them

#### Scenario: Edited write query
- **WHEN** the user edits a query into `MATCH (n) DELETE n` and runs it
- **THEN** the UI shows a "not read-only" error and nothing is changed

### Requirement: Session history
The UI SHALL keep the list of questions and results for the browser session, newest last, and SHALL provide a control to clear it. The history MUST NOT be stored on the server between sessions.

#### Scenario: Clear history
- **WHEN** the user clicks "Clear conversation"
- **THEN** all earlier questions and results are removed from the page
