*** Settings ***
Documentation    Audio Keywords
Library          RequestsLibrary
Library          Collections
Library          OperatingSystem
Variables        ../setup/test_data.py
Resource         session_keywords.robot
Resource         conversation_keywords.robot
Resource         queue_keywords.robot

*** Keywords ***
Upload Audio File
      [Documentation]    Upload audio file using session with proper multipart form data
      [Arguments]    ${audio_file_path}    ${device_name}=robot-test    ${folder}=.

      # Verify file exists
      File Should Exist    ${audio_file_path}

      # Debug the request being sent
      
      Log    Sending file: ${audio_file_path}
      Log    Device name: ${device_name}
      Log    Folder: ${folder}

      # Create proper file upload using Python expressions to actually open the file
      Log    Files dictionary will contain: files -> ${audio_file_path}
      Log    Data dictionary will contain: device_name -> ${device_name}

    #   # Build params dict with optional folder parameter
          ${response}=       POST On Session    api    /api/audio/upload
          ...                files=${{ {'files': open('${audio_file_path}', 'rb')} }}
          ...                params=device_name=${device_name}&folder=${folder}
          ...                expected_status=any

      # Detailed debugging of the response
      Log    Upload response status: ${response.status_code}
      Log    Upload response headers: ${response.headers}
      Log    Upload response content type: ${response.headers.get('content-type', 'not set')}
      Log    Upload response text length: ${response.text.__len__()}
      Log    Upload response raw text: ${response.text}

      # Parse JSON response to dictionary
      ${upload_response}=    Set Variable    ${response.json()}
      Log    Parsed upload response: ${upload_response}

      # Validate upload was successful
      Should Be Equal As Strings    ${upload_response['summary']['processing']}    1    Upload failed: No files enqueued
      Should Be Equal As Strings    ${upload_response['files'][0]['status']}    processing    Upload failed: ${response.text}

      # Extract important values
      ${audio_uuid}=    Set Variable    ${upload_response['files'][0]['audio_uuid']}
      ${job_id}=        Set Variable    ${upload_response['files'][0]['conversation_id']}
      ${transcript_job_id}=    Set Variable    ${upload_response['files'][0]['transcript_job_id']}
      Log    Audio UUID: ${audio_uuid}
      Log    Conversation ID: ${job_id}
      Log    Transcript Job ID: ${transcript_job_id}

      # Wait for conversation to be created and transcribed
      Log    Waiting for transcription to complete...

      Wait Until Keyword Succeeds    60s    5s       Check job status   ${transcript_job_id}    completed
      ${job}=    Get Job Details    ${transcript_job_id}

     # Get the completed conversation
      ${conversation}=     Get Conversation By ID    ${job}[result][conversation_id]
      Should Not Be Equal    ${conversation}    ${None}    Conversation not found after upload and processing

      Log    Found conversation: ${conversation}
      RETURN    ${conversation}


Upload Audio File And Wait For Memory
    [Documentation]    Upload audio file and wait for complete processing including memory extraction.
    ...                This is for E2E testing - use Upload Audio File for upload-only tests.
    ...                Performs assertions inline to verify successful memory extraction.
    [Arguments]    ${audio_file_path}    ${device_name}=robot-test    ${folder}=.    ${min_memories}=1

    # Upload file (uses existing keyword)
    ${conversation}=    Upload Audio File    ${audio_file_path}    ${device_name}    ${folder}

    # Get conversation ID to find memory job
    ${conversation_id}=    Set Variable    ${conversation}[conversation_id]
    Log    Conversation ID: ${conversation_id}

    # Find memory job for this conversation
    ${memory_jobs}=    Get Jobs By Type And Conversation    process_memory_job    ${conversation_id}
    Should Not Be Empty    ${memory_jobs}    No memory job found for conversation ${conversation_id}

    ${memory_job}=    Set Variable    ${memory_jobs}[0]
    ${memory_job_id}=    Set Variable    ${memory_job}[job_id]

    Log    Found memory job: ${memory_job_id}

    # Wait for memory extraction (returns result dictionary)
    ${result}=    Wait For Memory Extraction    ${memory_job_id}

    # Verify memory extraction succeeded
    Should Be True    ${result}[success]
    ...    Memory extraction failed: ${result.get('error_message', 'Unknown error')}

    # Verify job completed successfully
    Should Be Equal As Strings    ${result}[status]    completed
    ...    Expected job status 'completed', got '${result}[status]'

    # Verify minimum memories were extracted
    ${memory_count}=    Set Variable    ${result}[memory_count]
    Should Be True    ${memory_count} >= ${min_memories}
    ...    Expected at least ${min_memories} memories, found ${memory_count}

    ${memories}=    Set Variable    ${result}[memories]
    Log    Successfully extracted ${memory_count} memories

    RETURN    ${conversation}    ${memories}


Get Cropped Audio Info
    [Documentation]    Get cropped audio information for a conversation
    [Arguments]     ${audio_uuid}

    ${response}=    GET On Session    api    /api/conversations/${audio_uuid}/cropped    headers=${headers}
    RETURN    ${response.json()}[cropped_audios]    
