const NUMBERED_UNKNOWN_SPEAKER = /^unknown speaker\s+(\d+)$/i

export function nextUnknownSpeakerLabel(speakerNames: string[]): string {
  const highestNumber = speakerNames.reduce((highest, name) => {
    const match = NUMBERED_UNKNOWN_SPEAKER.exec(name.trim())
    return match ? Math.max(highest, Number(match[1])) : highest
  }, 0)

  return `Unknown Speaker ${highestNumber + 1}`
}
