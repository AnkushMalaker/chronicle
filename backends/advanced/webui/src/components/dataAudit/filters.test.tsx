// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { speakersFilter, UNKNOWN_SPEAKERS_FILTER_KEY } from './filters'

afterEach(cleanup)

describe('speaker filter', () => {
  it('renders one semantic unknown option instead of numbered local labels', () => {
    const Editor = speakersFilter.Editor!
    render(<Editor value={{}} onChange={vi.fn()} ctx={{ speakers: ['Ankush'], datasets: [] }} />)

    expect(screen.getByRole('button', { name: 'Unknown speakers' })).toBeInTheDocument()
    expect(screen.queryByText('Unknown Speaker 1')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ankush' })).toBeInTheDocument()
  })

  it('serializes unknown tri-state separately from named identities', () => {
    expect(
      speakersFilter.toParams({
        [UNKNOWN_SPEAKERS_FILTER_KEY]: 'include',
        Ankush: 'exclude',
      })
    ).toEqual({
      include_speakers: [],
      exclude_speakers: ['Ankush'],
      unknown_speakers: 'include',
    })
  })

  it('cycles the generic unknown control through include and exclude', () => {
    const onChange = vi.fn()
    const Editor = speakersFilter.Editor!
    const { rerender } = render(
      <Editor value={{}} onChange={onChange} ctx={{ speakers: [], datasets: [] }} />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Unknown speakers' }))
    expect(onChange).toHaveBeenLastCalledWith({ [UNKNOWN_SPEAKERS_FILTER_KEY]: 'include' })

    rerender(
      <Editor value={{ [UNKNOWN_SPEAKERS_FILTER_KEY]: 'include' }} onChange={onChange} ctx={{ speakers: [], datasets: [] }} />
    )
    fireEvent.click(screen.getByRole('button', { name: 'Unknown speakers' }))
    expect(onChange).toHaveBeenLastCalledWith({ [UNKNOWN_SPEAKERS_FILTER_KEY]: 'exclude' })
  })
})
