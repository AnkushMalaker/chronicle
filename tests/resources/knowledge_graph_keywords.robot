*** Settings ***
Documentation    Knowledge Graph API Keywords
...
...              This file contains keywords for knowledge graph operations including
...              entity CRUD, search, conversation documents, people, timeline, and
...              knowledge base (MEMORY.md) management.
...
...              Keywords that should NOT be in this file:
...              - Verification/assertion keywords (belong in tests)
...              - Session management (belong in session_keywords.robot)
Library          RequestsLibrary
Library          Collections
Variables        ../setup/test_env.py

*** Keywords ***

Get KG Health
    [Documentation]    Get knowledge graph health status
    [Arguments]    ${session}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/health
    RETURN    ${response}

Get KG Entities
    [Documentation]    Get entities for authenticated user with optional filters
    [Arguments]    ${session}    ${entity_type}=${None}    ${limit}=100
    &{params}=    Create Dictionary    limit=${limit}
    IF    '${entity_type}' != '${None}'
        Set To Dictionary    ${params}    entity_type=${entity_type}
    END
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/entities    params=${params}
    RETURN    ${response}

Get KG Entity
    [Documentation]    Get a single entity by ID
    [Arguments]    ${session}    ${entity_id}    ${expected_status}=200
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/entities/${entity_id}    expected_status=${expected_status}
    RETURN    ${response}

Get KG Entity Relationships
    [Documentation]    Get all relationships for an entity
    [Arguments]    ${session}    ${entity_id}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/entities/${entity_id}/relationships
    RETURN    ${response}

Search KG Entities
    [Documentation]    Search entities by name or details
    [Arguments]    ${session}    ${query}    ${limit}=20
    &{params}=    Create Dictionary    query=${query}    limit=${limit}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/search    params=${params}
    RETURN    ${response}

Update KG Entity
    [Documentation]    Update an entity's name, details, or icon
    [Arguments]    ${session}    ${entity_id}    ${data}
    ${response}=    PATCH On Session    ${session}    /api/knowledge-graph/entities/${entity_id}    json=${data}
    RETURN    ${response}

Delete KG Entity
    [Documentation]    Delete an entity by ID
    [Arguments]    ${session}    ${entity_id}    ${expected_status}=200
    ${response}=    DELETE On Session    ${session}    /api/knowledge-graph/entities/${entity_id}    expected_status=${expected_status}
    RETURN    ${response}

Get Conversation Docs
    [Documentation]    Get conversation documents with linked people
    [Arguments]    ${session}    ${person}=${None}    ${limit}=50
    &{params}=    Create Dictionary    limit=${limit}
    IF    '${person}' != '${None}'
        Set To Dictionary    ${params}    person=${person}
    END
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/conversations    params=${params}
    RETURN    ${response}

Get KG People
    [Documentation]    Get distinct people with mention counts
    [Arguments]    ${session}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/people
    RETURN    ${response}

Get KG Timeline
    [Documentation]    Get entities within a time range
    [Arguments]    ${session}    ${start}    ${end}    ${limit}=100
    &{params}=    Create Dictionary    start=${start}    end=${end}    limit=${limit}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/timeline    params=${params}
    RETURN    ${response}

Get Knowledge Base
    [Documentation]    Get the user's basic memory (MEMORY.md content)
    [Arguments]    ${session}
    ${response}=    GET On Session    ${session}    /api/knowledge-graph/kb
    RETURN    ${response}

Update Knowledge Base
    [Documentation]    Update the user's basic memory content
    [Arguments]    ${session}    ${content}
    &{data}=    Create Dictionary    content=${content}
    ${response}=    PUT On Session    ${session}    /api/knowledge-graph/kb    json=${data}
    RETURN    ${response}
