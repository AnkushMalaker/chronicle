import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { apiService } from '../services/api'

interface User {
  id: string
  username: string
  created_at: string
}

interface UserContextType {
  user: User | null
  users: User[]
  isLoading: boolean
  selectUser: (userId: string) => void
  refreshUsers: () => Promise<void>
}

const SELECTED_USER_KEY = 'selectedUserId'

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

  // Tenants are Chronicle user ids; this service cannot mint one, so the
  // selector only picks among tenants that already have speaker data.
  const selectUser = (userId: string) => {
    const selected = users.find(u => u.id === userId)
    if (!selected) {
      console.error(`No such tenant: ${userId}`)
      return
    }
    setUser(selected)
    localStorage.setItem(SELECTED_USER_KEY, selected.id)
  }

  useEffect(() => {
    const initializeUser = async () => {
      setIsLoading(true)
      try {
        const userList = await apiService.getUsers()
        setUsers(userList)

        const savedId = localStorage.getItem(SELECTED_USER_KEY)
        const saved = savedId ? userList.find(u => u.id === savedId) : undefined
        if (saved) {
          setUser(saved)
        } else {
          localStorage.removeItem(SELECTED_USER_KEY)
          if (userList.length === 1) {
            setUser(userList[0])
            localStorage.setItem(SELECTED_USER_KEY, userList[0].id)
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
