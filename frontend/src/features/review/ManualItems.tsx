import type { Run, Unfinished } from '../../api'
import { QuotedText } from '../../QuotedText'

export function UnfinishedBox({ run }: { run: Run }) {
  const items = (run.summary.unfinished || []).filter((item) =>
    ['manual', 'blocked', 'not_selected'].includes(item.kind || ''),
  )
  if (!items.length) return null
  const labels: Record<string, string> = {
    not_selected: '按你的选择保留',
    blocked: '未写入',
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

export function ManualDetails({ item }: { item: Unfinished }) {
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
