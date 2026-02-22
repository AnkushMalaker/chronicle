import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { motion, AnimatePresence, PanInfo } from 'framer-motion'
import { X, Check, Heart, HeartCrack, Pencil, Play, Pause } from 'lucide-react'
import { api, BACKEND_URL } from '../services/api'
import { getStorageKey } from '../utils/storage'

type DiffToken = { text: string; type: 'equal' | 'added' | 'removed' }

/** Simple word-level diff using LCS to highlight changes. */
function computeWordDiff(original: string, corrected: string): { originalTokens: DiffToken[]; correctedTokens: DiffToken[] } {
  const a = original.split(/(\s+)/)
  const b = corrected.split(/(\s+)/)

  // Build LCS table
  const m = a.length, n = b.length
  const dp: number[][] = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0))
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1])
    }
  }

  // Backtrack to get diff
  const originalTokens: DiffToken[] = []
  const correctedTokens: DiffToken[] = []
  let i = m, j = n
  const origReverse: DiffToken[] = []
  const corrReverse: DiffToken[] = []

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      origReverse.push({ text: a[i - 1], type: 'equal' })
      corrReverse.push({ text: b[j - 1], type: 'equal' })
      i--; j--
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      corrReverse.push({ text: b[j - 1], type: 'added' })
      j--
    } else {
      origReverse.push({ text: a[i - 1], type: 'removed' })
      i--
    }
  }

  originalTokens.push(...origReverse.reverse())
  correctedTokens.push(...corrReverse.reverse())
  return { originalTokens, correctedTokens }
}

const AUTO_SHOW_KEY = 'userloop-auto-show'

interface Suggestion {
  id: string
  annotation_type: string
  conversation_id: string
  segment_index: number | null
  original_text: string
  corrected_text: string
  created_at: string
  conversation_title: string | null
  transcript_snippet: string | null
  segment_start: number | null
  segment_end: number | null
}

/** Read auto-show preference from localStorage (default: false). */
function getAutoShow(): boolean {
  try {
    return localStorage.getItem(AUTO_SHOW_KEY) === 'true'
  } catch {
    return false
  }
}

