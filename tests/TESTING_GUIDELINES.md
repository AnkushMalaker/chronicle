# Robot Framework Testing Guidelines

This file provides specific guidelines for organizing and writing Robot Framework tests in this project.

## Test Organization Principles

### Resource File Organization

Each resource file should have a clear purpose and contain related keywords. Resource files should include documentation explaining what types of functions belong in that file.

#### Resource File Categories

**setup_resources.robot**
- Docker service management (start/stop services)
- Environment validation
- Health checks and service dependency verification
- System preparation keywords
- Any keywords that prepare the testing environment

**session_resources.robot**
- API session creation and management
- Authentication workflows
- Token management (when needed for external tools like curl)
- Session validation and cleanup
- Keywords that handle API authentication and session state

**user_resources.robot**
- User account creation, deletion, and management
- User-related operations and utilities
- User permission validation
- Keywords specific to user account lifecycle

**integration_keywords.robot**
- Core integration workflow keywords
- File processing and upload operations
- System interaction keywords that don't fit in other categories
- Complex multi-step operations that combine multiple services

### Verification vs Setup Separation

**Verification Steps**
- **MUST be written directly in test files, not abstracted into resource keywords**
- Keep verifications close to the test logic for readability and maintainability
- Use descriptive assertion messages that explain what is being verified
- Example: `Should Be Equal As Integers    ${response.status_code}    200    Health check should return 200`
- Verification keywords should only exist in resource files if they perform complex multi-step verification that needs to be reused across multiple test suites

**Setup/Action Keywords**
- Environment setup, service management, and system actions belong in resource files
- These can be reused across multiple tests and suites
- Focus on "what to do" rather than "what to verify"
- Examples: `Get Admin API Session`, `Upload Audio File For Processing`, `Start Docker Services`

**Suite-Level Keywords**
- If a specific set of verifications needs to be repeated multiple times within a single test suite, create keywords at the suite level (in the *** Keywords *** section of the test file)
- These should be specific to that suite's testing needs
- Only create suite-level keywords when the same verification logic is used 3+ times in the same suite

## Code Style Guidelines

### Human Readability
- Tests should be readable by domain experts without deep Robot Framework knowledge
- Use descriptive keyword names that explain the business purpose
- Prefer explicit over implicit - make test intentions clear
- Use meaningful variable names and comments where helpful
- Avoid Robot Framework-specific jargon in test names and documentation

### Test Structure
```robot
*** Test Cases ***
Test Name Should Describe Business Scenario
    [Documentation]    Clear explanation of what this test validates
    [Tags]            relevant    tags

    # Arrange - Setup test data and environment
    ${session}=    Get Admin API Session

    # Act - Perform the operation being tested
    ${result}=    Upload Audio File For Processing    ${session}    ${TEST_FILE}

    # Assert - Verify results directly in test (NOT in resource keywords)
    Should Be True    ${result}[successful] > 0    At least one file should be processed successfully
    Should Contain    ${result}[message]    processing completed    Processing should complete successfully
```

### Resource File Documentation
Each resource file should start with clear documentation:

```robot
*** Settings ***
Documentation    Brief description of this resource file's purpose
...
...              This file contains keywords for [specific purpose].
...              Keywords in this file should handle [what types of operations].
...
...              Examples of keywords that belong here:
...              - Keyword type 1
...              - Keyword type 2
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - Keywords specific to other domains
```

### Authentication Pattern
- Tests should use session-based authentication via `session_resources.robot`
- Avoid passing tokens directly in tests - use sessions instead
- Extract tokens from sessions only when required for external tools (like curl)

Example:
```robot
# Good - Session-based approach
${admin_session}=    Get Admin API Session
${conversations}=    Get User Conversations    ${admin_session}

# Avoid - Direct token handling in tests
${token}=    Get Admin Token
${conversations}=    Get User Conversations    ${token}
```

## File Naming and Structure

### Test Files
- Use descriptive names that indicate the testing scope
- Example: `full_pipeline_test.robot`, `user_management_test.robot`
- Use `_test.robot` suffix for test files

### Resource Files
- Use `_resources.robot` suffix
- Name should indicate the domain: `session_resources.robot`, `user_resources.robot`

### Keywords
- Use descriptive names with clear action words
- Start with action verb when possible: `Get User Conversations`, `Upload Audio File`, `Create Test User`
- Avoid abbreviations unless they're widely understood in the domain
- Use consistent naming patterns across similar keywords

## Error Handling

### Resource Keywords
- Should handle expected error conditions gracefully
- Use appropriate Robot Framework error handling (TRY/EXCEPT blocks)
- Log meaningful error messages for debugging
- Fail fast with clear error messages when setup fails

### Test Assertions
- Write verification steps directly in tests with clear failure messages
- Use descriptive assertion messages that explain what went wrong
- Example: `Should Be Equal    ${status}    active    User should be in active status after creation`
- Include relevant context in failure messages (expected vs actual values)

