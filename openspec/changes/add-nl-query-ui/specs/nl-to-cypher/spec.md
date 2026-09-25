## Purpose

Turns a plain-language question about an indexed repository into a read-only Cypher query, runs it, and returns the rows with a concise answer grounded in them. The UI uses it now, and the M4 benchmark will use it as the text-to-Cypher baseline.

## ADDED Requirements

### Requirement: LLM configuration
The system SHALL read the LLM settings from environment variables, loading `.env` in the same way as the CLI:
- `LLM_PROVIDER`: `openai` (default) or `openrouter`
- `LLM_MODEL`: the model name
- `OPENAI_API_KEY`, or `OPENROUTER_API_KEY` when the provider is `openrouter`
- `NLQ_MAX_ROWS` (default 200), `NLQ_TIMEOUT_SECONDS` (default 15) and `NLQ_MAX_REPAIRS` (default 2)

A missing key for the selected provider or an unknown provider MUST be reported as a configuration error that names the variable. API keys MUST NOT appear in logs, UI output or error messages.

#### Scenario: OpenRouter selected
- **WHEN** `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY` is set and `LLM_MODEL` names an OpenRouter model
- **THEN** questions are answered by that model through OpenRouter

#### Scenario: Missing key
- **WHEN** `LLM_PROVIDER=openai` and `OPENAI_API_KEY` is empty
- **THEN** asking a question fails with a configuration error naming `OPENAI_API_KEY`, and no request is sent to any provider

### Requirement: Schema-grounded query generation
Given a question and a selected repository name, the system SHALL ask the LLM for exactly one Cypher query and a one-sentence explanation of it. The prompt MUST describe the graph's labels, properties, relationship types and property meanings as defined by the `graph-schema` capability. The query MUST read the repository name from the `$repo` parameter rather than embedding it as a literal. The system MUST NOT send source code or database rows to the LLM at this step.

#### Scenario: Structural question
- **WHEN** the user asks "which classes inherit from Base?" for the indexed sample repository
- **THEN** the generated query matches `INHERITS` relationships to a class named `Base`, filters by `$repo`, and running it returns the subclasses defined in that repository

#### Scenario: Repo passed as parameter
- **WHEN** any query is generated
- **THEN** it references `$repo`, and the repository name is supplied as a query parameter at execution time

### Requirement: Validation and repair
Before execution, every generated query SHALL be validated by the `cypher-safety` rules and checked for a `$repo` reference. If validation fails, or execution raises a Cypher syntax or semantic error, the system MUST send the query and the error message back to the LLM and ask for a corrected query, at most `NLQ_MAX_REPAIRS` times. If no valid query results, the system MUST return an error result that includes the last attempted query and the reason, and MUST NOT execute any query that failed validation.

#### Scenario: Syntax error repaired
- **WHEN** the first generated query has a Cypher syntax error and the LLM's corrected query is valid
- **THEN** the corrected query runs, its rows are returned, and the result records that one repair attempt was made

#### Scenario: Write query never executed
- **WHEN** the LLM keeps returning a query containing `DELETE` on every attempt
- **THEN** no attempt is executed, and the result is an error that shows the last query and says it is not read-only

### Requirement: Grounded answer
After a successful execution, the system SHALL ask the LLM for a concise plain-language answer to the question, using only the question, the executed query and the returned rows (at most `NLQ_MAX_ROWS`). The answer MUST NOT claim facts that the rows do not contain. When rows include file paths and line numbers, the answer MUST cite them as `path:line`. When there are no rows, the answer MUST say that nothing matched instead of guessing. When rows were truncated at the limit, the answer MUST say so.

#### Scenario: Empty result
- **WHEN** the executed query returns zero rows
- **THEN** the answer states that no matching results were found in the repository

#### Scenario: Citations
- **WHEN** the rows include `source_file` and `line` values for import edges
- **THEN** the answer cites those locations in `path:line` form

#### Scenario: Truncated rows
- **WHEN** a query would return more rows than `NLQ_MAX_ROWS`
- **THEN** only `NLQ_MAX_ROWS` rows are returned and the answer notes that the results were truncated

### Requirement: Question result
Each question SHALL produce a result that contains the question, the repository, the final Cypher and its explanation, the column names, the rows, whether rows were truncated, the answer or an error message, the number of repair attempts, and the elapsed time of the generation, execution and answer steps.

#### Scenario: Result fields
- **WHEN** a question is answered successfully
- **THEN** the result exposes all of the fields above with non-empty `cypher`, `columns` and `answer`

### Requirement: Off-topic questions
Questions that cannot be answered from the code graph SHALL produce a polite answer saying that the question is outside what the graph contains. No query is executed in that case.

#### Scenario: Unrelated question
- **WHEN** the user asks "what's the weather in Lahore?"
- **THEN** no Cypher is executed and the answer says the question cannot be answered from the code graph
