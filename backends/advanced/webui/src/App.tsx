import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import { RecordingProvider } from './contexts/RecordingContext'
import Layout from './components/layout/Layout'
import LoginPage from './pages/LoginPage'
import Chat from './pages/Chat'
import ConversationsRouter from './pages/ConversationsRouter'
import MemoriesRouter from './pages/MemoriesRouter'
import MemoryDetail from './pages/MemoryDetail'
import TimelineRouter from './pages/TimelineRouter'
import Users from './pages/Users'
import System from './pages/System'
import Upload from './pages/Upload'
import Queue from './pages/Queue'
import LiveRecord from './pages/LiveRecord'
import Plugins from './pages/Plugins'
import Finetuning from './pages/Finetuning'
import ProtectedRoute from './components/auth/ProtectedRoute'
import { ErrorBoundary, PageErrorBoundary } from './components/ErrorBoundary'

function App() {
  console.log('🚀 Full App restored with working login!')

  // Get base path from Vite config (e.g., "/prod/" for path-based routing)
  const basename = import.meta.env.BASE_URL

  return (
    <ErrorBoundary>
      <ThemeProvider>
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
                    <ConversationsRouter />
                  </PageErrorBoundary>
                } />
                <Route path="live-record" element={
                  <PageErrorBoundary>
                    <LiveRecord />
                  </PageErrorBoundary>
                } />
                <Route path="chat" element={
                  <PageErrorBoundary>
                    <Chat />
                  </PageErrorBoundary>
                } />
                <Route path="conversations" element={
                  <PageErrorBoundary>
                    <ConversationsRouter />
                  </PageErrorBoundary>
                } />
                <Route path="memories/:id" element={
                  <PageErrorBoundary>
                    <MemoryDetail />
                  </PageErrorBoundary>
                } />
                <Route path="memories" element={
                  <PageErrorBoundary>
                    <MemoriesRouter />
                  </PageErrorBoundary>
                } />
                <Route path="timeline" element={
                  <PageErrorBoundary>
                    <TimelineRouter />
                  </PageErrorBoundary>
                } />
                <Route path="users" element={
                  <PageErrorBoundary>
                    <Users />
                  </PageErrorBoundary>
                } />
                <Route path="system" element={
                  <PageErrorBoundary>
                    <System />
                  </PageErrorBoundary>
                } />
                <Route path="upload" element={
                  <PageErrorBoundary>
                    <Upload />
                  </PageErrorBoundary>
                } />
                <Route path="queue" element={
                  <PageErrorBoundary>
                    <Queue />
                  </PageErrorBoundary>
                } />
                <Route path="plugins" element={
                  <PageErrorBoundary>
                    <Plugins />
                  </PageErrorBoundary>
                } />
                <Route path="finetuning" element={
                  <PageErrorBoundary>
                    <Finetuning />
                  </PageErrorBoundary>
                } />
              </Route>
            </Routes>
            </Router>
          </RecordingProvider>
        </AuthProvider>
      </ThemeProvider>
    </ErrorBoundary>
  )
}

export default App