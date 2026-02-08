import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 60000, // 60 seconds default timeout
})

export interface User {
  id: number
  username: string
  created_at: string
}

export interface Speaker {
  id: string
  name: string
  user_id: number
  created_at: string
  updated_at: string
}

export interface Annotation {
  id?: number
  audio_file_path: string
  audio_file_hash: string
  audio_file_name: string
  start_time: number
  end_time: number
  speaker_id?: string
  speaker_label?: string
  deepgram_speaker_label?: string
  label: 'CORRECT' | 'INCORRECT' | 'UNCERTAIN'
  confidence?: number
  transcription?: string
  user_id: number
  notes?: string
}

export interface QualityMetrics {
  overall_quality: number
  snr_db: number
  duration_seconds: number
  quality_level: string
  quality_color: string
  recommendations?: string[]
}

export interface AudioInfo {
  duration_seconds: number
  sample_rate: number
  channels: number
  format?: string
}

class ApiService {
  // Generic HTTP methods
  async get(url: string, config?: any) {
    const response = await api.get(url, config)
    return response
  }

  async post(url: string, data?: any, config?: any) {
    const response = await api.post(url, data, config)
    return response
  }

  async delete(url: string, config?: any) {
    const response = await api.delete(url, config)
    return response
  }

  // User management
  async getUsers(): Promise<User[]> {
    try {
      const response = await api.get('/users')
      return response.data
    } catch (error) {
      // If endpoint doesn't exist, return empty array and log
      console.warn('Users endpoint not available, using local storage')
      return []
    }
  }

  async getOrCreateUser(username: string): Promise<User> {
    try {
      const response = await api.post('/users', { username })
      return response.data
    } catch (error) {
      // Fallback to local user creation
      const existingUsers = JSON.parse(localStorage.getItem('users') || '[]')
      const existingUser = existingUsers.find((u: User) => u.username === username)
      
      if (existingUser) {
        return existingUser
      }

      const newUser: User = {
        id: Date.now(),
        username,
        created_at: new Date().toISOString()
      }
      
      existingUsers.push(newUser)
      localStorage.setItem('users', JSON.stringify(existingUsers))
      return newUser
    }
  }

  // Speaker management
  async getSpeakers(): Promise<Speaker[]> {
    const response = await api.get('/speakers')
    return response.data
  }

  async deleteSpeaker(speakerId: string): Promise<void> {
    await api.delete(`/speakers/${speakerId}`)
  }

  // Enrollment
  async enrollSpeaker(
    speakerId: string,
    speakerName: string,
    audioFile: File
  ): Promise<any> {
    const formData = new FormData()
    formData.append('file', audioFile)
    formData.append('speaker_id', speakerId)
    formData.append('speaker_name', speakerName)

    const response = await api.post('/enroll/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 120000, // 2 minutes for enrollment operations
    })
    return response.data
  }

  async enrollSpeakerBatch(
    speakerId: string,
    speakerName: string,
    audioFiles: File[]
  ): Promise<any> {
    const formData = new FormData()
    
    audioFiles.forEach(file => {
      formData.append('files', file)
    })
    formData.append('speaker_name', speakerName)
    formData.append('speaker_id', speakerId)

    const response = await api.post('/enroll/batch', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 120000, // 2 minutes for enrollment operations
    })
    return response.data
  }

  // Speaker identification
  async identifySpeaker(audioFile: File): Promise<any> {
    const formData = new FormData()
    formData.append('file', audioFile)

    const response = await api.post('/diarize-and-identify', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      timeout: 180000, // 3 minutes for inference operations
    })
    return response.data
  }

  // Health check
  async checkHealth(): Promise<boolean> {
    try {
      const response = await api.get('/health')
      return response.status === 200
    } catch {
      return false
    }
  }

  // Transcription methods
  async transcribeAudio(
    audioFile: File | Blob,
    params?: Record<string, any>
  ): Promise<any> {
    const formData = new FormData()
    formData.append('file', audioFile)

    const response = await api.post('/v1/listen', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      params,
      timeout: 300000, // 5 minutes for transcription operations
    })
    return response.data
  }

}

export const apiService = new ApiService()