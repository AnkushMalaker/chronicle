/**
 * Custom hook for managing Deepgram API key and session configuration
 */

import { useState, useEffect, useRef } from 'react'
import { apiService } from '../services/api'
import { sessionLogger } from '../utils/logger'
import { validateApiKey } from '../utils/common'

export type ApiKeySource = 'server' | 'manual' | 'loading'

export interface UseDeepgramSessionReturn {
  deepgramApiKey: string
  apiKeySource: ApiKeySource
  setDeepgramApiKey: (key: string) => void
  fetchApiKey: () => Promise<void>
  validateCurrentKey: () => { isValid: boolean; error?: string }
}

export function useDeepgramSession(): UseDeepgramSessionReturn {
  const [deepgramApiKey, setDeepgramApiKey] = useState('')
  const [apiKeySource, setApiKeySource] = useState<ApiKeySource>('loading')
  const apiKeyFetchedRef = useRef(false)

  const fetchApiKey = async () => {
    if (apiKeyFetchedRef.current) return
    apiKeyFetchedRef.current = true

    sessionLogger.info('Fetching Deepgram API key from server...')

    try {
      const response = await apiService.get('/deepgram/config')
      sessionLogger.info('✅ Server API key retrieved successfully')
      setDeepgramApiKey(response.data.api_key)
      setApiKeySource('server')
    } catch (error) {
      const status = (error as any)?.response?.status
      sessionLogger.info(`❌ Server API key not available: ${status}`)
      sessionLogger.info('User will need to provide API key manually')
      setApiKeySource('manual')
    }
  }

  const validateCurrentKey = () => {
    return validateApiKey(deepgramApiKey)
  }

  // Fetch API key on mount
  useEffect(() => {
    fetchApiKey()
  }, [])

  return {
    deepgramApiKey,
    apiKeySource,
    setDeepgramApiKey,
    fetchApiKey,
    validateCurrentKey
  }
}
