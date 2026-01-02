import React, { useState, useCallback, useEffect } from 'react'
import { Upload as UploadIcon, File, X, CheckCircle, AlertCircle, RefreshCw, FolderPlus, PlayCircle, Archive } from 'lucide-react'
import { uploadApi, obsidianApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'

interface UploadFile {
  file: File
  id: string
  status: 'pending' | 'uploading' | 'success' | 'error'
  error?: string
}

export default function Upload() {
  const [files, setFiles] = useState<UploadFile[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [gdriveFolderId, setGdriveFolderId] = useState('')

  const { isAdmin } = useAuth()

  const generateId = () => Math.random().toString(36).substr(2, 9)

  const [gdriveUploadStatus, setGdriveUploadStatus] = useState<{
    type: 'success' | 'error' | null
    message: string
  }>({
    type: null,
    message: ''
  })

  // Obsidian import state
  const [obsidianZip, setObsidianZip] = useState<File | null>(null)
  const [obsidianUploadProgress, setObsidianUploadProgress] = useState(0)
  const [obsidianJobId, setObsidianJobId] = useState<string | null>(null)
  const [obsidianStatus, setObsidianStatus] = useState<any>(null)
  const [obsidianPolling, setObsidianPolling] = useState(false)
  const [obsidianMessage, setObsidianMessage] = useState('')
  const [obsidianError, setObsidianError] = useState('')

  // Handle Google Drive folder submission
  const handleGDriveSubmit = async () => {
    if (!gdriveFolderId) return

    setIsUploading(true)
    setGdriveUploadStatus({ type: null, message: '' })

    try {
      await uploadApi.uploadFromGDriveFolder({
        gdrive_folder_id: gdriveFolderId,
        device_name: 'upload',
        auto_generate_client: true,
      })

      setGdriveUploadStatus({
        type: 'success',
        message: 'Google Drive folder submitted successfully.',
      })

      setGdriveFolderId('')
    } catch (err: any) {
      setGdriveUploadStatus({
        type: 'error',
        message: err?.response?.data?.detail || 'Failed to upload folder.',
      })
    } finally {
      setIsUploading(false)
    }
  }

  const handleFileSelect = (selectedFiles: FileList | null) => {
    if (!selectedFiles) return

    const audioFiles = Array.from(selectedFiles).filter(
      (file) =>
        file.type.startsWith('audio/') ||
        file.name.toLowerCase().endsWith('.wav') ||
        file.name.toLowerCase().endsWith('.mp3') ||
        file.name.toLowerCase().endsWith('.m4a') ||
        file.name.toLowerCase().endsWith('.flac')
    )

    const newFiles: UploadFile[] = audioFiles.map((file) => ({
      file,
      id: generateId(),
      status: 'pending',
    }))

    setFiles((prev) => [...prev, ...newFiles])
  }

  const removeFile = (id: string) => {
    setFiles(files.filter((f) => f.id !== id))
  }

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') setDragActive(true)
    else if (e.type === 'dragleave') setDragActive(false)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    handleFileSelect(e.dataTransfer.files)
  }, [])

  const uploadFiles = async () => {
    if (files.length === 0) return

    setIsUploading(true)
    setUploadProgress(0)

    try {
      const formData = new FormData()
      files.forEach(({ file }) => {
        formData.append('files', file)
      })

      setFiles((prev) =>
        prev.map((f) => ({ ...f, status: 'uploading' }))
      )

      await uploadApi.uploadAudioFiles(formData, (progress) => {
        setUploadProgress(progress)
      })

      setFiles((prev) =>
        prev.map((f) => ({ ...f, status: 'success' }))
      )
    } catch (err: any) {
      console.error('Upload failed:', err)

      setFiles((prev) =>
        prev.map((f) => ({
          ...f,
          status: 'error',
          error: err.message || 'Upload failed',
        }))
      )
    } finally {
      setIsUploading(false)
      setUploadProgress(100)
    }
  }

  // Obsidian handlers
  const handleObsidianZipSelect = (fileList: FileList | null) => {
    const f = fileList?.[0]
    if (!f) return
    if (!f.name.toLowerCase().endsWith('.zip')) {
      setObsidianError('Please select a .zip file containing your Obsidian vault')
      return
    }
    setObsidianError('')
    setObsidianZip(f)
    setObsidianUploadProgress(0)
    setObsidianJobId(null)
    setObsidianStatus(null)
  }

  const uploadObsidianZip = async () => {
    if (!obsidianZip) return
    setObsidianError('')
    setObsidianMessage('')
    setObsidianUploadProgress(0)
    try {
      const res = await obsidianApi.uploadZip(obsidianZip, (p) => setObsidianUploadProgress(p))
      setObsidianJobId(res.data.job_id)
      setObsidianMessage(`Uploaded. Found ${res.data.total_files} notes. Click Start to ingest.`)
    } catch (err: any) {
      setObsidianError(err.response?.data?.detail || 'Failed to upload vault zip')
    }
  }

  const startObsidianIngestion = async () => {
    if (!obsidianJobId) return
    setObsidianError('')
    setObsidianMessage('Starting ingestion...')
    try {
      await obsidianApi.start(obsidianJobId)
      setObsidianPolling(true)
    } catch (err: any) {
      setObsidianError(err.response?.data?.detail || 'Failed to start ingestion')
    }
  }

  useEffect(() => {
    let interval: number | undefined
    if (obsidianPolling && obsidianJobId) {
      const poll = async () => {
        try {
          const res = await obsidianApi.status(obsidianJobId)
          setObsidianStatus(res.data)
          if (res.data.status === 'completed' || res.data.status === 'failed') {
            setObsidianPolling(false)
            setObsidianMessage(res.data.status === 'completed' ? '✅ Ingestion completed' : `❌ Failed: ${res.data.error || 'unknown error'}`)
          }
        } catch (err) {
          setObsidianPolling(false)
          setObsidianError('Failed to fetch status')
        }
      }
      poll()
      interval = window.setInterval(poll, 1500)
    }
    return () => {
      if (interval) window.clearInterval(interval)
    }
  }, [obsidianPolling, obsidianJobId])

  const clearCompleted = () => {
    setFiles(files.filter((f) => f.status === 'pending' || f.status === 'uploading'))
  }

  const formatFileSize = (bytes: number) => {
    if (bytes === 0) return '0 Bytes'
    const k = 1024
    const sizes = ['Bytes', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`
  }

  const getStatusIcon = (status: UploadFile['status']) => {
    switch (status) {
      case 'success':
        return <CheckCircle className="h-5 w-5 text-green-500" />
      case 'error':
        return <AlertCircle className="h-5 w-5 text-red-500" />
      case 'uploading':
        return <RefreshCw className="h-5 w-5 text-blue-500 animate-spin" />
      default:
        return <File className="h-5 w-5 text-gray-500" />
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <UploadIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to upload audio files.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center space-x-2 mb-6">
        <UploadIcon className="h-6 w-6 text-blue-600" />
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Upload Audio Files
        </h1>
      </div>

      {/* Google Drive Folder Upload */}
      <div className="mb-6 p-4 bg-gray-50 dark:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600">
        <label className="block mb-2 font-medium text-gray-900 dark:text-gray-100">
          Paste Google Drive Folder ID:
        </label>

        <div className="flex space-x-2">
          <input
            type="text"
            value={gdriveFolderId}
            onChange={(e) => setGdriveFolderId(e.target.value)}
            placeholder="1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
            className="flex-1 px-3 py-2 border rounded-lg dark:bg-gray-800 dark:text-gray-100"
          />

          <button
            onClick={handleGDriveSubmit}
            disabled={isUploading || !gdriveFolderId}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            {isUploading ? 'Submitting...' : 'Submit Folder'}
          </button>
        </div>

        {gdriveUploadStatus.type && (
          <div
            className={`mt-3 p-3 rounded-lg text-sm ${
              gdriveUploadStatus.type === 'success'
                ? 'bg-green-100 text-green-800 border border-green-300'
                : 'bg-red-100 text-red-800 border border-red-300'
            }`}
          >
            {gdriveUploadStatus.message}
          </div>
        )}
      </div>

      {/* Drop Zone */}
      <div
        className={`relative border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
          dragActive
            ? 'border-blue-400 bg-blue-50 dark:bg-blue-900/10'
            : 'border-gray-300 dark:border-gray-600 hover:border-blue-400'
        }`}
        onDragEnter={handleDrag}
        onDragLeave={handleDrag}
        onDragOver={handleDrag}
        onDrop={handleDrop}
      >
        <UploadIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">
          Drop audio files here or click to browse
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          Supported formats: WAV, MP3, M4A, FLAC
        </p>

        <input
          type="file"
          multiple
          accept="audio/*,.wav,.mp3,.m4a,.flac"
          onChange={(e) => handleFileSelect(e.target.files)}
          className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
        />

        <button
          onClick={() => (document.querySelector('input[type="file"]') as HTMLInputElement)?.click()}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          Select Files
        </button>
      </div>

      {/* File List */}
      {files.length > 0 && (
        <div className="mt-8">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Files ({files.length})
            </h2>
            <div className="flex space-x-2">
              <button
                onClick={clearCompleted}
                className="px-3 py-1 text-sm bg-gray-600 text-white rounded hover:bg-gray-700"
              >
                Clear Completed
              </button>
              <button
                onClick={uploadFiles}
                disabled={isUploading || files.every((f) => f.status !== 'pending')}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                {isUploading ? 'Uploading...' : 'Upload All'}
              </button>
            </div>
          </div>

          <div className="space-y-2">
            {files.map((uploadFile) => (
              <div
                key={uploadFile.id}
                className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600"
              >
                <div className="flex items-center space-x-3 flex-1">
                  {getStatusIcon(uploadFile.status)}
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-gray-900 dark:text-gray-100 truncate">
                      {uploadFile.file.name}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      {formatFileSize(uploadFile.file.size)}
                      {uploadFile.error && (
                        <span className="text-red-600 dark:text-red-400 ml-2">
                          • {uploadFile.error}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <div className="flex items-center space-x-2">
                  <span
                    className={`text-sm font-medium ${
                      uploadFile.status === 'success'
                        ? 'text-green-600'
                        : uploadFile.status === 'error'
                        ? 'text-red-600'
                        : uploadFile.status === 'uploading'
                        ? 'text-blue-600'
                        : 'text-gray-600 dark:text-gray-400'
                    }`}
                  >
                    {uploadFile.status.charAt(0).toUpperCase() + uploadFile.status.slice(1)}
                  </span>

                  {uploadFile.status === 'pending' && (
                    <button
                      onClick={() => removeFile(uploadFile.id)}
                      className="p-1 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Upload Progress */}
      {isUploading && (
        <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-blue-700 dark:text-blue-300">
              Processing audio files...
            </span>
            <span className="text-sm text-blue-600 dark:text-blue-400">
              {uploadProgress}%
            </span>
          </div>
          <div className="w-full bg-blue-200 dark:bg-blue-800 rounded-full h-2">
            <div
              className="bg-blue-600 h-2 rounded-full transition-all duration-300"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
          <p className="text-xs text-blue-600 dark:text-blue-400 mt-2">
            Note: Processing may take up to 5 minutes depending on file size and quantity.
          </p>
        </div>
      )}

      {/* Instructions */}
      <div className="mt-8 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4">
        <h3 className="font-medium text-yellow-800 dark:text-yellow-200 mb-2">
          📝 Upload Instructions
        </h3>
        <ul className="text-sm text-yellow-700 dark:text-yellow-300 space-y-1">
          <li>• Audio files will be processed sequentially for transcription and memory extraction</li>
          <li>• Processing time varies based on audio length (roughly 3× duration + 60s)</li>
          <li>• Large files or multiple files may cause timeout errors</li>
          <li>• Check the Conversations tab for processed results</li>
          <li>• Supported formats: WAV, MP3, M4A, FLAC</li>
        </ul>
      </div>

      {/* Obsidian Vault Import */}
      {isAdmin && (
        <div className="mt-8 p-6 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center space-x-2">
              <FolderPlus className="h-5 w-5 text-purple-600" />
              <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Obsidian Vault Import</h2>
            </div>
          </div>

          <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
            Upload a .zip of your Obsidian vault, then click Start to ingest notes into Neo4j. Progress will be shown below.
          </p>

          {/* Zip selector */}
          <div className="flex flex-wrap items-center gap-3">
            <label className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200 rounded-lg cursor-pointer hover:bg-gray-200 dark:hover:bg-gray-600">
              <Archive className="inline h-4 w-4 mr-2" /> Select Vault .zip
              <input type="file" accept=".zip" className="hidden" onChange={(e) => handleObsidianZipSelect(e.target.files)} />
            </label>

            <button
              onClick={uploadObsidianZip}
              disabled={!obsidianZip}
              className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50"
            >
              Upload Zip
            </button>

            <button
              onClick={startObsidianIngestion}
              disabled={!obsidianJobId || obsidianPolling}
              className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 flex items-center"
            >
              <PlayCircle className="h-4 w-4 mr-2" /> Start Ingestion
            </button>
          </div>

          {obsidianZip && (
            <div className="mt-2 text-sm text-gray-700 dark:text-gray-300">Selected: {obsidianZip.name}</div>
          )}

          {/* Upload progress */}
          {obsidianZip && obsidianUploadProgress > 0 && obsidianUploadProgress < 100 && (
            <div className="mt-4">
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm text-purple-700 dark:text-purple-300">Uploading vault...</span>
                <span className="text-sm text-purple-600 dark:text-purple-400">{obsidianUploadProgress}%</span>
              </div>
              <div className="w-full bg-purple-200 dark:bg-purple-900 rounded-full h-2">
                <div className="bg-purple-600 h-2 rounded-full transition-all duration-300" style={{ width: `${obsidianUploadProgress}%` }} />
              </div>
            </div>
          )}

          {/* Ingestion status */}
          {obsidianStatus && (
            <div className="mt-4 p-3 bg-gray-50 dark:bg-gray-700 rounded-md border border-gray-200 dark:border-gray-600">
              <div className="flex justify-between text-sm">
                <div>Status: <span className="font-medium">{obsidianStatus.status}</span></div>
                <div>Processed: {obsidianStatus.processed}/{obsidianStatus.total} ({obsidianStatus.percent}%)</div>
              </div>
              <div className="w-full bg-blue-200 dark:bg-blue-900 rounded-full h-2 mt-2">
                <div className="bg-blue-600 h-2 rounded-full transition-all duration-300" style={{ width: `${obsidianStatus.percent || 0}%` }} />
              </div>
              {obsidianStatus.last_file && (
                <div className="mt-2 text-xs text-gray-600 dark:text-gray-300 truncate">Last file: {obsidianStatus.last_file}</div>
              )}
              {obsidianStatus.errors?.length > 0 && (
                <div className="mt-2 text-xs text-red-600 dark:text-red-400">
                  <div className="mb-1">Errors: {obsidianStatus.errors.length}</div>
                  <details className="mt-1">
                    <summary className="cursor-pointer text-red-700 dark:text-red-300">View first 10 errors</summary>
                    <ul className="mt-2 space-y-1">
                      {(obsidianStatus.errors.slice(0, 10) as string[]).map((e: string, idx: number) => (
                        <li key={idx} className="break-all">{e}</li>
                      ))}
                    </ul>
                  </details>
                </div>
              )}
            </div>
          )}

          {/* Messages */}
          {obsidianMessage && (
            <div className="mt-3 p-2 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded text-sm text-green-700 dark:text-green-300">{obsidianMessage}</div>
          )}
          {obsidianError && (
            <div className="mt-3 p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded text-sm text-red-700 dark:text-red-300">{obsidianError}</div>
          )}
        </div>
      )}
    </div>
  )
}
