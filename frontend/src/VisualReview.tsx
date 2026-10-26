import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Run, type Unfinished } from './api'
import './visual-review.css'
import { QuotedText } from './QuotedText'

export function VisualReview({ run }: { run: Run }) {
  const pending = run.pending!,
    request = pending.request,
    patches = request.patches || [],
    pages = request.preview?.pages || []
  const client = useQueryClient()
  const [selected, setSelected] = useState(
    () =>
      new Set(
        patches
          .filter((p) => !request.defaults?.excluded_ids?.includes(p.patch_id))
          .map((p) => p.patch_id),
      ),
  )
  const [page, setPage] = useState(patches[0]?.rects?.[0]?.page || 1)
  const [focused, setFocused] = useState(patches[0]?.patch_id || '')
  const [zoom, setZoom] = useState(95)
  const [paperWidth, setPaperWidth] = useState(0)
  const [loaded, setLoaded] = useState(''),
    [imageError, setImageError] = useState(false),
    [retry, setRetry] = useState(0)
  const [arrow, setArrow] = useState('')
  const board = useRef<HTMLDivElement>(null),
    cards = useRef<HTMLDivElement>(null),
    paper = useRef<HTMLDivElement>(null)
  const imageKey = `${page}-${retry}`
  const submit = useMutation({
    mutationFn: () =>
      api(`/runs/${run.id}/decisions`, {
        method: 'POST',
        body: JSON.stringify({
          pending_id: pending.id,
          version: run.version,
          payload: {
            excluded_ids: patches.filter((p) => !selected.has(p.patch_id)).map((p) => p.patch_id),
          },
        }),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['run', run.id] })
      client.invalidateQueries({ queryKey: ['events', run.id] })
    },
  })
  function toggle(id: string) {
    setSelected((old) => {
      const next = new Set(old)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  function focus(id: string, fromPaper = false) {
    const patch = patches.find((p) => p.patch_id === id)
    setFocused(id)
    if (fromPaper) scrollInside(cards.current, `[data-card="${id}"]`)
    else {
      setPage(patch?.rects?.[0]?.page || page)
      setImageError(false)
    }
  }
  const currentPage = pages.find((p) => p.number === page)
  useEffect(() => {
    if (!pages.length) return
    // Enter the review workspace with the whole page in the viewport. Further
    // card navigation never scrolls the document or the preview pane.
    const frame = requestAnimationFrame(() =>
      board.current?.parentElement?.scrollIntoView({ block: 'start' }),
    )
    return () => cancelAnimationFrame(frame)
  }, [pending.id])
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
  }, [focused, page, paperWidth, loaded, imageKey, selected])
  return (
    <section className="visual-review" aria-label="最终分页确认">
      <div className="review-heading">
        <div>
          <span className="eyebrow">FINAL REVIEW</span>
          <h2>对照原稿，决定每一处修改</h2>
        </div>
        <strong>
          已选 {selected.size} / {patches.length} 处
        </strong>
      </div>
      <p className="muted">
        默认全部勾选；取消勾选将保留原文。点击左侧修改或原稿红框可相互定位。确认后不再新增模型修改。
      </p>
      {(patches.length > 0 || pages.length > 0) && (
        <div className="review-workspace">
          <div className="review-board" ref={board}>
            <aside className="review-sidebar" aria-label="审阅边栏">
              <div className="review-sidebar-controls">
                {!!pages.length && (
                  <>
                    <div className="page-controls">
                      <label>
                        原稿第{' '}
                        <select
                          aria-label="原稿页码"
                          value={page}
                          onChange={(e) => {
                            setPage(Number(e.target.value))
                            setImageError(false)
                          }}
                        >
                          {pages.map((p) => (
                            <option key={p.number} value={p.number}>
                              {p.number}
                            </option>
                          ))}
                        </select>{' '}
                        / {pages.length} 页
                      </label>
                      <div className="button-row">
                        <button
                          aria-label="上一页"
                          disabled={page <= 1}
                          onClick={() => {
                            setPage(page - 1)
                            setImageError(false)
                          }}
                        >
                          上一页
                        </button>
                        <button
                          aria-label="下一页"
                          disabled={page >= pages.length}
                          onClick={() => {
                            setPage(page + 1)
                            setImageError(false)
                          }}
                        >
                          下一页
                        </button>
                      </div>
                    </div>
                    <div className="zoom-controls">
                      <button
                        aria-label="缩小原稿"
                        disabled={zoom <= 50}
                        onClick={() => setZoom((z) => Math.max(50, z - 5))}
                      >
                        −
                      </button>
                      <label>
                        缩放{' '}
                        <select
                          aria-label="预览缩放"
                          value={zoom}
                          onChange={(e) => setZoom(Number(e.target.value))}
                        >
                          {Array.from({ length: 31 }, (_, i) => 50 + i * 5).map((value) => (
                            <option key={value} value={value}>
                              {value}%
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        aria-label="放大原稿"
                        disabled={zoom >= 200}
                        onClick={() => setZoom((z) => Math.min(200, z + 5))}
                      >
                        ＋
                      </button>
                      <button
                        onClick={() => {
                          setZoom(95)
                          paper.current?.scrollTo(0, 0)
                        }}
                      >
                        整页
                      </button>
                    </div>
                  </>
                )}
                <div className="button-row">
                  <button
                    className="secondary"
                    disabled={submit.isPending}
                    onClick={() => setSelected(new Set(patches.map((p) => p.patch_id)))}
                  >
                    全选
                  </button>
                  <button
                    className="secondary"
                    disabled={submit.isPending}
                    onClick={() => setSelected(new Set())}
                  >
                    全不选
                  </button>
                </div>
              </div>
              <div className="review-cards" ref={cards} aria-label="修改清单">
                {!patches.length && (
                  <p className="review-empty">
                    当前没有可确认的自动修改。请检查下方人工处理事项；确认后保留原文，并将这些事项写入
                    Word 批注。
                  </p>
                )}
                {patches.map((patch, index) => (
                  <article
                    key={patch.patch_id}
                    data-card={patch.patch_id}
                    className={`review-card ${focused === patch.patch_id ? 'focused' : ''} ${selected.has(patch.patch_id) ? '' : 'omitted'}`}
                  >
                    <div className="review-card-top">
                      <label>
                        <input
                          type="checkbox"
                          aria-label={`采用第 ${index + 1} 项修改`}
                          checked={selected.has(patch.patch_id)}
                          disabled={submit.isPending}
                          onChange={() => toggle(patch.patch_id)}
                        />
                        <strong>修改 {String(index + 1).padStart(2, '0')}</strong>
                      </label>
                      <button className="text-button" onClick={() => focus(patch.patch_id)}>
                        定位第 {patch.rects?.[0]?.page} 页 ↗
                      </button>
                    </div>
                    <p className="patch-location">{patch.position}</p>
                    <button
                      className="review-card-content"
                      onClick={() => focus(patch.patch_id)}
                      aria-label={`查看第 ${index + 1} 项原文`}
                    >
                      <dl>
                        <dt>原文</dt>
                        <dd className="original-quote">{patch.before}</dd>
                        <dt>修改为</dt>
                        <dd>{patch.replace || '（删除）'}</dd>
                        <dt>
                          <strong>原因</strong>
                        </dt>
                        <dd className="muted">{patch.rationale || '未提供原因，请谨慎核对'}</dd>
                      </dl>
                    </button>
                    {!selected.has(patch.patch_id) && (
                      <small className="kept-original">已取消 · 将保留原文</small>
                    )}
                    {patch.review_note && <p className="muted">{patch.review_note}</p>}
                    {patch.params?.source_verification && (
                      <p className="muted">
                        {patch.params.source_verification.verification}{' '}
                        {patch.params.source_verification.source_url?.startsWith(
                          'https://doi.org/',
                        ) && (
                          <a
                            href={patch.params.source_verification.source_url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            核对文献来源 ↗
                          </a>
                        )}
                      </p>
                    )}
                  </article>
                ))}
              </div>
            </aside>
            <div className="review-paper-scroll" ref={paper} aria-label="原论文分页预览">
              {request.preview?.error && (
                <div className="error" role="alert">
                  {request.preview.error} 为避免误改，无法定位的修改已禁止写入。
                </div>
              )}
              {currentPage && (
                <div
                  className="review-paper"
                  style={{
                    width: paperWidth,
                    aspectRatio: `${currentPage.width} / ${currentPage.height}`,
                  }}
                >
                  <img
                    key={imageKey}
                    alt={`原论文第 ${page} 页`}
                    src={`/api/v1/runs/${run.id}/preview/${page}?fingerprint=${encodeURIComponent(request.preview!.fingerprint)}&retry=${retry}`}
                    onLoad={() => {
                      setLoaded(imageKey)
                      setImageError(false)
                    }}
                    onError={() => setImageError(true)}
                  />
                  {loaded === imageKey &&
                    !imageError &&
                    patches.flatMap((patch, index) =>
                      (patch.rects || [])
                        .filter((rect) => rect.page === page)
                        .map((rect, ri) => (
                          <button
                            key={`${patch.patch_id}-${ri}`}
                            data-highlight={patch.patch_id}
                            aria-label={`原文修改 ${index + 1}${ri ? ` 区域 ${ri + 1}` : ''}`}
                            title={`修改 ${index + 1}：${patch.before}`}
                            onClick={() => focus(patch.patch_id, true)}
                            className={`review-highlight ${focused === patch.patch_id ? 'focused' : ''} ${selected.has(patch.patch_id) ? '' : 'omitted'}`}
                            style={{
                              left: `${rect.x * 100}%`,
                              top: `${rect.y * 100}%`,
                              width: `${rect.width * 100}%`,
                              height: `${rect.height * 100}%`,
                            }}
                          >
                            {ri === 0 && <span>{index + 1}</span>}
                          </button>
                        )),
                    )}
                </div>
              )}
              {currentPage && loaded !== imageKey && !imageError && (
                <p role="status">正在加载原稿页面…</p>
              )}
              {imageError && (
                <div className="error" role="alert">
                  页面加载失败，请重试后再确认。
                  <button
                    onClick={() => {
                      setRetry(retry + 1)
                      setImageError(false)
                    }}
                  >
                    重试预览
                  </button>
                </div>
              )}
            </div>
            <svg className="review-arrow" aria-hidden="true">
              <defs>
                <marker
                  id="review-arrowhead"
                  markerWidth="7"
                  markerHeight="7"
                  refX="6"
                  refY="3.5"
                  orient="auto"
                >
                  <path d="M0,0 L7,3.5 L0,7" fill="#b64136" />
                </marker>
              </defs>
              {arrow && !imageError && (
                <path
                  d={arrow}
                  fill="none"
                  stroke="#b64136"
                  strokeWidth="1.8"
                  markerEnd="url(#review-arrowhead)"
                />
              )}
            </svg>
          </div>
        </div>
      )}
      {!pages.length && request.preview?.error && (
        <p className="error" role="alert">
          {request.preview.error}
        </p>
      )}
      {!!request.manual_items?.length && (
        <section className="unfinished-box" aria-label="需人工修改的事项">
          <h3>以下事项无法自动修改，需人工修改</h3>
          <p>确认后会将这些事项写入 Word 批注；不会擅自采用其它方案。</p>
          <ol>
            {request.manual_items.map((item) => (
              <li key={item.manual_id}>
                <ManualDetails item={item} />
              </li>
            ))}
          </ol>
        </section>
      )}
      <div className="review-submit">
        <p>
          将写入 {selected.size} 处修改，保留 {patches.length - selected.size} 处原文。
        </p>
        {submit.error && (
          <p className="error" role="alert">
            {submit.error.message}
          </p>
        )}
        <button
          className="primary"
          disabled={submit.isPending || imageError || (!!pages.length && loaded !== imageKey)}
          onClick={() => submit.mutate()}
        >
          {submit.isPending ? '正在保存选择…' : '确认并生成修订版'}
        </button>
      </div>
    </section>
  )
}

function scrollInside(container: HTMLElement | null, selector: string) {
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

export function UnfinishedBox({ run }: { run: Run }) {
  const items = (run.summary.unfinished || []).filter((item) =>
    ['manual', 'blocked', 'not_selected'].includes(item.kind || ''),
  )
  if (!items.length) return null
  const labels: Record<string, string> = {
    not_selected: '按你的选择保留',
    blocked: '未写入',
    verification: '检查限制',
    unresolved: '待核对',
  }
  return (
    <section className="unfinished-box" aria-label="未完成的修改及原因">
      <h2>未完成的修改及原因</h2>
      <p>这里列出确认时已告知的人工修改事项，以及确认后未写入或被取消的修改。</p>
      {items.length ? (
        <ol>
          {items.map((item, index) => (
            <li key={index}>
              {['manual', 'blocked'].includes(item.kind || '') ? (
                <ManualDetails item={item} />
              ) : (
                <>
                  <span className="unfinished-kind">{labels[item.kind || ''] || '待核对'}</span>
                  <strong>{item.title}</strong>
                  {item.position && <small>{item.position}</small>}
                  <p>原因：{item.reason}</p>
                </>
              )}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  )
}

function ManualDetails({ item }: { item: Unfinished }) {
  return (
    <>
      <strong>{item.title}</strong>
      {item.position && <p>{item.position}</p>}
      {item.evidence && (
        <blockquote>
          <strong>{item.evidence}</strong>
        </blockquote>
      )}
      <p>
        <b>无法修改，需人工修改。</b>
      </p>
      <p>
        <strong>无法修改的原因：</strong>
        <QuotedText text={item.blocked_reason || item.reason} evidence={item.evidence} />
      </p>
      <p>
        <strong>修改建议：</strong>
        <QuotedText
          text={item.suggestion || '请结合原文人工完成此项修改'}
          evidence={item.evidence}
        />
      </p>
      <p>
        <strong>原因：</strong>
        <QuotedText text={item.reason} evidence={item.evidence} />
      </p>
    </>
  )
}
