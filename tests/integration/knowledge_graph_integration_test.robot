*** Settings ***
Documentation    Knowledge Graph Integration Tests
...
...              End-to-end tests that verify the knowledge graph populates correctly
...              after audio upload and memory extraction. Requires API keys for real
...              LLM entity extraction.
Library          RequestsLibrary
Library          Collections
Library          String
Library          DateTime
Resource         ../setup/setup_keywords.robot
Resource         ../setup/teardown_keywords.robot
Resource         ../resources/session_keywords.robot
Resource         ../resources/audio_keywords.robot
Resource         ../resources/conversation_keywords.robot
Resource         ../resources/memory_keywords.robot
Resource         ../resources/queue_keywords.robot
Resource         ../resources/knowledge_graph_keywords.robot
Variables        ../setup/test_env.py
Variables        ../setup/test_data.py
Suite Setup      Suite Setup
Suite Teardown   Suite Teardown
Test Setup       Clear Test Databases

*** Test Cases ***

Memory Processing Stores Conversation Docs In FalkorDB
    [Documentation]    Upload audio, wait for memory extraction, verify conversation docs stored in FalkorDB graph
    [Tags]    e2e	requires-api-keys
    [Timeout]    600s

    # Upload audio and wait for full pipeline (transcription + memory extraction)
    ${conversation}    ${memories}=    Upload Audio File And Wait For Memory
    ...    ${TEST_AUDIO_FILE}
    ...    ${TEST_DEVICE_NAME}

    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]
    Log    Conversation processed: ${conversation_id}

    # Verify conversation doc was stored in FalkorDB
    ${response}=    Get Conversation Docs    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Should Be True    ${body}[count] > 0    No conversation docs stored in FalkorDB after memory processing

    # Verify doc structure - ConvDoc nodes were written to and read from FalkorDB
    ${first_doc}=    Set Variable    ${body}[conversations][0]
    Dictionary Should Contain Key    ${first_doc}    conversation_id
    Dictionary Should Contain Key    ${first_doc}    title
    Dictionary Should Contain Key    ${first_doc}    summary
    Dictionary Should Contain Key    ${first_doc}    date
    Dictionary Should Contain Key    ${first_doc}    people
    Log    Stored conversation doc: ${first_doc}[conversation_id] - ${first_doc}[title]

    Set Suite Variable    ${TEST_CONVERSATION_ID}    ${conversation_id}

KG Knowledge Base CRUD Via FalkorDB-Backed Service
    [Documentation]    Write and read knowledge base content - validates FalkorDB service is operational
    [Tags]    e2e	requires-api-keys
    [Timeout]    60s

    # Write content
    ${test_content}=    Set Variable    E2E test: FalkorDB knowledge base round-trip at ${TEST_CONVERSATION_ID}
    ${update_response}=    Update Knowledge Base    api    ${test_content}
    Should Be Equal As Integers    ${update_response.status_code}    200

    # Read back
    ${get_response}=    Get Knowledge Base    api
    Should Be Equal As Integers    ${get_response.status_code}    200
    Should Be Equal    ${get_response.json()}[content]    ${test_content}

KG Entities Endpoint Returns Valid Response After Processing
    [Documentation]    Verify KG entities endpoint works with FalkorDB (may be empty if LLM extraction didn't produce entities)
    [Tags]    e2e	requires-api-keys
    [Timeout]    60s

    ${response}=    Get KG Entities    api
    Should Be Equal As Integers    ${response.status_code}    200
    ${body}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${body}    entities
    Dictionary Should Contain Key    ${body}    count
    Log    KG entities count: ${body}[count]

    # If entities were extracted, verify structure
    ${entity_count}=    Get Length    ${body}[entities]
    IF    ${entity_count} > 0
        ${first}=    Set Variable    ${body}[entities][0]
        Dictionary Should Contain Key    ${first}    id
        Dictionary Should Contain Key    ${first}    name
        Dictionary Should Contain Key    ${first}    type
        Log    First entity: ${first}[name] (${first}[type])
    ELSE
        Log    No entities extracted (LLM may not have produced valid JSON) - FalkorDB endpoint still works    WARN
    END

KG People And Timeline Endpoints Work
    [Documentation]    Verify people and timeline endpoints query FalkorDB successfully
    [Tags]    e2e	requires-api-keys
    [Timeout]    60s

    # People endpoint
    ${people_response}=    Get KG People    api
    Should Be Equal As Integers    ${people_response.status_code}    200
    Dictionary Should Contain Key    ${people_response.json()}    people
    Log    People count: ${people_response.json()}[count]

    # Timeline endpoint
    ${timeline_response}=    Get KG Timeline    api    2020-01-01T00:00:00    2030-01-01T00:00:00
    Should Be Equal As Integers    ${timeline_response.status_code}    200
    Dictionary Should Contain Key    ${timeline_response.json()}    entities
    Log    Timeline entities: ${timeline_response.json()}[count]
