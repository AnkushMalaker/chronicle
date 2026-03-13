*** Settings ***
Documentation    Chat Service Keywords
Library          RequestsLibrary
Library          Collections
Variables        ../setup/test_env.py
Resource         session_keywords.robot

*** Keywords ***

Create Chat Session
    [Documentation]    Create a new chat session (uses api session)
    [Arguments]    ${title}=${None}    ${expected_status}=200

    &{data}=       Create Dictionary

    IF    '${title}' != '${None}'
        Set To Dictionary    ${data}    title=${title}
    END

    ${response}=    POST On Session    api    /api/chat/sessions    json=${data}    expected_status=${expected_status}
    RETURN    ${response}

Get Chat Sessions
    [Documentation]    Get all chat sessions for user (uses api session)
    [Arguments]    ${limit}=50

    &{params}=     Create Dictionary    limit=${limit}
    ${response}=    GET On Session    api    /api/chat/sessions    params=${params}
    RETURN    ${response}

Delete Chat Session
    [Documentation]    Delete a chat session (uses api session)
    [Arguments]    ${session_id}    ${expected_status}=200

    ${response}=    DELETE On Session    api    /api/chat/sessions/${session_id}    expected_status=${expected_status}
    RETURN    ${response}

Create Test Chat Session
    [Documentation]    Create a test chat session with random title (uses admin session)
    [Arguments]    ${title_prefix}=Test Session
    ${random_suffix}=    Generate Random String    6    [LETTERS][NUMBERS]
    ${title}=            Set Variable    ${title_prefix} ${random_suffix}

    ${response}=         Create Chat Session    ${title}
    Should Be Equal As Integers    ${response.status_code}    200

    RETURN    ${response.json()}

Get Chat Session
    [Documentation]    Get a specific chat session (uses api session)
    [Arguments]    ${session_id}    ${expected_status}=200

    ${response}=    GET On Session    api    /api/chat/sessions/${session_id}    expected_status=${expected_status}
    RETURN    ${response}

Get Session Messages
    [Documentation]    Get messages from a chat session (uses api session)
    [Arguments]    ${session_id}    ${limit}=100    ${expected_status}=200

    &{params}=    Create Dictionary    limit=${limit}
    ${response}=    GET On Session    api    /api/chat/sessions/${session_id}/messages    params=${params}    expected_status=${expected_status}
    RETURN    ${response}

Update Chat Session
    [Documentation]    Update a chat session title (uses api session)
    [Arguments]    ${session_id}    ${new_title}    ${expected_status}=200

    &{data}=    Create Dictionary    title=${new_title}
    ${response}=    PUT On Session    api    /api/chat/sessions/${session_id}    json=${data}    expected_status=${expected_status}
    RETURN    ${response}

Get Chat Statistics
    [Documentation]    Get chat statistics for user (uses admin session)

    ${response}=    GET On Session    api    /api/chat/statistics
    RETURN    ${response}

Send Chat Message
    [Documentation]    Send a message via OpenAI-compatible completions endpoint (non-streaming)
    [Arguments]    ${session_id}    ${message_content}    ${expected_status}=200

    ${user_msg}=        Create Dictionary    role=user    content=${message_content}
    @{messages}=        Create List    ${user_msg}
    ${body}=            Create Dictionary    messages=${messages}    stream=${False}    session_id=${session_id}
    ${response}=        POST On Session    api    /api/chat/completions    json=${body}    expected_status=${expected_status}

    RETURN    ${response}
