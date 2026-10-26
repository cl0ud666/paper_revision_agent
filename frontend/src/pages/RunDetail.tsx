import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, CheckCircle2, CircleHelp, Download, Loader2 } from 'lucide-react'
import { api, active, date, labels, stages, type Run, type Event } from '../api'
import { VisualReview } from '../features/review/VisualReview'
import { UnfinishedBox } from '../features/review/ManualItems'
import { DecisionForm } from '../components/DecisionForm'
import { ErrorText, Status, WAIT_MESSAGE } from '../components/Feedback'

export function Detail() {
  const { id } = useParams(),
    client = useQueryClient()
  const detail = useQuery({
    queryKey: ['run', id],
    queryFn: () => api<Run>(`/runs/${id}`),
    refetchInterval: (query) =>
      active(query.state.data?.status || '')
        ? 2000
        : query.state.data?.status === 'waiting_user'
          ? 5000
          : false,
  })
  const events = useQuery({
    queryKey: ['events', id],
    queryFn: () => api<{ events: Event[] }>(`/runs/${id}/events?latest=true`),
    refetchInterval: detail.data && active(detail.data.status) ? 2500 : 5000,
  })
  const change = useMutation({
    mutationFn: (action: string) => api(`/runs/${id}/${action}`, { method: 'POST' }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['run', id] })
      client.invalidateQueries({ queryKey: ['events', id] })
      client.invalidateQueries({ queryKey: ['me'] })
    },
  })
  if (detail.isPending)
    return (
      <div className="page loading-page">
        <Loader2 className="spin" />
        {WAIT_MESSAGE}
      </div>
    )
  if (detail.error || !detail.data)
    return (
      <div className="page">
        <Link to="/">返回任务列表</Link>
        <ErrorText error={detail.error} />
      </div>
    )
  const run = detail.data
  const step =
    run.status === 'completed'
      ? 4
      : run.pending?.request.type === 'final_confirm'
        ? 3
        : run.status === 'waiting_user'
          ? 2
          : ['revision', 'review'].includes(run.stage)
            ? 3
            : 1
  return (
    <div className="page">
      <Link className="back" to="/">
        <ArrowLeft size={16} />
        全部论文任务
      </Link>
      <div className="detail-heading">
        <div>
          <span className="eyebrow">PAPER REVISION</span>
          <h1>{run.document_name}</h1>
          <p className="muted">
            {date(run.created_at)} 提交 · {run.request}
          </p>
        </div>
        <Status value={run.status} />
      </div>
      <div className="workflow-strip">
        {['论文上传', '全文审查', '确认口径', '修订与复核', '文件交付'].map((label, index) => (
          <div className={index < step ? 'done' : index === step ? 'current' : ''} key={label}>
            <span>{index < step ? <CheckCircle2 size={16} /> : `0${index + 1}`}</span>
            {label}
          </div>
        ))}
      </div>
      {run.pending ? (
        run.pending.request.visual ? (
          <VisualReview key={run.pending.id} run={run} />
        ) : (
          <DecisionForm key={run.pending.id} run={run} />
        )
      ) : run.status === 'completed' ? (
        <section className="result-card">
          <div className="result-icon">
            <CheckCircle2 size={30} />
          </div>
          <div>
            <span className="eyebrow">READY TO REVIEW</span>
            <div className="result-heading">
              <h2>
                你的修订文件已就绪，所有修订及未能写进去的修订均已在修订版 Word
                里面添加了批注，可点击审阅-显示标记的最终状态进行查看。
              </h2>
            </div>
            <p>
              实际写入 {run.summary.written ?? run.summary.changes ?? 0} 处修改，最终取消{' '}
              {run.summary.excluded || 0} 处。原稿已保留。
            </p>
            <div className="button-row">
              <a className="primary" href={`/api/v1/runs/${id}/download`}>
                <Download size={17} />
                下载修订版 Word
              </a>
            </div>
          </div>
        </section>
      ) : (
        <section className={`processing-card ${run.status === 'failed' ? 'failure' : ''}`}>
          <div>
            {active(run.status) ? <Loader2 className="spin" size={27} /> : <CircleHelp size={27} />}
          </div>
          <h2>
            {run.status === 'failed'
              ? '这次处理遇到了问题'
              : run.status === 'cancelled'
                ? '任务已取消'
                : stages[run.stage] || labels[run.status]}
          </h2>
          <p>{run.error || (active(run.status) ? WAIT_MESSAGE : '')}</p>
          {run.status === 'failed' && (
            <button
              className="secondary"
              disabled={change.isPending}
              onClick={() => change.mutate('retry')}
            >
              从保存的位置重试
            </button>
          )}
        </section>
      )}
      {run.status === 'completed' && <UnfinishedBox run={run} />}
      <ErrorText error={change.error} />
      {(run.summary.warnings || []).length > 0 && (
        <div className="warning">
          <strong>本次处理存在以下限制</strong>
          {run.summary.warnings!.map((item, i) => (
            <p key={i}>{item}</p>
          ))}
        </div>
      )}
      <div className="detail-bottom">
        <section className="panel">
          <h3>处理记录</h3>
          {(events.data?.events || []).slice(-20).map((e) => (
            <div className="event" key={e.id}>
              <span className="event-dot" />
              <p>
                {e.message}
                <small>{date(e.created_at)}</small>
              </p>
            </div>
          ))}
          <ErrorText error={events.error} />
          {!['completed', 'failed', 'cancelled', 'cancel_requested'].includes(run.status) && (
            <button
              className="text-button danger"
              disabled={change.isPending}
              onClick={() => change.mutate('cancel')}
            >
              取消这个任务
            </button>
          )}
        </section>
      </div>
    </div>
  )
}
