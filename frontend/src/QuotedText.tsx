/** Render quoted source text as emphasis, never as model-supplied HTML. */
export function QuotedText({ text, evidence }: { text?: string; evidence?: string }) {
  const value = text || ''
  const ranges: [number, number][] = []
  for (const match of value.matchAll(/「[^」]+」|“[^”]+”|《[^》]+》|\*\*[^*]+\*\*/g)) {
    ranges.push([match.index!, match.index! + match[0].length])
  }
  if (evidence) {
    let offset = 0
    while ((offset = value.indexOf(evidence, offset)) >= 0) {
      ranges.push([offset, offset + evidence.length])
      offset += evidence.length
    }
  }
  const merged: [number, number][] = []
  for (const range of ranges.sort((a, b) => a[0] - b[0])) {
    const previous = merged[merged.length - 1]
    if (previous && range[0] <= previous[1]) previous[1] = Math.max(previous[1], range[1])
    else merged.push([...range])
  }
  const parts = []
  let cursor = 0
  for (const [start, end] of merged) {
    parts.push(value.slice(cursor, start))
    parts.push(<strong key={start}>{value.slice(start, end).replace(/^\*\*|\*\*$/g, '')}</strong>)
    cursor = end
  }
  parts.push(value.slice(cursor))
  return <>{parts}</>
}
