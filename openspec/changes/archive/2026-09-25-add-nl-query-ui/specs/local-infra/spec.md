## ADDED Requirements

### Requirement: Web UI served by the application service
By default, the application service SHALL run the codegraph web UI on container port 8501. It MUST be published to the host on the loopback interface only (`127.0.0.1:8501`), because the UI has no authentication. Running CLI commands with `podman compose run --rm app codegraph ...` or `podman compose exec app codegraph ...` MUST keep working.

#### Scenario: UI after compose up
- **WHEN** a developer runs `podman compose up -d` with a valid `.env`
- **THEN** http://localhost:8501 serves the codegraph UI once Neo4j is healthy

#### Scenario: Not exposed on the network
- **WHEN** the published ports of the application container are inspected
- **THEN** port 8501 is bound to `127.0.0.1` only

#### Scenario: CLI still available
- **WHEN** the stack is up and a developer runs `podman compose exec app codegraph --help`
- **THEN** the CLI help is printed and the command exits with code 0

### Requirement: Repos root for UI connections
The application service SHALL set `CODEGRAPH_REPOS_ROOT=/repos` so that the UI offers folders under the read-only repo mount. The README MUST recommend pointing `REPO_PATH` at a parent folder, for example `D:/Projects`, so that several projects can be connected from the UI without editing compose.

#### Scenario: Parent folder mounted
- **WHEN** `REPO_PATH` points to a folder that contains `langchain_basics` and `codegraph`
- **THEN** both appear as connectable folders in the UI running in the container

### Requirement: Git and persistent caches in the application image
The application image SHALL include the `git` command. The application service SHALL mount named volumes for the clone workspace (`CODEGRAPH_WORKSPACE`) and for the Whisper model cache, so that cloned repositories and downloaded models survive container re-creation and image rebuilds.

#### Scenario: Git available
- **WHEN** a developer runs `podman compose exec app git --version`
- **THEN** a git version is printed and the command exits with code 0

#### Scenario: Model survives rebuild
- **WHEN** a voice question has downloaded the Whisper model and the app image is rebuilt and the container recreated
- **THEN** the next voice question does not download the model again

#### Scenario: Clones survive restart
- **WHEN** a Git repository was cloned from the UI and the stack is restarted
- **THEN** its clone is still present in the workspace, and re-indexing it works
