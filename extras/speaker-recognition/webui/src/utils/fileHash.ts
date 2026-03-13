import SparkMD5 from 'spark-md5'

export async function calculateFileHash(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const spark = new SparkMD5.ArrayBuffer()
    const fileReader = new FileReader()
    const chunkSize = 2097152 // 2MB chunks
    let currentChunk = 0
    const chunks = Math.ceil(file.size / chunkSize)

    fileReader.onload = function(e) {
      if (e.target?.result) {
        spark.append(e.target.result as ArrayBuffer)
        currentChunk++

        if (currentChunk < chunks) {
          loadNext()
        } else {
          const hash = spark.end()
          resolve(hash)
        }
      }
    }

    fileReader.onerror = function() {
      reject(new Error('File reading failed'))
    }

    function loadNext() {
      const start = currentChunk * chunkSize
      const end = Math.min(start + chunkSize, file.size)
      fileReader.readAsArrayBuffer(file.slice(start, end))
    }

    loadNext()
  })
}

export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 Bytes'

  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))

  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

export function isAudioFile(file: File): boolean {
  const audioTypes = [
    'audio/wav',
    'audio/wave',
    'audio/x-wav',
    'audio/flac',
    'audio/mpeg',
    'audio/mp3',
    'audio/m4a',
    'audio/mp4',
    'audio/ogg',
    'audio/webm'
  ]

  return audioTypes.includes(file.type) ||
         /\.(wav|flac|mp3|m4a|ogg)$/i.test(file.name)
}