export default function UserLoopModal() {
  const [isOpen, setIsOpen] = useState(false)
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [direction, setDirection] = useState(0)
  const [isAnimating, setIsAnimating] = useState(false)
  const [particles, setParticles] = useState<{ id: number; x: number; y: number; type: 'heart' | 'heart-break' }[]>([])
  const [isEditing, setIsEditing] = useState(false)
  const [editText, setEditText] = useState('')
  const [isPlaying, setIsPlaying] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const stopAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setIsPlaying(false)
  }, [])

  const fetchSuggestions = useCallback(async (): Promise<Suggestion[]> => {
    try {
      const response = await api.get('/api/annotations/suggestions', { params: { limit: 20 } })
      const data = response.data
      if (Array.isArray(data) && data.length > 0) {
        setSuggestions(data)
        setCurrentIndex(0)
        return data
      }
      return []
    } catch {
      return []
    }
  }, [])

  // Auto-show: only poll & auto-open when the user has opted in via localStorage
  useEffect(() => {
    if (!getAutoShow()) return

    const check = async () => {
      const data = await fetchSuggestions()
      if (data.length > 0) setIsOpen(true)
    }
    check()
    const interval = setInterval(check, 60000)
    return () => clearInterval(interval)
  }, [fetchSuggestions])

  // Explicit trigger from Fine-tuning page (always works regardless of auto-show)
  useEffect(() => {
    const handler = () => {
      fetchSuggestions().then(data => {
        if (data.length > 0) setIsOpen(true)
      })
    }
    window.addEventListener('open-swipe-ui', handler)
    return () => window.removeEventListener('open-swipe-ui', handler)
  }, [fetchSuggestions])

  // Stop audio on unmount
  useEffect(() => {
    return () => { stopAudio() }
  }, [stopAudio])

  // Stop audio when card changes
  useEffect(() => {
    stopAudio()
  }, [currentIndex, stopAudio])

  // Clean up particles
  useEffect(() => {
    const timer = setTimeout(() => setParticles([]), 1000)
    return () => clearTimeout(timer)
  }, [particles])

  // Close modal when no suggestions left
  useEffect(() => {
    if (suggestions.length === 0 && isOpen) {
      stopAudio()
      setIsOpen(false)
    }
  }, [suggestions.length, isOpen, stopAudio])

  // Keyboard shortcuts
  useEffect(() => {
    if (!isOpen || suggestions.length === 0) return

    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't capture keys when editing (textarea handles its own keys)
      if (isEditing) return

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault()
          handleSkip()
          break
        case 'ArrowUp':
          e.preventDefault()
          setEditText(suggestions[currentIndex]?.corrected_text || '')
          setIsEditing(true)
          break
        case 'ArrowLeft':
          e.preventDefault()
          handleAction('reject', -1)
          break
        case 'ArrowRight':
          e.preventDefault()
          handleAction('accept', 1)
          break
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, suggestions, currentIndex, isEditing, isAnimating])

  const createParticles = (type: 'heart' | 'heart-break') => {
    setParticles(
      Array.from({ length: 8 }, (_, i) => ({
        id: Date.now() + i,
        x: Math.random() * 400 - 200,
        y: Math.random() * 200 - 100,
        type,
      }))
    )
  }

  const handleSkip = () => {
    if (isAnimating) return
    setIsEditing(false)
    stopAudio()
    if (currentIndex < suggestions.length - 1) {
      setCurrentIndex(prev => prev + 1)
    } else {
      setIsOpen(false)
      setSuggestions([])
    }
  }

  const handleEditSave = async () => {
    const suggestion = suggestions[currentIndex]
    if (!suggestion) return
    try {
      await api.patch(`/api/annotations/${suggestion.id}`, { corrected_text: editText })
      // Update local state so diff re-renders with new text
      setSuggestions(prev => prev.map((s, i) => i === currentIndex ? { ...s, corrected_text: editText } : s))
    } catch (error) {
      console.error('Failed to save edit:', error)
    }
    setIsEditing(false)
  }

  const togglePlay = () => {
    const s = suggestions[currentIndex]
    if (!s || s.segment_start == null || s.segment_end == null) return

    if (isPlaying && audioRef.current) {
      stopAudio()
      return
    }

    const token = localStorage.getItem(getStorageKey('token')) || ''
    const url = `${BACKEND_URL}/api/audio/chunks/${s.conversation_id}?start_time=${s.segment_start}&end_time=${s.segment_end}&token=${token}`
    const audio = new Audio(url)
    audioRef.current = audio
    audio.addEventListener('ended', () => setIsPlaying(false))
    audio.play().then(() => setIsPlaying(true)).catch(() => setIsPlaying(false))
  }

  const handleAction = async (action: 'accept' | 'reject', swipeDirection: number) => {
    const suggestion = suggestions[currentIndex]
    if (!suggestion || isAnimating) return

    setIsAnimating(true)
    setDirection(swipeDirection)
    createParticles(action === 'accept' ? 'heart' : 'heart-break')

    try {
      const status = action === 'accept' ? 'accepted' : 'rejected'
      await api.patch(`/api/annotations/${suggestion.id}/status`, null, {
        params: { status },
      })
    } catch (error) {
      console.error(`Failed to ${action} suggestion:`, error)
    }

    setTimeout(() => {
      if (currentIndex < suggestions.length - 1) {
        setCurrentIndex(prev => prev + 1)
      } else {
        setIsOpen(false)
        setSuggestions([])
      }
      setIsAnimating(false)
      setDirection(0)
    }, 400)
  }

  const onPanEnd = (_event: MouseEvent | TouchEvent | PointerEvent, info: PanInfo) => {
    if (isAnimating) return
    const threshold = 100
    if (info.offset.x > threshold) {
      handleAction('accept', 1)
    } else if (info.offset.x < -threshold) {
      handleAction('reject', -1)
    }
  }

  const diff = useMemo(() => {
    if (!isOpen || suggestions.length === 0) return null
    const current = suggestions[currentIndex]
    return computeWordDiff(current.original_text, current.corrected_text)
  }, [isOpen, suggestions, currentIndex])

  if (!isOpen || suggestions.length === 0) return null

  const current = suggestions[currentIndex]

  const cardVariants = {
    enter: (dir: number) => ({ x: dir > 0 ? 1000 : -1000, opacity: 0, scale: 0.8 }),
    center: { zIndex: 1, x: 0, opacity: 1, scale: 1 },
    exit: (dir: number) => ({ zIndex: 0, x: dir > 0 ? 1000 : -1000, opacity: 0, scale: 0.8 }),
  }

  return (
    <AnimatePresence mode="wait">
      {isOpen && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3 }}
        >
          <div className="relative w-full max-w-md mx-4">
            {/* Particles */}
            <AnimatePresence mode="popLayout">
              {particles.map(p => (
                <motion.div
                  key={p.id}
                  className="absolute top-1/2 left-1/2 pointer-events-none"
                  initial={{ x: p.x, y: p.y, scale: 0, opacity: 1 }}
                  animate={{ y: p.y - 200, scale: [0, 1.5, 1], opacity: [1, 1, 0] }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.8, ease: 'easeOut' }}
                >
                  {p.type === 'heart' ? (
                    <Heart className="h-16 w-16 text-pink-500 fill-pink-500" />
                  ) : (
                    <HeartCrack className="h-16 w-16 text-red-500" />
                  )}
                </motion.div>
              ))}
            </AnimatePresence>

            {/* Card */}
            <motion.div
              className="relative bg-white dark:bg-gray-800 rounded-3xl shadow-2xl p-6 cursor-grab active:cursor-grabbing"
              custom={direction}
              variants={cardVariants}
              initial="enter"
              animate="center"
              exit="exit"
              transition={{ x: { type: 'spring', stiffness: 300, damping: 30 }, opacity: { duration: 0.2 } }}
              drag="x"
              dragConstraints={{ left: 0, right: 0 }}
              dragElastic={0.1}
              onDragEnd={onPanEnd}
              whileDrag={{ scale: 1.05 }}
            >
              {/* Status Overlays */}
              <AnimatePresence mode="popLayout">
                {direction > 0 && (
                  <motion.div
                    className="absolute top-6 right-8 text-5xl font-bold text-green-500 border-4 border-green-500 rounded-2xl px-4 py-2"
                    initial={{ scale: 0, opacity: 0, rotate: -15 }}
                    animate={{ scale: 1, opacity: 1, rotate: -15 }}
                    exit={{ scale: 1.2, opacity: 0 }}
                  >
                    GOOD
                  </motion.div>
                )}
                {direction < 0 && (
                  <motion.div
                    className="absolute top-6 left-8 text-5xl font-bold text-red-500 border-4 border-red-500 rounded-2xl px-4 py-2"
                    initial={{ scale: 0, opacity: 0, rotate: 15 }}
                    animate={{ scale: 1, opacity: 1, rotate: 15 }}
                    exit={{ scale: 1.2, opacity: 0 }}
                  >
                    NOPE
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Content */}
              <motion.div
                className="text-center"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1, duration: 0.4 }}
              >
                <div className="mb-2 text-2xl font-semibold text-gray-900 dark:text-gray-100">
                  Review Suggestion
                </div>

                {current.conversation_title && (
                  <div className="mb-3 text-sm text-gray-500 dark:text-gray-400">
                    {current.conversation_title}
                  </div>
                )}

                {/* Original vs corrected with diff highlighting */}
                <div className="mb-4 text-left space-y-3">
                  <div>
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1">Original</div>
                    <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3 text-sm text-gray-800 dark:text-gray-200">
                      {diff?.originalTokens.map((t, i) =>
                        t.type === 'removed' ? (
                          <span key={i} className="bg-red-200 dark:bg-red-700/50 text-red-800 dark:text-red-200 line-through rounded px-0.5">{t.text}</span>
                        ) : (
                          <span key={i}>{t.text}</span>
                        )
                      )}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1 flex items-center gap-1.5">
                      {isEditing ? (
                        <>
                          <Pencil className="h-3 w-3 text-amber-500" />
                          <span className="text-amber-600 dark:text-amber-400">Editing...</span>
                        </>
                      ) : (
                        'Suggested'
                      )}
                    </div>
                    {isEditing ? (
                      <div>
                        <textarea
                          value={editText}
                          onChange={(e) => setEditText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault()
                              handleEditSave()
                            } else if (e.key === 'Escape') {
                              e.preventDefault()
                              setIsEditing(false)
                            }
                          }}
                          className="w-full bg-amber-50 dark:bg-amber-900/20 border-2 border-amber-400 dark:border-amber-600 rounded-lg p-3 text-sm text-gray-800 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-amber-400 resize-none"
                          rows={3}
                          autoFocus
                        />
                        <div className="flex items-center justify-between mt-1.5 text-xs text-gray-400">
                          <span>Enter to save &middot; Shift+Enter for newline &middot; Esc to cancel</span>
                        </div>
                      </div>
                    ) : (
                      <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg p-3 text-sm text-gray-800 dark:text-gray-200">
                        {diff?.correctedTokens.map((t, i) =>
                          t.type === 'added' ? (
                            <span key={i} className="bg-green-200 dark:bg-green-700/50 text-green-800 dark:text-green-200 font-medium rounded px-0.5">{t.text}</span>
                          ) : (
                            <span key={i}>{t.text}</span>
                          )
                        )}
                      </div>
                    )}
                  </div>
                </div>

                {/* Context snippet */}
                {current.transcript_snippet && (
                  <div className="mb-4 text-left">
                    <div className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-1 flex items-center gap-2">
                      <span>Context</span>
                      {current.segment_start != null && current.segment_end != null && (
                        <button
                          onClick={togglePlay}
                          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 hover:bg-blue-200 dark:hover:bg-blue-900/50 transition-colors"
                          title={isPlaying ? 'Pause audio' : 'Play segment audio'}
                        >
                          {isPlaying ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                          <span className="text-[10px]">{isPlaying ? 'Pause' : 'Play'}</span>
                        </button>
                      )}
                    </div>
                    <pre className="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 text-xs text-gray-600 dark:text-gray-400 whitespace-pre-wrap font-mono">
                      {current.transcript_snippet}
                    </pre>
                  </div>
                )}

                {/* Counter */}
                <div className="text-sm text-gray-500 dark:text-gray-400 mb-3">
                  {currentIndex + 1} / {suggestions.length}
                </div>

                {/* Instructions */}
                <div className="text-xs text-gray-400 dark:text-gray-500 space-y-1">
                  <div className="flex items-center justify-center gap-2">
                    <motion.span animate={{ x: [0, -10, 0] }} transition={{ duration: 1.5, repeat: Infinity }}>
                      &larr;
                    </motion.span>
                    Swipe or arrow keys
                    <motion.span animate={{ x: [0, 10, 0] }} transition={{ duration: 1.5, repeat: Infinity }}>
                      &rarr;
                    </motion.span>
                  </div>
                  <div className="flex items-center justify-center gap-3 font-mono">
                    <span>&larr; reject</span>
                    <span>&rarr; accept</span>
                    <span>&darr; skip</span>
                    <span>&uarr; edit</span>
                  </div>
                </div>
              </motion.div>

              {/* Close Button */}
              <motion.button
                onClick={() => { stopAudio(); setIsOpen(false) }}
                className="absolute top-4 right-4 p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                whileHover={{ scale: 1.1, rotate: 90 }}
                whileTap={{ scale: 0.9 }}
              >
                <X className="h-6 w-6" />
              </motion.button>
            </motion.div>

            {/* Control Buttons */}
            <motion.div
              className="flex justify-center items-center gap-8 mt-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.4, duration: 0.4 }}
            >
              <motion.button
                onClick={() => handleAction('reject', -1)}
                className="w-16 h-16 rounded-full bg-white dark:bg-gray-800 border-2 border-red-500 text-red-500 flex items-center justify-center shadow-lg"
                whileHover={{ scale: 1.1 }}
                whileTap={{ scale: 0.9 }}
              >
                <X className="h-8 w-8" />
              </motion.button>
              <motion.button
                onClick={() => handleAction('accept', 1)}
                className="w-16 h-16 rounded-full bg-white dark:bg-gray-800 border-2 border-green-500 text-green-500 flex items-center justify-center shadow-lg"
                whileHover={{ scale: 1.1 }}
                whileTap={{ scale: 0.9 }}
              >
                <Check className="h-8 w-8" />
              </motion.button>
            </motion.div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
