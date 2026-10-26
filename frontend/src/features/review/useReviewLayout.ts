import { useEffect, useRef, useState } from 'react'
import type { Preview } from '../../api'

type LayoutOptions = {
  pendingId: string
  currentPage: Preview['pages'][number] | undefined
  zoom: number
  focused: string
  loaded: string
  imageKey: string
  selected: Set<string>
}

export function useReviewLayout({
  pendingId,
  currentPage,
  zoom,
  focused,
  loaded,
  imageKey,
  selected,
}: LayoutOptions) {
  const board = useRef<HTMLDivElement>(null)
  const cards = useRef<HTMLDivElement>(null)
  const paper = useRef<HTMLDivElement>(null)
  const [paperWidth, setPaperWidth] = useState(0)
  const [arrow, setArrow] = useState('')
  useEffect(() => {
    if (!currentPage) return
    // Enter the review workspace with the whole page in the viewport. Further
    // card navigation never scrolls the document or the preview pane.
    const frame = requestAnimationFrame(() =>
      board.current?.parentElement?.scrollIntoView({ block: 'start' }),
    )
    return () => cancelAnimationFrame(frame)
  }, [pendingId])
  useEffect(() => {
    const pane = paper.current
    if (!pane || !currentPage) return
    const resize = () => {
      const css = getComputedStyle(pane)
      const width = pane.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight)
      const height = pane.clientHeight - parseFloat(css.paddingTop) - parseFloat(css.paddingBottom)
      // The initial 95% view fits the page. User zoom is applied afterwards,
      // never clamped back to fit: increasing the control really enlarges it.
      setPaperWidth(
        Math.max(
          0,
          (Math.min(width, (height * currentPage.width) / currentPage.height) * zoom) / 95,
        ),
      )
    }
    const observer = new ResizeObserver(resize)
    observer.observe(pane)
    resize()
    return () => observer.disconnect()
  }, [currentPage?.width, currentPage?.height, zoom])
  useEffect(() => {
    const root = board.current
    if (!root) return
    function draw() {
      const card = cards.current?.querySelector<HTMLElement>(`[data-card="${focused}"]`)
      const mark = paper.current?.querySelector<HTMLElement>(`[data-highlight="${focused}"]`)
      if (!card || !mark || loaded !== imageKey || !cards.current || !paper.current) {
        setArrow('')
        return
      }
      const r = root!.getBoundingClientRect(),
        a = card.getBoundingClientRect(),
        b = mark.getBoundingClientRect(),
        c = cards.current.getBoundingClientRect(),
        d = paper.current.getBoundingClientRect()
      if (
        a.bottom < c.top ||
        a.top > c.bottom ||
        b.bottom < d.top ||
        b.top > d.bottom ||
        b.left > d.right ||
        b.right < d.left
      ) {
        setArrow('')
        return
      }
      const x = a.right - r.left,
        y = Math.max(c.top + 8, Math.min(c.bottom - 8, a.top + a.height / 2)) - r.top
      const tx = Math.max(d.left, b.left) - r.left,
        ty = b.top + b.height / 2 - r.top
      setArrow(`M ${x} ${y} C ${x + 28} ${y}, ${tx - 28} ${ty}, ${tx} ${ty}`)
    }
    draw()
    const observer = new ResizeObserver(draw)
    observer.observe(root)
    if (paper.current) observer.observe(paper.current)
    root.addEventListener('scroll', draw, true)
    window.addEventListener('resize', draw)
    return () => {
      observer.disconnect()
      root.removeEventListener('scroll', draw, true)
      window.removeEventListener('resize', draw)
    }
  }, [focused, paperWidth, loaded, imageKey, selected])
  return { board, cards, paper, paperWidth, arrow }
}

export function scrollInside(container: HTMLElement | null, selector: string) {
  const target = container?.querySelector<HTMLElement>(selector)
  if (!container || !target) return
  const outer = container.getBoundingClientRect(),
    inner = target.getBoundingClientRect()
  if (inner.top < outer.top + 24 || inner.bottom > outer.bottom - 24) {
    container.scrollTo({
      top: Math.max(0, container.scrollTop + inner.top - outer.top - 40),
      behavior: 'smooth',
    })
  }
}
