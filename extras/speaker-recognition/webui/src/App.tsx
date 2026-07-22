import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { UserProvider } from './contexts/UserContext'
import Layout from './components/layout/Layout'
import AudioViewer from './pages/AudioViewer'
import Annotation from './pages/Annotation'
import Enrollment from './pages/Enrollment'
import EnrollmentHealth from './pages/EnrollmentHealth'
import Speakers from './pages/Speakers'
import Inference from './pages/Inference'
import InferLive from './pages/InferLive'
import InferLiveSimplified from './pages/InferLiveSimplified'
import { ThemeProvider } from './contexts/ThemeContext'

function App() {
  return (
    <UserProvider>
      <ThemeProvider>
        <Router future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <Layout>
            <Routes>
              <Route path="/" element={<AudioViewer />} />
              <Route path="/audio" element={<AudioViewer />} />
              <Route path="/annotation" element={<Annotation />} />
              <Route path="/enrollment" element={<Enrollment />} />
              <Route path="/enrollment-health" element={<EnrollmentHealth />} />
              <Route path="/speakers" element={<Speakers />} />
              <Route path="/inference" element={<Inference />} />
              <Route path="/infer-live" element={<InferLive />} />
              <Route path="/infer-live-simple" element={<InferLiveSimplified />} />
            </Routes>
          </Layout>
        </Router>
      </ThemeProvider>
    </UserProvider>
  )
}

export default App
