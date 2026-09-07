export type DiffToken = {
  text: string
  type: 'equal' | 'added' | 'removed'
}

export type WordDiffResult = {
  beforeTokens: DiffToken[]
  afterTokens: DiffToken[]
}

const MAX_LCS_CELLS = 1_000_000

function coalesce(tokens: DiffToken[]): DiffToken[] {
  return tokens.reduce<DiffToken[]>((result, token) => {
    const previous = result[result.length - 1]
    if (previous?.type === token.type) previous.text += token.text
    else if (token.text) result.push({ ...token })
    return result
  }, [])
}

function boundedFallback(before: string[], after: string[]): WordDiffResult {
  let prefix = 0
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix++

  let suffix = 0
  while (
    suffix < before.length - prefix
    && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) suffix++

  const beforeSuffix = suffix ? before.slice(before.length - suffix) : []
  const afterSuffix = suffix ? after.slice(after.length - suffix) : []

  return {
    beforeTokens: coalesce([
      ...before.slice(0, prefix).map(text => ({ text, type: 'equal' as const })),
      ...before.slice(prefix, before.length - suffix).map(text => ({ text, type: 'removed' as const })),
      ...beforeSuffix.map(text => ({ text, type: 'equal' as const })),
    ]),
    afterTokens: coalesce([
      ...after.slice(0, prefix).map(text => ({ text, type: 'equal' as const })),
      ...after.slice(prefix, after.length - suffix).map(text => ({ text, type: 'added' as const })),
      ...afterSuffix.map(text => ({ text, type: 'equal' as const })),
    ]),
  }
}

/** Word-and-whitespace LCS diff with a bounded fallback for unusually large notes. */
export function computeWordDiff(beforeText: string, afterText: string): WordDiffResult {
  const before = beforeText.split(/(\s+)/)
  const after = afterText.split(/(\s+)/)
  const rows = before.length + 1
  const columns = after.length + 1

  if (rows * columns > MAX_LCS_CELLS) return boundedFallback(before, after)

  const table = new Uint32Array(rows * columns)
  const cell = (row: number, column: number) => row * columns + column

  for (let row = 1; row < rows; row++) {
    for (let column = 1; column < columns; column++) {
      table[cell(row, column)] = before[row - 1] === after[column - 1]
        ? table[cell(row - 1, column - 1)] + 1
        : Math.max(table[cell(row - 1, column)], table[cell(row, column - 1)])
    }
  }

  const beforeReverse: DiffToken[] = []
  const afterReverse: DiffToken[] = []
  let row = before.length
  let column = after.length

  while (row > 0 || column > 0) {
    if (row > 0 && column > 0 && before[row - 1] === after[column - 1]) {
      beforeReverse.push({ text: before[row - 1], type: 'equal' })
      afterReverse.push({ text: after[column - 1], type: 'equal' })
      row--
      column--
    } else if (column > 0 && (row === 0 || table[cell(row, column - 1)] >= table[cell(row - 1, column)])) {
      afterReverse.push({ text: after[column - 1], type: 'added' })
      column--
    } else {
      beforeReverse.push({ text: before[row - 1], type: 'removed' })
      row--
    }
  }

  return {
    beforeTokens: coalesce(beforeReverse.reverse()),
    afterTokens: coalesce(afterReverse.reverse()),
  }
}

export function WordDiff({ tokens }: { tokens: DiffToken[] }) {
  return (
    <>
      {tokens.map((token, index) => {
        if (token.type === 'equal') return <span key={index}>{token.text}</span>

        const changeClass = token.type === 'removed'
          ? 'bg-red-200 text-red-900 line-through decoration-red-600 dark:bg-red-900/60 dark:text-red-100 dark:decoration-red-400'
          : 'bg-green-200 text-green-900 font-medium dark:bg-green-900/60 dark:text-green-100'

        return (
          <mark
            key={index}
            className={`box-decoration-clone rounded-sm px-0.5 ${changeClass}`}
            title={token.type === 'removed' ? 'Removed text' : 'Added text'}
          >
            {token.text}
          </mark>
        )
      })}
    </>
  )
}
