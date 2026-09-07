// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { computeWordDiff, WordDiff } from './WordDiff'

afterEach(cleanup)

describe('computeWordDiff', () => {
  it('preserves text while isolating changed words on both sides', () => {
    const result = computeWordDiff(
      'Ankush likes tea.\nMeet at 3.',
      'Ankush likes coffee.\nMeet at 4.',
    )

    expect(result.beforeTokens.map(token => token.text).join('')).toBe('Ankush likes tea.\nMeet at 3.')
    expect(result.afterTokens.map(token => token.text).join('')).toBe('Ankush likes coffee.\nMeet at 4.')
    expect(result.beforeTokens.filter(token => token.type === 'removed').map(token => token.text)).toEqual(['tea.', '3.'])
    expect(result.afterTokens.filter(token => token.type === 'added').map(token => token.text)).toEqual(['coffee.', '4.'])
  })

  it('renders semantic highlights with non-color descriptions', () => {
    const { rerender } = render(<WordDiff tokens={computeWordDiff('old text', 'new text').beforeTokens} />)
    expect(screen.getByTitle('Removed text')).toHaveTextContent('old')

    rerender(<WordDiff tokens={computeWordDiff('old text', 'new text').afterTokens} />)
    expect(screen.getByTitle('Added text')).toHaveTextContent('new')
  })
})
