import { Annotation } from './api'

class DatabaseService {
  private getStorageKey(userId: number): string {
    return `annotations_user_${userId}`
  }

  private getHashStorageKey(hash: string, userId: number): string {
    return `annotations_${hash}_user_${userId}`
  }

  // Save annotations to localStorage with file hash
  async saveAnnotations(
    fileHash: string,
    fileName: string,
    annotations: Annotation[],
    userId: number
  ): Promise<void> {
    try {
      const storageKey = this.getHashStorageKey(fileHash, userId)
      const data = {
        fileHash,
        fileName,
        annotations,
        userId,
        savedAt: new Date().toISOString()
      }

      localStorage.setItem(storageKey, JSON.stringify(data))

      // Also maintain a list of all annotation files for this user
      const userKey = this.getStorageKey(userId)
      const existingFiles = JSON.parse(localStorage.getItem(userKey) || '[]')

      const fileEntry = {
        hash: fileHash,
        name: fileName,
        annotationCount: annotations.length,
        lastSaved: new Date().toISOString()
      }

      const existingIndex = existingFiles.findIndex((f: any) => f.hash === fileHash)
      if (existingIndex >= 0) {
        existingFiles[existingIndex] = fileEntry
      } else {
        existingFiles.push(fileEntry)
      }

      localStorage.setItem(userKey, JSON.stringify(existingFiles))
    } catch (error) {
      console.error('Failed to save annotations:', error)
      throw error
    }
  }

  // Load annotations by file hash
  async loadAnnotations(fileHash: string, userId: number): Promise<Annotation[] | null> {
    try {
      const storageKey = this.getHashStorageKey(fileHash, userId)
      const data = localStorage.getItem(storageKey)

      if (!data) {
        return null
      }

      const parsed = JSON.parse(data)
      return parsed.annotations
    } catch (error) {
      console.error('Failed to load annotations:', error)
      return null
    }
  }

  // Check if annotations exist for a file hash
  async hasAnnotations(fileHash: string, userId: number): Promise<boolean> {
    const storageKey = this.getHashStorageKey(fileHash, userId)
    return localStorage.getItem(storageKey) !== null
  }

  // Get annotation metadata for a file
  async getAnnotationMetadata(fileHash: string, userId: number): Promise<any> {
    try {
      const storageKey = this.getHashStorageKey(fileHash, userId)
      const data = localStorage.getItem(storageKey)

      if (!data) {
        return null
      }

      const parsed = JSON.parse(data)
      return {
        fileName: parsed.fileName,
        annotationCount: parsed.annotations.length,
        savedAt: parsed.savedAt
      }
    } catch (error) {
      console.error('Failed to get annotation metadata:', error)
      return null
    }
  }

  // List all annotation files for a user
  async getUserAnnotationFiles(userId: number): Promise<any[]> {
    try {
      const userKey = this.getStorageKey(userId)
      const data = localStorage.getItem(userKey)
      return data ? JSON.parse(data) : []
    } catch (error) {
      console.error('Failed to get user annotation files:', error)
      return []
    }
  }

  // Delete annotations for a file
  async deleteAnnotations(fileHash: string, userId: number): Promise<void> {
    try {
      const storageKey = this.getHashStorageKey(fileHash, userId)
      localStorage.removeItem(storageKey)

      // Remove from user file list
      const userKey = this.getStorageKey(userId)
      const existingFiles = JSON.parse(localStorage.getItem(userKey) || '[]')
      const filteredFiles = existingFiles.filter((f: any) => f.hash !== fileHash)
      localStorage.setItem(userKey, JSON.stringify(filteredFiles))
    } catch (error) {
      console.error('Failed to delete annotations:', error)
      throw error
    }
  }

  // Clear all annotations for a user
  async clearUserAnnotations(userId: number): Promise<void> {
    try {
      const files = await this.getUserAnnotationFiles(userId)
      for (const file of files) {
        await this.deleteAnnotations(file.hash, userId)
      }

      const userKey = this.getStorageKey(userId)
      localStorage.removeItem(userKey)
    } catch (error) {
      console.error('Failed to clear user annotations:', error)
      throw error
    }
  }
}

export const databaseService = new DatabaseService()