## Keywords vs Inline Code

### BEFORE Writing Test Code: Check Existing Keywords

**CRITICAL: Always review existing resource files before writing any test code.**

Before implementing ANY test logic:
1. **Open and scan ALL relevant resource files** for existing keywords
2. **Read keyword documentation** to understand what they do
3. **Look for similar patterns** - if your test needs to do something common (like "create conversation", "wait for job", "send audio"), a keyword likely exists
4. **Check the keyword's dependencies** - keywords often call other helper keywords you should also use

**Why this matters:**
- Prevents code duplication and maintenance burden
- Ensures consistent test patterns across the suite
- Leverages battle-tested, optimized implementations
- Reduces test complexity and improves readability

**How to do this:**
```robot
# Bad - Writing inline code without checking
${jobs}=    Get Jobs By Type And Client    open_conversation    ${device}
${count}=    Get Length    ${jobs}
# ... manual logic to wait for new job ...

# Good - Using existing keyword
${jobs}=    Wait Until Keyword Succeeds    30s    2s
...    Wait For New Job To Appear    open_conversation    ${device}    ${baseline_count}
```

**Resource Files to Check (based on your test domain):**
- `websocket_keywords.robot` - WebSocket streaming, audio chunks, conversation creation
- `conversation_keywords.robot` - Conversation CRUD, transcript operations
- `queue_keywords.robot` - Job tracking, waiting for job states, queue monitoring
- `memory_keywords.robot` - Memory operations, search, retrieval
- `audio_keywords.robot` - Audio file handling, processing
- `session_resources.robot` - Authentication, API sessions
- `integration_keywords.robot` - Complex multi-step workflows

### When to Create Keywords
- Reusable operations that are used across multiple tests or suites
- Complex multi-step setup or teardown operations
- Operations that encapsulate business logic or domain concepts
- Operations that interact with external systems (APIs, databases, files)
- **ONLY after confirming no existing keyword does what you need**

### When to Keep Code Inline
- Verification steps (assertions) - these should almost always be inline in tests
- Simple operations that are only used once
- Test-specific logic that doesn't need to be reused
- Variable assignments and simple data manipulation

## Variable and Data Management

### Test Data
- Use meaningful variable names that describe the data's purpose
- Define test data at the appropriate scope (suite variables for shared data, test variables for test-specific data)
- Store complex test data in separate variable files when it becomes large
- Use descriptive names: `${VALID_USER_EMAIL}` instead of `${EMAIL1}`

### Environment Configuration
- Load environment variables through `test_env.py`
- Use consistent variable naming across tests
- Document required environment variables and their purposes

## Service Profiles (real vs stubbed backing services)

Chronicle runs **one** test suite. Tests are never selected by whether a
credential happens to be present.

What varies between runs is a **service profile**: a declaration of which backing
services are real and which are stubbed, defined in
[`tests/profiles.yml`](profiles.yml).

```bash
make test                              # profile: mock (default) -- no credentials
make test PROFILE=deepgram-openai      # real Deepgram STT + real OpenAI LLM
make test PROFILE=deepgram-openai-speaker   # ...plus the real speaker service
make test PROFILE=parakeet-ollama      # real local ASR + local Ollama
```

Every test runs in every profile. A profile that names a real service declares
what it needs (`requires_env`, `requires_service`); the harness verifies that
up-front and fails with the exact remedy, so a missing key is a clear setup error
rather than a pile of confusing test failures.

### Why there is no `requires-api-keys` tag

Gating tests on credentials meant a test skipped on pull requests was a test
nobody ran, and because fork pull requests cannot read repository secrets under
any circumstance, "run it later with a label" never covered the case it was added
for. Tags tied to credentials also rot silently: the suite hid genuinely broken
tests for as long as they stayed gated.

So the credential axis is gone. If a test appears to need a real provider, that
is a statement about **stub fidelity**, not about the test.

### Cassettes: how stubs stay honest

A stub that invents its own output forces a choice between skipping the test and
asserting something meaningless. Instead, stubs replay **cassettes** -- recorded
real provider responses, keyed by the sha256 of the audio that produced them and
committed under [`tests/cassettes/`](cassettes/).

That makes a content assertion identical in both directions:

```robot
# Holds against real Deepgram AND against the stub, because the stub replays
# a real recorded transcript of the same fixture.
Verify Transcription Quality    ${conversation}    ${EXPECTED_TRANSCRIPT_PHRASES}
```

Record or refresh them with real credentials, once:

```bash
make record-cassettes PROFILE=deepgram-openai
```

This is the same rule the rest of the project follows for paid APIs: record once,
commit, never spend again. A normal test run makes no external calls.

### Writing assertions that survive every profile

