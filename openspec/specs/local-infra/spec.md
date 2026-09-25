# local-infra Specification

## Purpose
Brings up the local codegraph stack (a Neo4j graph database plus the codegraph application) with a single Podman Compose command, configured entirely from environment variables and with the target repository mounted read-only.

## Requirements

### Requirement: Single-command stack startup
The project SHALL provide a Compose file that starts a Neo4j service and an application service with `podman compose up`, without requiring Docker. Every container image reference MUST be fully qualified with its registry (for example `docker.io/library/neo4j:<tag>`).

#### Scenario: Fresh start
- **WHEN** a developer with Podman installed copies `.env.example` to `.env`, sets a password, and runs `podman compose up -d`
- **THEN** a Neo4j container and an application container are created and Neo4j accepts Bolt connections on host port 7687 and serves its browser on host port 7474

#### Scenario: Image names are registry-qualified
- **WHEN** the Compose file and Containerfile are inspected
- **THEN** every image reference includes an explicit registry host

### Requirement: Neo4j health reporting
The Neo4j service SHALL define a healthcheck that passes only once the database accepts authenticated queries, and the application service SHALL declare a dependency on Neo4j being healthy.

#### Scenario: Health before readiness
- **WHEN** the Neo4j container has started but the database is not yet accepting queries
- **THEN** the service health status is not `healthy`

#### Scenario: Healthy database
- **WHEN** Neo4j accepts an authenticated `RETURN 1` query
- **THEN** the service health status becomes `healthy`

### Requirement: Persistent graph data
Neo4j data SHALL be stored in a named volume so that the indexed graph survives container restarts and re-creation.

#### Scenario: Restart keeps data
- **WHEN** a repository has been indexed and the stack is stopped with `podman compose down` (without removing volumes) and started again
- **THEN** the previously indexed nodes are still present

### Requirement: Environment-based configuration
All credentials and connection settings SHALL come from environment variables loaded from a git-ignored `.env` file. The repository MUST ship a `.env.example` that lists every variable the stack reads, with placeholder values and no real secrets. The Neo4j password MUST NOT appear in the Compose file, the Containerfile, or any committed file.

#### Scenario: Example env is complete
- **WHEN** `.env.example` is copied to `.env` and only the placeholder password is changed
- **THEN** the stack starts and the application can connect to Neo4j

#### Scenario: Secrets are not committed
- **WHEN** the committed files are searched for the configured password value
- **THEN** no match is found and `.env` is listed in `.gitignore`

### Requirement: Read-only repository mount
The application service SHALL mount the host directory named by a configurable environment variable at `/repos` inside the container in read-only mode.

#### Scenario: Repository visible in container
- **WHEN** the repo path variable points to an existing host directory containing Python files and the stack is up
- **THEN** those files are listed under `/repos` inside the application container

#### Scenario: Mount is read-only
- **WHEN** a process in the application container tries to create a file under `/repos`
- **THEN** the write fails with a read-only filesystem or permission error

#### Scenario: Windows host path
- **WHEN** the stack runs under `podman machine` on Windows and the repo path variable is set as documented in the README for Windows hosts
- **THEN** the repository files are visible under `/repos` inside the application container

### Requirement: CLI available in the application container
The application image SHALL contain the installed `codegraph` CLI and its dependencies so that CLI commands can be run with `podman compose run --rm app codegraph <command>`, connecting to Neo4j through the Compose network.

#### Scenario: Run CLI in container
- **WHEN** a developer runs `podman compose run --rm app codegraph --help`
- **THEN** the CLI help is printed and the command exits with code 0
