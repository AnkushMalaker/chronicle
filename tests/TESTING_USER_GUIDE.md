# Robot Framework Testing User Guide

A beginner-friendly guide to setting up VSCode for Robot Framework testing, running tests, and creating new tests for the Chronicle project.

## Table of Contents
- [VSCode Setup](#vscode-setup)
- [Running Tests](#running-tests)
- [Understanding the Test Structure](#understanding-the-test-structure)
- [Creating Your First Test](#creating-your-first-test)
- [Working with Keywords](#working-with-keywords)
- [Working with API Sessions](#working-with-api-sessions)
- [Common Patterns and Examples](#common-patterns-and-examples)
- [Troubleshooting](#troubleshooting)

---

## VSCode Setup

### 1. Install Required Extensions

Open VSCode and install these extensions:

1. **Robot Framework Language Server** (by Robocorp)
   - Extension ID: `robocorp.robotframework-lsp`
   - Provides syntax highlighting, code completion, and debugging

2. **Robot Framework Intellisense** (by TomiSoft)
   - Extension ID: `tomisoft.robotframework-intellisense`
   - Additional code completion support

### 2. Install Python Dependencies

From the project root directory:

```bash
# Install Robot Framework and required libraries
pip install robotframework
pip install robotframework-requests
pip install python-dotenv
```

Or if using a virtual environment:

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install robotframework robotframework-requests python-dotenv
```

### 3. Configure VSCode Settings

Open VSCode settings (Cmd+, on Mac, Ctrl+, on Windows) and add:

```json
{
  "robot.language-server.python": "/usr/bin/python3",
  "robot.completions.section_headers.form": "*** {name} ***",
  "files.associations": {
    "*.robot": "robotframework"
  },
  "robot.editor.4spacesTab": true
}
```

### 4. Workspace Setup

Create or update `.vscode/settings.json` in your project root:

```json
{
  "robot.pythonpath": [
    "${workspaceFolder}/tests",
    "${workspaceFolder}/tests/setup",
    "${workspaceFolder}/tests/resources"
  ],
  "robot.variables": {
    "API_URL": "http://localhost:8001"
  }
}
```

---

## Running Tests

### Quick Start

The easiest way to run tests is using the test runner script:

```bash
# From the tests/ directory
cd tests/
./run-robot-tests.sh
```

This script will:
1. Check for required API keys
2. Start all required services (MongoDB, Redis, FalkorDB, Backend)
3. Run all Robot Framework tests
4. Generate test reports
5. Clean up (by default)

### Running Individual Test Files

```bash
# Run a single test file
robot tests/endpoints/auth_tests.robot

# Run with custom output directory
robot --outputdir results/ tests/endpoints/auth_tests.robot

# Run with verbose logging
robot --loglevel DEBUG tests/endpoints/auth_tests.robot
```

### Running Tests by Tags

```bash
# Run all permission-related tests
robot --include permissions tests/

# Run conversation and memory tests
robot --include conversationORmemory tests/

# Run everything except e2e tests
robot --exclude e2e tests/

# Run multiple specific tags
robot --include permissions --include conversation tests/
```

See [@tests/tags.md](tags.md) for the complete list of approved tags.

### Running from VSCode

1. Open a `.robot` file
2. Click the "Run Test" or "Debug Test" button that appears above each test case
3. View results in the Terminal panel

### Development Mode (Keep Containers Running)

For faster iteration during development:

```bash
# First run - starts containers
robot tests/endpoints/auth_tests.robot

# Subsequent runs - reuses containers (much faster!)
robot tests/endpoints/conversation_tests.robot
robot tests/endpoints/memory_tests.robot

# When done, manually stop containers
docker-compose -f backend/docker-compose-test.yml down -v
```

---

## Understanding the Test Structure

### Project Organization

```
tests/
├── endpoints/              # Test files organized by API domain
│   ├── auth_tests.robot
│   ├── conversation_tests.robot
│   ├── memory_tests.robot
│   └── ...
├── resources/              # Reusable keywords
│   ├── session_keywords.robot
│   ├── user_keywords.robot
│   ├── conversation_keywords.robot
│   └── ...
├── setup/                  # Test environment setup
│   ├── setup_keywords.robot
│   ├── teardown_keywords.robot
│   ├── test_env.py         # Environment variables
│   └── .env.test           # Test configuration
└── test_assets/            # Test files (audio, etc.)
```

### Test File Anatomy

A Robot Framework test file has four main sections:

```robot
*** Settings ***
# Imports and configuration
Documentation    Brief description of what this test suite does
Library          RequestsLibrary
Resource         ../resources/session_keywords.robot
Variables        ../setup/test_env.py

Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Variables ***
# Test-specific variables (optional)
${CUSTOM_TIMEOUT}    30

*** Test Cases ***
# Your actual tests go here
Test Name Should Describe What Is Being Tested
    [Documentation]    Detailed explanation of this specific test
    [Tags]    permissions
    # Test steps here

*** Keywords ***
# Suite-specific helper keywords (optional)
Helper Keyword For This Suite Only
    [Arguments]    ${param}
    # Keyword implementation
```

---

## Creating Your First Test

### Step 1: Choose the Right Test File

Determine which domain your test belongs to:
- **auth_tests.robot** - User authentication, login, permissions
- **conversation_tests.robot** - Conversation management, transcripts
- **memory_tests.robot** - Memory storage, search, retrieval
- **chat_tests.robot** - Chat sessions and messages
- **health_tests.robot** - Health checks, system status

Or create a new file if testing a new domain.

### Step 2: Write a Simple Test

Open the appropriate test file and add your test to the `*** Test Cases ***` section:

```robot
*** Test Cases ***

Get User Profile Test
    [Documentation]    Test that authenticated user can retrieve their profile
    [Tags]    permissions

    # Arrange - Get an authenticated session
    Create API Session    api

    # Act - Make the API request
    ${response}=    GET On Session    api    /users/me

    # Assert - Verify the response
    Should Be Equal As Integers    ${response.status_code}    200
    ${user}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${user}    email
    Dictionary Should Contain Key    ${user}    id
    Should Be Equal    ${user}[email]    ${ADMIN_EMAIL}
```

### Step 3: Run Your Test

```bash
# Run just your new test
robot --test "Get User Profile Test" tests/endpoints/auth_tests.robot

# Or run the entire file
robot tests/endpoints/auth_tests.robot
```

### Step 4: Review Results

Robot Framework generates three files:
- **log.html** - Detailed execution log with expandable steps
- **report.html** - High-level test report
- **output.xml** - Machine-readable results

Open `log.html` in your browser to see detailed results.

---

## Working with Keywords

### What Are Keywords?

Keywords are reusable functions in Robot Framework. They can be:
1. **Built-in keywords** - Provided by Robot Framework (e.g., `Should Be Equal`)
2. **Library keywords** - From imported libraries (e.g., `GET On Session` from RequestsLibrary)
3. **Resource keywords** - Custom keywords from resource files (e.g., `Create API Session`)

### Finding Existing Keywords

**CRITICAL: Always check existing resource files before writing test code!**

Before writing ANY test logic, scan these resource files:

```robot
# Session management
tests/resources/session_keywords.robot

# User operations
tests/resources/user_keywords.robot

# Conversations
tests/resources/conversation_keywords.robot

# Memories
tests/resources/memory_keywords.robot

# Chat
tests/resources/chat_keywords.robot

# Audio processing
tests/resources/audio_keywords.robot

# WebSocket streaming
tests/resources/websocket_keywords.robot

# Queue monitoring
tests/resources/queue_keywords.robot
```

### Using Keywords in Tests

```robot
*** Test Cases ***

Example Test Using Keywords
    [Documentation]    Shows how to use various types of keywords
    [Tags]    permissions

    # Custom keyword from session_keywords.robot
    Create API Session    api

    # Custom keyword from user_keywords.robot
    ${user}=    Create Test User    api

    # Library keyword from RequestsLibrary
    ${response}=    GET On Session    api    /api/users

    # Built-in keyword from Robot Framework
    Should Be Equal As Integers    ${response.status_code}    200

    # Cleanup
    Delete User    api    ${user}[id]
```

### When to Create Your Own Keywords

**Only create new keywords when:**
1. You've confirmed no existing keyword does what you need
2. The operation will be reused across multiple tests
3. The operation is complex and benefits from abstraction

**Example: Creating a suite-level keyword**

```robot
*** Keywords ***

Create Conversation With Memories
    [Documentation]    Create a conversation and extract memories (suite-specific helper)
    [Arguments]    ${session}    ${transcript_text}

    # Create conversation
    ${conv}=    Create Test Conversation    ${session}    ${transcript_text}

    # Wait for memory processing
    Sleep    2s

    # Get memories
    ${memories}=    Get User Memories    ${session}

    RETURN    ${conv}    ${memories}
```

**Where to create keywords:**
- **Suite-level** (in test file's `*** Keywords ***` section): Use 3+ times in same suite
- **Resource file** (e.g., `conversation_keywords.robot`): Use across multiple test suites

### Important Keyword Guidelines

From [@tests/TESTING_GUIDELINES.md](TESTING_GUIDELINES.md):

1. **Verifications stay in tests** - Don't abstract assertions into keywords
2. **Keywords are for actions** - Setup, API calls, data manipulation
3. **Check existing keywords first** - Avoid duplication
4. **Use descriptive names** - `Create Test User` not `Make User`

---

## Working with API Sessions

### Understanding Sessions

API sessions in Robot Framework maintain:
- Base URL (e.g., `http://localhost:8001`)
- Authentication headers (JWT tokens)
- Connection pooling for performance

### Session Management Keywords

The project uses a session-based pattern defined in `session_keywords.robot`:

#### Create Authenticated Session

```robot
*** Test Cases ***

Example With Admin Session
    [Documentation]    Most common pattern - admin session
    [Tags]    permissions

    # Create authenticated session as admin (default credentials)
    Create API Session    api

    # Now you can make API calls
    ${response}=    GET On Session    api    /api/users
    Should Be Equal As Integers    ${response.status_code}    200
```

#### Create Session for Specific User

```robot
*** Test Cases ***

Example With Custom User Session
    [Documentation]    Create session with specific credentials
    [Tags]    permissions

    # First create the user
    Create API Session    admin_session
    ${user}=    Create Test User    admin_session    email=custom@example.com    password=mypass123

    # Create session for that user
    Create API Session    user_session    email=custom@example.com    password=mypass123

    # Make API calls as that user
    ${profile}=    Get User Details    user_session    me
    Should Be Equal    ${profile}[email]    custom@example.com

    # Cleanup
    Delete User    admin_session    ${user}[id]
```

#### Create Anonymous (Unauthenticated) Session

```robot
*** Test Cases ***

Example Testing Unauthorized Access
    [Documentation]    Test that endpoints require authentication
    [Tags]    permissions

    # Create session without authentication
    Get Anonymous Session    anon_session

    # This should fail with 401
    ${response}=    GET On Session    anon_session    /users/me    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401
```

### Making API Requests with Sessions

The project provides enhanced HTTP keywords that show detailed errors:

```robot
# GET request
${response}=    GET On Session    api    /api/conversations
${conversations}=    Set Variable    ${response.json()}

# POST request
&{data}=    Create Dictionary    name=Test    value=123
${response}=    POST On Session    api    /api/endpoint    json=${data}

# PUT request
&{updates}=    Create Dictionary    status=active
${response}=    PUT On Session    api    /api/users/${user_id}    json=${updates}

# DELETE request
${response}=    DELETE On Session    api    /api/users/${user_id}

# Expect specific status code
${response}=    GET On Session    api    /api/endpoint    expected_status=404
```

### Session Pattern Benefits

1. **Cleaner tests** - No manual token management
2. **Better errors** - Enhanced error messages show response body
3. **Reusable** - Same session for multiple requests
4. **Type-safe** - No typos in token headers

---

## Common Patterns and Examples

### Pattern 1: Arrange-Act-Assert

The standard test structure:

```robot
*** Test Cases ***

User Can Create And Delete Conversation Test
    [Documentation]    Test the full conversation lifecycle
    [Tags]    conversation

    # Arrange - Setup prerequisites
    Create API Session    api

    # Act - Perform the operation
    ${conv}=    Create Test Conversation    api    transcript=Hello world

    # Assert - Verify results (inline, not in keywords!)
    Should Not Be Empty    ${conv}[id]
    Should Contain    ${conv}[transcript]    Hello world

    # Cleanup
    Delete Conversation    api    ${conv}[id]
```

### Pattern 2: Testing Permissions

```robot
*** Test Cases ***

Regular User Cannot Access Admin Endpoint Test
    [Documentation]    Verify authorization controls work
    [Tags]    permissions

    # Create regular user (not admin)
    Create API Session    admin_session
    ${user}=    Create Test User    admin_session    is_superuser=False
    Create API Session    user_session    email=${user}[email]    password=${TEST_USER_PASSWORD}

    # Try to access admin endpoint
    ${response}=    GET On Session    user_session    /api/admin/config    expected_status=403
    Should Be Equal As Integers    ${response.status_code}    403

    # Cleanup
    Delete User    admin_session    ${user}[id]
```

### Pattern 3: Data Isolation Between Users

```robot
*** Test Cases ***

Users Cannot See Each Other's Data Test
    [Documentation]    Verify user data isolation
    [Tags]    permissions	memory

    # Create two users
    Create API Session    admin_session
    ${user1}=    Create Test User    admin_session
    ${user2}=    Create Test User    admin_session

    # Create sessions for both
    Create API Session    user1_session    email=${user1}[email]    password=${TEST_USER_PASSWORD}
    Create API Session    user2_session    email=${user2}[email]    password=${TEST_USER_PASSWORD}

    # User 1 creates a conversation
    ${conv}=    Create Test Conversation    user1_session    transcript=User 1 data

    # User 2 should not see User 1's conversations
    ${user2_convs}=    Get User Conversations    user2_session
    Should Be Empty    ${user2_convs}

    # Cleanup
    Delete User    admin_session    ${user1}[id]
    Delete User    admin_session    ${user2}[id]
```

### Pattern 4: Testing Error Cases

```robot
*** Test Cases ***

API Returns 404 For Non-Existent Resource Test
    [Documentation]    Test error handling for missing resources
    [Tags]    conversation

    Create API Session    api
    ${fake_id}=    Set Variable    00000000-0000-0000-0000-000000000000

    # Should return 404
    ${response}=    GET On Session    api    /api/conversations/${fake_id}    expected_status=404
    Should Be Equal As Integers    ${response.status_code}    404
```

### Pattern 5: Using Test Teardown

```robot
*** Test Cases ***

Test With Guaranteed Cleanup
    [Documentation]    Cleanup happens even if test fails
    [Tags]    permissions

    Create API Session    api
    ${user}=    Create Test User    api

    # Test operations that might fail
    ${details}=    Get User Details    api    ${user}[id]
    Should Be Equal    ${details}[email]    ${user}[email]

    # This runs even if test fails
    [Teardown]    Delete User    api    ${user}[id]
```

---

## Troubleshooting

### Common Issues

#### Issue: "No keyword with name 'Create API Session' found"

**Solution:** Import the resource file:

```robot
*** Settings ***
Resource    ../resources/session_keywords.robot
```

#### Issue: "Connection refused" or "Failed to connect"

**Solution:** Ensure backend is running:

```bash
# Start test infrastructure
cd tests/
./run-robot-tests.sh

# Or manually start services
cd backend/
docker-compose -f docker-compose-test.yml up -d
```

#### Issue: "401 Unauthorized" errors

**Solution:** Check your `.env.test` file has correct credentials:

```bash
# tests/setup/.env.test
ADMIN_EMAIL=test-admin@example.com
ADMIN_PASSWORD=test-admin-password-123
```

#### Issue: Tests pass individually but fail when run together

**Solution:** Add proper cleanup in teardown:

```robot
*** Test Cases ***

My Test
    [Documentation]    Test with cleanup
    [Tags]    permissions

    Create API Session    api
    ${user}=    Create Test User    api

    # ... test logic ...

    [Teardown]    Delete User    api    ${user}[id]
```

#### Issue: "Variable '${ADMIN_EMAIL}' not found"

**Solution:** Import test_env.py:

```robot
*** Settings ***
Variables    ../setup/test_env.py
```

#### Issue: VSCode not providing keyword completion

**Solution:**
1. Check that Robot Framework LSP extension is installed
2. Reload VSCode window (Cmd+Shift+P > "Reload Window")
3. Verify `.vscode/settings.json` has correct pythonpath:

```json
{
  "robot.pythonpath": [
    "${workspaceFolder}/tests",
    "${workspaceFolder}/tests/setup",
    "${workspaceFolder}/tests/resources"
  ]
}
```

### Debug Tips

#### View Detailed Logs

```bash
# Run with debug logging
robot --loglevel DEBUG tests/endpoints/auth_tests.robot

# Open log.html in browser for detailed step-by-step execution
open log.html
```

#### Stop on First Failure

```bash
robot --exitonfailure tests/
```

#### Run Only Failed Tests

```bash
# After a test run
robot --rerunfailed output.xml tests/
```

#### Leave Containers Running for Inspection

```bash
# Don't cleanup after test
CLEANUP_CONTAINERS=false robot tests/endpoints/auth_tests.robot

# Inspect backend logs
docker logs advanced-chronicle-backend-test-1

# Inspect database
docker exec -it advanced-mongo-test-1 mongosh test_db

# When done
docker-compose -f backend/docker-compose-test.yml down -v
```

---

## Additional Resources

- **[@tests/TESTING_GUIDELINES.md](TESTING_GUIDELINES.md)** - Comprehensive testing standards and patterns
- **[@tests/tags.md](tags.md)** - Complete list of approved test tags (only 11 permitted!)
- **[@tests/setup/README.md](setup/README.md)** - Environment setup and configuration details
- **[@tests/README.md](README.md)** - Test suite overview and coverage
- **[Robot Framework User Guide](https://robotframework.org/robotframework/latest/RobotFrameworkUserGuide.html)** - Official documentation
- **[RequestsLibrary Docs](https://marketsquare.github.io/robotframework-requests/doc/RequestsLibrary.html)** - HTTP request library documentation

---

## Quick Reference Card

### Essential Keywords

```robot
# Session Management
Create API Session    api
Get Anonymous Session    anon_session

# User Management
${user}=    Create Test User    api
Delete User    api    ${user}[id]
${user}=    Get User Details    api    me

# HTTP Requests
${response}=    GET On Session       api    /api/endpoint
${response}=    POST On Session      api    /api/endpoint    json=${data}
${response}=    PUT On Session       api    /api/endpoint    json=${data}
${response}=    DELETE On Session    api    /api/endpoint

# Assertions
Should Be Equal                      ${actual}    ${expected}
Should Be Equal As Integers          ${response.status_code}    200
Should Contain                       ${text}    substring
Dictionary Should Contain Key        ${dict}    key_name
Should Not Be Empty                  ${value}
```

### Test Template

```robot
*** Settings ***
Documentation    Brief description of test suite
Library          RequestsLibrary
Resource         ../resources/session_keywords.robot
Resource         ../resources/user_keywords.robot
Variables        ../setup/test_env.py

Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Test Cases ***

Descriptive Test Name Test
    [Documentation]    What this test validates
    [Tags]    appropriate-tag

    # Arrange
    Create API Session    api

    # Act
    ${result}=    Some Action    api

    # Assert
    Should Be Equal As Integers    ${result}[status]    200
```

---

**Happy Testing!** 🤖

For questions or issues, check the troubleshooting section or review existing test files for examples.