Assert on **behaviour and content**, not on one provider's formatting.

```robot
# Good - content words any competent engine produces
Should Contain    ${transcript}    glass

# Bad - encodes Deepgram's tokenization; other engines emit "glassblowing",
# so this silently means "only run against Deepgram"
Should Contain    ${transcript}    glass blowing

# Bad - pins an exact snapshot of one provider's segmentation, which drifts
# whenever that provider updates its model
Should Be Equal    ${segment}[end]    10.08
```

If an assertion can only hold for one provider, either widen it to the invariant
you actually care about, or anchor it to a cassette.


## Slow and SDK Test Organization

Chronicle excludes certain tests from default test runs to provide faster feedback and cleaner test execution.

### The `slow` Tag

**Purpose**: Mark tests that require long timeouts (>30s) or infrastructure operations like service restarts.

**Add this tag when tests:**
- Restart backend or other services (stop/start cycles)
- Test connection resilience after service failures
- Require timeouts longer than 30 seconds
- Test infrastructure operations that significantly slow down test execution

**Do NOT add this tag when tests:**
- Complete within normal timeouts (<30s)
- Don't restart or rebuild services
- Are simple endpoint or integration tests

**Example:**
```robot
*** Test Cases ***
Test Job Persistence Through Backend Restart
    [Documentation]    Test that RQ jobs persist when backend service restarts
    [Tags]    queue	slow
    [Timeout]    120s

    ${job_id}=    Reprocess Transcript    ${conversation_id}
    Restart Backend Service    wait_timeout=90s    # Longer timeout for slow test
    ${jobs_after}=    Get job queue
    Should Be True    ${jobs_count_after} >= 0
```

**Running Slow Tests:**
```bash
cd tests

# Default test run (EXCLUDES slow tests)
make test         # Faster feedback, no service restarts

# Run ONLY slow tests
make test-slow    # Explicit slow test execution

# Run ALL tests including slow
make test-all-with-slow-and-sdk
```

### The `sdk` Tag

**Purpose**: Mark tests for unreleased SDK functionality that should be excluded until the SDK is published.

**Add this tag when tests:**
- Test SDK client library features
- Require SDK installation or SDK-specific imports
- Are for SDK features not yet released to users
- Test SDK authentication, upload, or retrieval methods

**Do NOT add this tag when tests:**
- Test backend API endpoints directly (these should always run)
- Test features available through direct HTTP/WebSocket calls
- Are part of the core backend functionality

**Example:**
```robot
*** Test Cases ***
SDK Can Upload Audio File
    [Documentation]    Test SDK audio upload functionality
    [Tags]    audio-upload	sdk

    ${result}=    Run Process    uv    run    python
    ...    ${CURDIR}/../scripts/sdk_test_upload.py
    ...    ${BACKEND_URL}    ${ADMIN_EMAIL}    ${ADMIN_PASSWORD}    ${test_audio}
    Should Be Equal As Integers    ${result.rc}    0
```

**Running SDK Tests:**
```bash
cd tests

# Default test run (EXCLUDES sdk tests)
make test         # SDK not released yet

# Run ONLY SDK tests (when developing SDK)
make test-sdk

# Run ALL tests including SDK
make test-all-with-slow-and-sdk
```

**When to Re-enable SDK Tests:**
Once the SDK is released and published:
1. Remove `--exclude sdk` from default Makefile target (`make test`)
2. Keep the `sdk` tag for organization (allows filtering SDK-specific tests)
3. Update `tests/README.md` to reflect that SDK tests are included

### Benefits of Excluding Slow and SDK Tests

**Faster Default Test Runs:**
- Default `make test` excludes slow tests (service restarts, long timeouts)
- Provides faster feedback during development (saves 2-5 minutes per run)
- Developers can iterate quickly on endpoint and integration tests

**Cleaner Test Reports:**
- SDK tests won't fail in CI when SDK isn't released yet
- No confusing failures for unreleased features
- Clear separation of released vs unreleased functionality

**Explicit Execution When Needed:**
- Run slow tests explicitly when testing infrastructure resilience
- Run SDK tests explicitly when developing SDK features
- Full test suite available via `make test-all-with-slow-and-sdk`

### Tag Combination Examples

```robot
# Good - Slow infrastructure test
[Tags]    queue	slow
[Timeout]    120s

# Good - Unreleased SDK feature
[Tags]    audio-upload	sdk

# Good - Multiple component tags
[Tags]    conversation	memory

# Bad - Don't combine slow and sdk (different purposes)
[Tags]    slow	sdk
```

## Future Additions

As we develop more conventions and encounter new patterns, we will add them to this file:
- Performance testing guidelines
- Data management patterns
- Mock and test double strategies
- Continuous integration considerations
- Test reporting and metrics
- Parallel test execution patterns
- Test data isolation strategies
