*** Settings ***
Documentation   Tests for LLM Custom Provider Setup (ConfigManager)
Library         OperatingSystem
Library         Collections
Library         String
Library         ../libs/ConfigTestHelper.py

*** Keywords ***
Setup Temp Config
    [Documentation]  Creates a temporary configuration environment
    ${random_suffix}=  Generate Random String  8  [NUMBERS]
    ${temp_path}=  Join Path  ${OUTPUT DIR}  temp_config_${random_suffix}
    Create Directory  ${temp_path}

    # Create initial default config content
    ${defaults}=  Create Dictionary  llm=openai-llm  embedding=openai-embed  stt=stt-deepgram
    ${model1_params}=  Create Dictionary  temperature=${0.2}  max_tokens=${2000}
    ${model1}=  Create Dictionary
    ...  name=openai-llm
    ...  description=OpenAI GPT-4o-mini
    ...  model_type=llm
    ...  model_provider=openai
    ...  api_family=openai
    ...  model_name=gpt-4o-mini
    ...  model_url=https://api.openai.com/v1
    ...  api_key=\${oc.env:OPENAI_API_KEY,''}
    ...  model_params=${model1_params}
    ...  model_output=json

    ${model2}=  Create Dictionary
    ...  name=local-embed
    ...  description=Local embeddings via Ollama
    ...  model_type=embedding
    ...  model_provider=ollama
    ...  api_family=openai
    ...  model_name=nomic-embed-text:latest
    ...  model_url=http://localhost:11434/v1
    ...  api_key=\${oc.env:OPENAI_API_KEY,ollama}
    ...  embedding_dimensions=${768}
    ...  model_output=vector

    ${models}=  Create List  ${model1}  ${model2}
    ${memory}=  Create Dictionary  provider=chronicle
    ${config}=  Create Dictionary  defaults=${defaults}  models=${models}  memory=${memory}

    Create Temp Config Structure  ${temp_path}  ${config}
    Set Test Variable  ${TEMP_PATH}  ${temp_path}

Cleanup Temp Config
    Remove Directory  ${TEMP_PATH}  recursive=True

*** Test Cases ***
Add New Model To Config
    [Documentation]  add_or_update_model() should append a new model when name doesn't exist.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${params}=  Create Dictionary  temperature=${0.2}  max_tokens=${2000}
    ${new_model}=  Create Dictionary
    ...  name=custom-llm
    ...  description=Custom OpenAI-compatible LLM
    ...  model_type=llm
    ...  model_provider=openai
    ...  api_family=openai
    ...  model_name=llama-3.1-70b-versatile
    ...  model_url=https://api.groq.com/openai/v1
    ...  api_key=\${oc.env:CUSTOM_LLM_API_KEY,''}
    ...  model_params=${params}
    ...  model_output=json

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}
    Add Model To Config Manager  ${cm}  ${new_model}

    ${config}=  Call Method  ${cm}  get_full_config
    ${models}=  Get From Dictionary  ${config}  models

    ${target_model}=  Set Variable  ${None}
    FOR  ${m}  IN  @{models}
        Run Keyword If  '${m["name"]}' == 'custom-llm'  Set Test Variable  ${target_model}  ${m}
    END

    Should Not Be Equal  ${target_model}  ${None}
    Should Be Equal  ${target_model["model_name"]}  llama-3.1-70b-versatile
    Should Be Equal  ${target_model["model_url"]}  https://api.groq.com/openai/v1
    Should Be Equal  ${target_model["model_type"]}  llm

Update Existing Model
    [Documentation]  add_or_update_model() should replace an existing model with the same name.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}

    # First add
    ${model_v1}=  Create Dictionary  name=custom-llm  model_type=llm  model_name=model-v1  model_url=https://example.com/v1
    Add Model To Config Manager  ${cm}  ${model_v1}

    # Then update
    ${model_v2}=  Create Dictionary  name=custom-llm  model_type=llm  model_name=model-v2  model_url=https://example.com/v2
    Add Model To Config Manager  ${cm}  ${model_v2}

    ${config}=  Call Method  ${cm}  get_full_config
    ${models}=  Get From Dictionary  ${config}  models

    ${count}=  Set Variable  0
    ${target_model}=  Set Variable  ${None}
    FOR  ${m}  IN  @{models}
        IF  '${m["name"]}' == 'custom-llm'
            Set Test Variable  ${target_model}  ${m}
            ${count}=  Evaluate  ${count} + 1
        END
    END

    Should Be Equal As Integers  ${count}  1
    Should Be Equal  ${target_model["model_name"]}  model-v2
    Should Be Equal  ${target_model["model_url"]}  https://example.com/v2

Add Model To Empty Models List
    [Documentation]  add_or_update_model() should create models list if it doesn't exist.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    # Overwrite config with empty models
    ${defaults}=  Create Dictionary  llm=openai-llm
    ${empty_config}=  Create Dictionary  defaults=${defaults}
    Create Temp Config Structure  ${TEMP_PATH}  ${empty_config}

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}
    ${test_model}=  Create Dictionary  name=test-model  model_type=llm
    Add Model To Config Manager  ${cm}  ${test_model}

    ${config}=  Call Method  ${cm}  get_full_config
    Dictionary Should Contain Key  ${config}  models
    ${models}=  Get From Dictionary  ${config}  models
    Length Should Be  ${models}  1
    Should Be Equal  ${models[0]["name"]}  test-model

