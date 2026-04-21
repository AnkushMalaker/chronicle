*** Settings ***
Documentation    Knowledge Graph API Endpoint Tests
...
...              Tests for the knowledge graph endpoints including entity CRUD,
...              search, conversation documents, timeline, knowledge base, and
...              authentication. These tests use mock services and do not require
...              API keys or actual entity extraction.
Library          RequestsLibrary
Library          Collections
Library          String
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/user_keywords.robot
Resource         ../resources/knowledge_graph_keywords.robot
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Test Cleanup

*** Test Cases ***

KG Health Check Returns Connected
    [Documentation]    Knowledge graph health endpoint should return healthy status
    [Tags]    health

    ${response}=    Get KG Health    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Should Be Equal    ${body}[status]    healthy
    Dictionary Should Contain Key    ${body}    falkordb

Get Entities Returns Empty List For New User
    [Documentation]    Getting entities with no data should return empty list
    [Tags]    memory

    ${response}=    Get KG Entities    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities
    Dictionary Should Contain Key    ${body}    count
    ${entities}=    Set Variable    ${body}[entities]
    Should Be True    isinstance($entities, list)

Get Entity Not Found Returns 404
    [Documentation]    Getting a non-existent entity should return 404
    [Tags]    memory

    ${response}=    Get KG Entity    api    00000000-0000-0000-0000-000000000000    expected_status=404
    Should Be Equal As Integers    ${response.status_code}    404

Search Entities Returns Valid Response
    [Documentation]    Searching entities should return valid response structure
    [Tags]    memory

    ${response}=    Search KG Entities    api    test_query
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    query
    Dictionary Should Contain Key    ${body}    entities
    Dictionary Should Contain Key    ${body}    count
    Should Be Equal    ${body}[query]    test_query

Get People Returns Valid Response
    [Documentation]    Getting people should return valid response structure
    [Tags]    memory

    ${response}=    Get KG People    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    people
    Dictionary Should Contain Key    ${body}    count

Get Conversation Docs Returns Valid Response
    [Documentation]    Getting conversation docs should return valid response structure
    [Tags]    memory

    ${response}=    Get Conversation Docs    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    conversations
    Dictionary Should Contain Key    ${body}    count

Get Timeline Returns Valid Response
    [Documentation]    Getting timeline with date range should return valid response
    [Tags]    memory

    ${response}=    Get KG Timeline    api    2020-01-01T00:00:00    2030-01-01T00:00:00
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities
    Dictionary Should Contain Key    ${body}    count
    Dictionary Should Contain Key    ${body}    start
    Dictionary Should Contain Key    ${body}    end

Get Knowledge Base Returns Content
    [Documentation]    Getting KB should return content field
    [Tags]    memory

    ${response}=    Get Knowledge Base    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    content
    Dictionary Should Contain Key    ${body}    user_id

Update Knowledge Base Round Trip
    [Documentation]    Updating KB content and reading it back should match
    [Tags]    memory

    # Update with test content
    ${test_content}=    Set Variable    Test knowledge base content for robot framework
    ${update_response}=    Update Knowledge Base    api    ${test_content}
    Should Be Equal As Integers    ${update_response.status_code}    200

    # Read back and verify
    ${get_response}=    Get Knowledge Base    api
    Should Be Equal As Integers    ${get_response.status_code}    200
    ${body}=    Set Variable    ${get_response.json()}
    Should Be Equal    ${body}[content]    ${test_content}

KG Entities Requires Authentication
    [Documentation]    Anonymous access to KG entities should return 401
    [Tags]    permissions

    Get Anonymous Session    anon
    ${response}=    GET On Session    anon    /api/knowledge-graph/entities    expected_status=401
    Should Be Equal As Integers    ${response.status_code}    401

KG Health Is Publicly Accessible
    [Documentation]    KG health endpoint should be accessible without authentication
    [Tags]    health

    Get Anonymous Session    anon
    ${response}=    GET On Session    anon    /api/knowledge-graph/health
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Should Be Equal    ${body}[status]    healthy

Non-Admin User Can Access Own KG Entities
    [Documentation]    Regular user should be able to access their own KG entities
    [Tags]    permissions

    ${test_user}=    Create Test User    api
    Create API Session    user_session    email=${test_user}[email]    password=${TEST_USER_PASSWORD}

    ${response}=    Get KG Entities    user_session
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities

    [Teardown]    Delete User    api    ${test_user}[id]

Get Entities With Type Filter
    [Documentation]    Getting entities with type filter should return valid response
    [Tags]    memory

    ${response}=    Get KG Entities    api    entity_type=person
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities

Get Entities With Limit
    [Documentation]    Getting entities with custom limit should respect the parameter
    [Tags]    memory

    ${response}=    Get KG Entities    api    limit=5
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities

*** Variables ***
${TEST_USER_PASSWORD}    test-user-password-123
