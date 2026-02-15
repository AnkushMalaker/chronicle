import { lazy, Suspense } from 'react'
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import { RecordingProvider } from './contexts/RecordingContext'
import Layout from './components/layout/Layout'
import LoginPage from './pages/LoginPage'
import ProtectedRoute from './components/auth/ProtectedRoute'
import { ErrorBoundary, PageErrorBoundary } from './components/ErrorBoundary'

// Lazy-loaded page components (code-split into separate chunks)
const Chat = lazy(() => import('./pages/Chat'))
const ConversationsRouter = lazy(() => import('./pages/ConversationsRouter'))
const MemoriesRouter = lazy(() => import('./pages/MemoriesRouter'))
const ConversationDetail = lazy(() => import('./pages/ConversationDetail'))
const MemoryDetail = lazy(() => import('./pages/MemoryDetail'))
const Users = lazy(() => import('./pages/Users'))
const System = lazy(() => import('./pages/System'))
const Settings = lazy(() => import('./pages/Settings'))
const Upload = lazy(() => import('./pages/Upload'))
const Queue = lazy(() => import('./pages/Queue'))
const LiveRecord = lazy(() => import('./pages/LiveRecord'))
const Plugins = lazy(() => import('./pages/Plugins'))
const Finetuning = lazy(() => import('./pages/Finetuning'))

function PageSkeleton() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
    </div>
  )
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

function App() {
  // Get base path from Vite config (e.g., "/prod/" for path-based routing)
  const basename = import.meta.env.BASE_URL

  return (
    <ErrorBoundary>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <RecordingProvider>
            <Router basename={basename} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/" element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }>
                <Route index element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <ConversationsRouter />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="live-record" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <LiveRecord />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="chat" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Chat />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="conversations/:id" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <ConversationDetail />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="conversations" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <ConversationsRouter />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="memories/:id" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <MemoryDetail />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="memories" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <MemoriesRouter />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="users" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Users />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="system" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <System />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="settings" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Settings />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="upload" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Upload />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="queue" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Queue />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="plugins" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Plugins />
                    </Suspense>
                  </PageErrorBoundary>
                } />
                <Route path="finetuning" element={
                  <PageErrorBoundary>
                    <Suspense fallback={<PageSkeleton />}>
                      <Finetuning />
                    </Suspense>
                  </PageErrorBoundary>
                } />
              </Route>
            </Routes>
            </Router>
          </RecordingProvider>
        </AuthProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}

export default App