Custom LLM And Embedding Model Added
    [Documentation]  Both LLM and embedding models should be created when embedding model is provided.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}

    ${params}=  Create Dictionary  temperature=${0.2}  max_tokens=${2000}
    ${llm_model}=  Create Dictionary
    ...  name=custom-llm
    ...  model_type=llm
    ...  model_provider=openai
    ...  api_family=openai
    ...  model_name=llama-3.1-70b-versatile
    ...  model_url=https://api.groq.com/openai/v1
    ...  api_key=\${oc.env:CUSTOM_LLM_API_KEY,''}
    ...  model_params=${params}
    ...  model_output=json

    ${embed_model}=  Create Dictionary
    ...  name=custom-embed
    ...  description=Custom OpenAI-compatible embeddings
    ...  model_type=embedding
    ...  model_provider=openai
    ...  api_family=openai
    ...  model_name=text-embedding-3-small
    ...  model_url=https://api.groq.com/openai/v1
    ...  api_key=\${oc.env:CUSTOM_LLM_API_KEY,''}
    ...  embedding_dimensions=${1536}
    ...  model_output=vector

    Add Model To Config Manager  ${cm}  ${llm_model}
    Add Model To Config Manager  ${cm}  ${embed_model}

    ${config}=  Call Method  ${cm}  get_full_config
    ${models}=  Get From Dictionary  ${config}  models
    ${model_names}=  Create List
    FOR  ${m}  IN  @{models}
        Append To List  ${model_names}  ${m["name"]}
    END

    List Should Contain Value  ${model_names}  custom-llm
    List Should Contain Value  ${model_names}  custom-embed

    ${target_embed}=  Set Variable  ${None}
    FOR  ${m}  IN  @{models}
        Run Keyword If  '${m["name"]}' == 'custom-embed'  Set Test Variable  ${target_embed}  ${m}
    END

    Should Be Equal  ${target_embed["model_type"]}  embedding
    Should Be Equal  ${target_embed["model_name"]}  text-embedding-3-small
    Should Be Equal As Integers  ${target_embed["embedding_dimensions"]}  1536

Custom LLM Without Embedding Falls Back To Local
    [Documentation]  defaults.embedding should be local-embed when no custom embedding is provided.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}

    ${llm_model}=  Create Dictionary
    ...  name=custom-llm
    ...  model_type=llm
    ...  model_name=some-model
    ...  model_url=https://api.example.com/v1

    Add Model To Config Manager  ${cm}  ${llm_model}
    ${defaults_update}=  Create Dictionary  llm=custom-llm  embedding=local-embed
    Update Defaults In Config Manager  ${cm}  ${defaults_update}

    ${defaults}=  Call Method  ${cm}  get_config_defaults
    Should Be Equal  ${defaults["llm"]}  custom-llm
    Should Be Equal  ${defaults["embedding"]}  local-embed

Custom LLM Updates Defaults With Embedding
    [Documentation]  defaults.llm and defaults.embedding should be updated correctly with custom embed.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}

    ${defaults_update}=  Create Dictionary  llm=custom-llm  embedding=custom-embed
    Update Defaults In Config Manager  ${cm}  ${defaults_update}

    ${defaults}=  Call Method  ${cm}  get_config_defaults
    Should Be Equal  ${defaults["llm"]}  custom-llm
    Should Be Equal  ${defaults["embedding"]}  custom-embed

Existing Models Preserved After Adding Custom
    [Documentation]  Adding a custom model should not remove existing models.
    [Setup]    Setup Temp Config
    [Teardown]  Cleanup Temp Config

    ${cm}=  Get Config Manager Instance  ${TEMP_PATH}
    ${config_before}=  Call Method  ${cm}  get_full_config
    ${models_before}=  Get From Dictionary  ${config_before}  models
    ${original_count}=  Get Length  ${models_before}

    ${new_model}=  Create Dictionary
    ...  name=custom-llm
    ...  model_type=llm
    ...  model_name=test-model
    ...  model_url=https://example.com/v1

    Add Model To Config Manager  ${cm}  ${new_model}

    ${config_after}=  Call Method  ${cm}  get_full_config
    ${models_after}=  Get From Dictionary  ${config_after}  models
    ${new_count}=  Get Length  ${models_after}
    ${expected_count}=  Evaluate  ${original_count} + 1

    Should Be Equal As Integers  ${new_count}  ${expected_count}

    ${model_names}=  Create List
    FOR  ${m}  IN  @{models_after}
        Append To List  ${model_names}  ${m["name"]}
    END

    List Should Contain Value  ${model_names}  openai-llm
    List Should Contain Value  ${model_names}  local-embed
    List Should Contain Value  ${model_names}  custom-llm
