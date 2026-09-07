import { describe, expect, it } from 'vitest'
import { episodeDisplayTitle, isSemanticMemoryEligible } from './episodePresentation'

describe('episodeDisplayTitle', () => {
  it('presents media as observed content rather than the user discussing it', () => {
    expect(episodeDisplayTitle({ kind: 'media', title: 'Discussing a deal and doubts about fishing' }))
      .toBe('Media: a deal and doubts about fishing')
    expect(episodeDisplayTitle({ kind: 'media', title: 'Watching commentary about OpenClaw' }))
      .toBe('Media: commentary about OpenClaw')
  })

  it('does not rewrite ordinary episode titles', () => {
    expect(episodeDisplayTitle({ kind: 'technical_work', title: 'Planning Queen3ASR integration' }))
      .toBe('Planning Queen3ASR integration')
  })
})

describe('isSemanticMemoryEligible', () => {
  it('keeps automatic media reference-only while remembering ordinary activity', () => {
    expect(isSemanticMemoryEligible({ kind: 'media', memory_policy: 'auto' })).toBe(false)
    expect(isSemanticMemoryEligible({ kind: 'conversation', memory_policy: 'auto' })).toBe(true)
  })

  it('honors explicit remember and reference choices', () => {
    expect(isSemanticMemoryEligible({ kind: 'media', memory_policy: 'remember' })).toBe(true)
    expect(isSemanticMemoryEligible({ kind: 'conversation', memory_policy: 'reference' })).toBe(false)
  })
})
