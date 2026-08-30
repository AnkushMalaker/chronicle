// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TimelineEpisode } from '../../services/api'
import EpisodeLabelBar from './EpisodeLabelBar'

afterEach(cleanup)

const episode = {
  episode_id: 'episode-1',
  started_at: '2026-02-15T19:27:19.000Z',
  ended_at: '2026-02-15T19:28:47.000Z',
  kind: 'spoken_activity',
} as TimelineEpisode

describe('EpisodeLabelBar', () => {
  it('commits a semantic episode type correction through the adjustment path', () => {
    const onAdjust = vi.fn()
    render(
      <EpisodeLabelBar
        episode={episode}
        selected={false}
        onToggleSelected={vi.fn()}
        onAdjust={onAdjust}
        onSplit={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    const type = screen.getByRole('combobox', { name: 'Episode type' })
    expect(type).toHaveValue('spoken_activity')

    fireEvent.change(type, { target: { value: 'media' } })
    fireEvent.blur(type)

    expect(onAdjust).toHaveBeenCalledWith({ kind: 'media' })
  })

  it('makes media reference-only unless the person explicitly remembers its content', () => {
    const onAdjust = vi.fn()
    render(
      <EpisodeLabelBar
        episode={{ ...episode, kind: 'media', memory_policy: 'auto' }}
        selected={false}
        onToggleSelected={vi.fn()}
        onAdjust={onAdjust}
        onSplit={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('checkbox', { name: 'Remember content' }))

    expect(onAdjust).toHaveBeenCalledWith({ memory_policy: 'remember' })
  })
})
