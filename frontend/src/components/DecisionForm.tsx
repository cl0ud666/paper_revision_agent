import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, CircleHelp } from 'lucide-react'
import { api, type Run, type HumanRequest, type Patch } from '../api'
import { QuotedText } from '../QuotedText'
import { ErrorText } from './Feedback'

function PatchView({ patch }: { patch: Patch }) {
  return (
    <div className="patch-body">
      <span className="patch-location">
        {patch.position || patch.span_label || '全文'}
        {patch.count ? ` · 共 ${patch.count} 处` : ''}
      </span>
      {patch.problem && <h4>{patch.problem}</h4>}
      <div className="diff">
        <div>
          <small>原文</small>
          <p>{patch.before || '—'}</p>
        </div>
        <div>
          <small>修改为</small>
          <p>{patch.replace || '（删除）'}</p>
        </div>
      </div>
      <p className="reason">修改依据：{patch.rationale || '—'}</p>
      {patch.conflict_reason && <p className="warning">{patch.conflict_reason}</p>}
    </div>
  )
}

export function DecisionForm({ run }: { run: Run }) {
  const client = useQueryClient(),
    pending = run.pending!,
    request: HumanRequest = pending.request
  const [choices, setChoices] = useState<Record<string, number>>({}),
    [rules, setRules] = useState('')
  const previous = new Set(request.defaults?.excluded_ids || [])
  const [selected, setSelected] = useState<string[]>(() =>
    request.type === 'final_confirm'
      ? (request.patches || [])
          .filter((p) => (p.patch_ids || [p.patch_id]).some((id) => previous.has(id)))
          .map((p) => p.patch_id)
      : (request.patches || []).filter((p) => p.status === 'proposed').map((p) => p.patch_id),
  )
  const [approved, setApproved] = useState<number[]>((request.issues || []).map((i) => i.id))
  const submit = useMutation({
    mutationFn: (payload: object) =>
      api(`/runs/${run.id}/decisions`, {
        method: 'POST',
        body: JSON.stringify({ pending_id: pending.id, version: run.version, payload }),
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['run', run.id] })
      client.invalidateQueries({ queryKey: ['events', run.id] })
    },
  })
  function toggle(id: string) {
    setSelected((items) => (items.includes(id) ? items.filter((i) => i !== id) : [...items, id]))
  }
  function send(e: FormEvent) {
    e.preventDefault()
    const extra = rules
      .split('\n')
      .map((r) => r.trim())
      .filter(Boolean)
    let payload: object
    if (request.type === 'issue_review') payload = { choices, rules: extra }
    else if (request.type === 'patch_review') payload = { accepted_ids: selected }
    else if (request.type === 'final_confirm') payload = { excluded_ids: selected }
    else payload = { approved_issue_ids: approved, constraints: extra, decision: 'approve' }
    submit.mutate(payload)
  }
  if (!['issue_review', 'patch_review', 'final_confirm', 'human_approval'].includes(request.type))
    return <ErrorText error={new Error('此确认类型暂不支持，请联系管理员')} />
  return (
    <section className="decision-panel">
      <div className="decision-title">
        <CircleHelp size={23} />
        <div>
          <span className="eyebrow">YOUR CALL</span>
          <h2>
            {request.type === 'issue_review'
              ? '这些口径，需要由你决定'
              : request.type === 'final_confirm'
                ? '生成文件前，最后确认一次'
                : '确认本轮修改'}
          </h2>
        </div>
        <span className="decision-count">
          {request.issues?.length || request.patches?.length || 0} 项
        </span>
      </div>
      {request.type === 'final_confirm' && (
        <p className="muted">
          下面列出本次全部改动。勾选你不希望写入文件的修改；不勾选则全部写入。
        </p>
      )}
      <form onSubmit={send}>
        {request.type === 'issue_review' &&
          (request.issues || [])
            .filter((issue) => (issue.options?.length || 0) >= 2)
            .map((issue, i) => (
              <fieldset className="issue" key={issue.id}>
                <legend>
                  <span>{String(i + 1).padStart(2, '0')}</span>
                  <QuotedText
                    text={issue.display?.problem || issue.problem}
                    evidence={issue.evidence}
                  />
                </legend>
                <p className="patch-location">{issue.display?.position || '请对照下方原文'}</p>
                {issue.evidence && (
                  <blockquote>
                    <strong>{issue.evidence}</strong>
                  </blockquote>
                )}
                <div className="choice-grid">
                  {(issue.options || []).map((option, index) => (
                    <label
                      className={`choice ${choices[issue.id] === index ? 'checked' : ''}`}
                      key={index}
                    >
                      <input
                        type="radio"
                        name={`issue-${issue.id}`}
                        checked={choices[issue.id] === index}
                        onChange={() => setChoices({ ...choices, [issue.id]: index })}
                      />
                      <QuotedText
                        text={issue.display?.display_options[index] || option}
                        evidence={issue.evidence}
                      />
                    </label>
                  ))}
                  <label className="choice">
                    <input
                      type="radio"
                      name={`issue-${issue.id}`}
                      checked={choices[issue.id] === undefined}
                      onChange={() =>
                        setChoices((old) => {
                          const next = { ...old }
                          delete next[issue.id]
                          return next
                        })
                      }
                    />
                    先不修改
                  </label>
                </div>
              </fieldset>
            ))}
        {['patch_review', 'final_confirm'].includes(request.type) &&
          (request.patches || []).map((patch) => (
            <div className="patch" key={patch.patch_id}>
              <label className="patch-check">
                <input
                  type="checkbox"
                  checked={selected.includes(patch.patch_id)}
                  disabled={request.type === 'patch_review' && patch.status !== 'proposed'}
                  onChange={() => toggle(patch.patch_id)}
                />
                {request.type === 'final_confirm' ? '剔除此项修改' : '接受此项修改'}
              </label>
              <PatchView patch={patch} />
            </div>
          ))}
        {request.type === 'human_approval' &&
          (request.issues || []).map((issue) => (
            <label key={issue.id} className="choice">
              <input
                type="checkbox"
                checked={approved.includes(issue.id)}
                onChange={() =>
                  setApproved((old) =>
                    old.includes(issue.id) ? old.filter((i) => i !== issue.id) : [...old, issue.id],
                  )
                }
              />
              {issue.problem}
            </label>
          ))}
        {['issue_review', 'human_approval'].includes(request.type) && (
          <label>
            补充判断或要求<small>可选，每行一条</small>
            <textarea
              rows={2}
              value={rules}
              onChange={(e) => setRules(e.target.value)}
              placeholder="例如：温度系数以 0.07 为准"
            />
          </label>
        )}
        <ErrorText error={submit.error} />
        <button className="primary" disabled={submit.isPending}>
          {submit.isPending
            ? '正在保存选择…'
            : request.type === 'final_confirm'
              ? '确认并生成修订版'
              : '确认选择并继续'}
          <ArrowRight size={17} />
        </button>
      </form>
    </section>
  )
}
