import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { authApi } from '../services/api'
import { getStorageKey } from '../utils/storage'

interface User {
  id: string
  name: string
  email: string
  is_superuser: boolean
}

interface AuthContextType {
  user: User | null
  token: string | null
  login: (email: string, password: string) => Promise<{success: boolean, error?: string, errorType?: string}>
  logout: () => void
  isLoading: boolean
  isAdmin: boolean
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [token, setToken] = useState<string | null>(localStorage.getItem(getStorageKey('token')))
  const [isLoading, setIsLoading] = useState(true)

  // Check if user is admin
  const isAdmin = user?.is_superuser || false

  useEffect(() => {
    const initAuth = async () => {
      console.log('🔐 AuthContext: Initializing authentication...')
      const savedToken = localStorage.getItem(getStorageKey('token'))
      console.log('🔐 AuthContext: Saved token exists:', !!savedToken)
      
      if (savedToken) {
        try {
          console.log('🔐 AuthContext: Verifying token with API call...')
          // Verify token is still valid by making a request
          const response = await authApi.getMe()
          console.log('🔐 AuthContext: API call successful, user data:', response.data)
          setUser(response.data)
          setToken(savedToken)
        } catch (error) {
          console.error('❌ AuthContext: Token verification failed:', error)
          // Token is invalid, clear it
          localStorage.removeItem(getStorageKey('token'))
          setToken(null)
          setUser(null)
        }
      } else {
        console.log('🔐 AuthContext: No saved token found')
      }
      console.log('🔐 AuthContext: Initialization complete, setting isLoading to false')
      setIsLoading(false)
    }

    initAuth()
  }, [])

  const login = async (email: string, password: string): Promise<{success: boolean, error?: string, errorType?: string}> => {
    try {
      const response = await authApi.login(email, password)

      const { access_token } = response.data
      setToken(access_token)
      localStorage.setItem(getStorageKey('token'), access_token)

      // Get user info
      const userResponse = await authApi.getMe()
      setUser(userResponse.data)

      return { success: true }
    } catch (error: any) {
      console.error('Login failed:', error)
      
      // Parse structured error response from backend
      let errorMessage = 'Login failed. Please try again.'
      let errorType = 'unknown'
      
      if (error.response?.data) {
        const errorData = error.response.data
        errorMessage = errorData.detail || errorMessage
        errorType = errorData.error_type || errorType
      } else if (error.code === 'ERR_NETWORK') {
        errorMessage = 'Unable to connect to server. Please check your connection and try again.'
        errorType = 'connection_failure'
      }
      
      return { 
        success: false, 
        error: errorMessage,
        errorType: errorType
      }
    }
  }

  const logout = () => {
    setUser(null)
    setToken(null)
    localStorage.removeItem(getStorageKey('token'))
  }

  return (
    <AuthContext.Provider value={{ user, token, login, logout, isLoading, isAdmin }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}