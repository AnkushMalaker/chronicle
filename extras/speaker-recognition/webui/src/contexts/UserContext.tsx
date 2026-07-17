import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { apiService } from '../services/api'

interface User {
  id: number
  username: string
  created_at: string
}

interface UserContextType {
  user: User | null
  users: User[]
  isLoading: boolean
  selectUser: (username: string) => Promise<void>
  createUser: (username: string) => Promise<void>
  refreshUsers: () => Promise<void>
}

const UserContext = createContext<UserContextType | undefined>(undefined)

export function UserProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [isLoading, setIsLoading] = useState(true)

  const refreshUsers = async () => {
    try {
      const userList = await apiService.getUsers()
      setUsers(userList)
    } catch (error) {
      console.error('Failed to fetch users:', error)
    }
  }

  const selectUser = async (username: string) => {
    try {
      const selectedUser = await apiService.getOrCreateUser(username)
      setUser(selectedUser)
      localStorage.setItem('selectedUser', JSON.stringify(selectedUser))
    } catch (error) {
      console.error('Failed to select user:', error)
      throw error
    }
  }

  const createUser = async (username: string) => {
    try {
      const newUser = await apiService.getOrCreateUser(username)
      await refreshUsers()
      setUser(newUser)
      localStorage.setItem('selectedUser', JSON.stringify(newUser))
    } catch (error) {
      console.error('Failed to create user:', error)
      throw error
    }
  }

  useEffect(() => {
    const initializeUser = async () => {
      setIsLoading(true)
      try {
        const userList = await apiService.getUsers()
        setUsers(userList)

        const savedUser = localStorage.getItem('selectedUser')
        if (savedUser) {
          const parsedUser = JSON.parse(savedUser)
          const canonicalUser = userList.find(u => u.username === parsedUser.username)
          if (canonicalUser) {
            setUser(canonicalUser)
            localStorage.setItem('selectedUser', JSON.stringify(canonicalUser))
          } else {
            localStorage.removeItem('selectedUser')
          }
        } else {
          if (userList.length === 0) {
            await createUser('admin')
          }
        }
      } catch (error) {
        console.error('Failed to initialize user:', error)
      } finally {
        setIsLoading(false)
      }
    }

    initializeUser()
  }, [])

  return (
    <UserContext.Provider value={{
      user,
      users,
      isLoading,
      selectUser,
      createUser,
      refreshUsers
    }}>
      {children}
    </UserContext.Provider>
  )
}

export function useUser() {
  const context = useContext(UserContext)
  if (context === undefined) {
    throw new Error('useUser must be used within a UserProvider')
  }
  return context
}
