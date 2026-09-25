## MODIFIED Requirements

### Requirement: Grounded answer
After a successful execution, the system SHALL ask the LLM for a concise plain-language answer to the question, using only the question, the executed query and the returned rows (at most `NLQ_MAX_ROWS`). The answer MUST NOT claim facts that the rows do not contain. When rows include file paths and line numbers, the answer MUST cite them as `path:line`. When there are no rows, the answer MUST say that nothing matched instead of guessing. When rows were truncated at the limit, the answer MUST say so.

The answer MUST also be complete with respect to the rows' **item column**. That is the first returned column whose values are not file paths or line numbers (columns named `file`, `path`, `source_file`, `line` or `start_line`). A value counts as mentioned when the answer contains the value itself or its last dotted segment.

The system decides whether the answer is **listing** the rows or **selecting** from them. It is listing when it mentions at least 60% of the distinct text values in the item column. In that case, the system SHALL append the values that are not mentioned, after the answer is written and without calling the LLM, as a line `Also in the results: …`. That line lists at most 20 values and then says how many more there are, pointing to the results table. When the answer mentions fewer than 60%, it is a deliberate selection (for example, picking the LLM client from all third-party packages), and the system MUST NOT append anything.

#### Scenario: Empty result
- **WHEN** the executed query returns zero rows
- **THEN** the answer states that no matching results were found in the repository

#### Scenario: Citations
- **WHEN** the rows include `source_file` and `line` values for import edges
- **THEN** the answer cites those locations in `path:line` form

#### Scenario: Truncated rows
- **WHEN** a query would return more rows than `NLQ_MAX_ROWS` and the truncation repair does not produce an untruncated result
- **THEN** only `NLQ_MAX_ROWS` rows are returned and the answer notes that the results were truncated

#### Scenario: Omitted item appended
- **WHEN** the rows list 10 async functions and the LLM's answer names only 9 of them
- **THEN** the final answer ends with `Also in the results:` followed by the missing function, and the other 9 are not repeated

#### Scenario: Short names count as mentioned
- **WHEN** the item column holds `app.api.health.health` and the answer mentions `health`
- **THEN** that value is treated as mentioned and is not appended

#### Scenario: Long omission lists are capped
- **WHEN** the rows hold 200 distinct item values, the answer mentions 165 of them, and 35 are omitted
- **THEN** 20 are appended by name, followed by a note that 15 more are in the results table

#### Scenario: Deliberate selection is not padded
- **WHEN** the question is "which library do we use to connect to the LLM?", the rows list 17 third-party packages, and the answer names 3 of them
- **THEN** nothing is appended, because the answer mentions fewer than 60% of the item values

#### Scenario: Nothing to append
- **WHEN** the answer already mentions every item value, or the rows have no text item column
- **THEN** the answer is left unchanged

## ADDED Requirements

### Requirement: Truncated result repair
When an executed query's results are truncated at `NLQ_MAX_ROWS`, the system SHALL make one additional generation attempt before writing the answer. It sends the query and a note that the results were truncated, and asks for a query that aggregates or narrows them (for example, `DISTINCT` items with counts). That attempt MUST go through the same validation and read-only execution as any generated query, and MUST be logged with origin `repaired`. If the new query succeeds and is not truncated, its rows are used. Otherwise the original truncated rows are used. The attempt counts toward the result's repair count. This attempt is in addition to the `NLQ_MAX_REPAIRS` budget for invalid queries, and it happens at most once per question.

#### Scenario: Aggregation fixes truncation
- **WHEN** the first query for "which third-party packages are imported?" returns one row per import statement and is truncated at 200 rows, and the repaired query returns 15 distinct packages
- **THEN** the answer is written from the 15 package rows, the result is not marked truncated, and its repair count is 1

#### Scenario: Repair still truncated
- **WHEN** the repaired query is also truncated
- **THEN** the answer is written from the original truncated rows and states that the results were truncated

#### Scenario: Repair rejected
- **WHEN** the repaired query fails validation (for example, it is not read-only)
- **THEN** it is not executed, the original truncated rows are used, and the rejection is audited

#### Scenario: No repair when not truncated
- **WHEN** a query returns fewer rows than `NLQ_MAX_ROWS`
- **THEN** no additional generation call is made

### Requirement: Distinct list queries
For questions that ask which items exist, such as packages, modules, classes, functions or files, the system SHALL instruct the LLM to generate queries that return one row per distinct item. Where locations or occurrence counts are useful, they are aggregated per item (for example, `count(*)` and `collect(...)[..5]` of locations), not returned as one row per occurrence. For dependency questions, items are top-level package names.

#### Scenario: Packages are grouped
- **WHEN** the user asks "Which third-party packages does this repo import?" on a repository with hundreds of import statements
- **THEN** the generated query returns at most one row per top-level package, and every third-party package appears in the answer

#### Scenario: Prompt examples are valid
- **WHEN** the few-shot examples for list questions are checked against the graph
- **THEN** each is accepted by the read-only gate and returns one row per distinct item on the fixture repository
