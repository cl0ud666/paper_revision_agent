import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ArrowRight, FileText, Clock3, CircleHelp, CheckCircle2, Plus } from 'lucide-react'
import { api, active, date, labels, stages, type User, type Run } from '../api'
import { ErrorText, Status } from '../components/Feedback'
import { Upload } from '../components/Upload'

export function Dashboard({ user }: { user: User }) {
  const runs = useQuery({
    queryKey: ['runs'],
    queryFn: () => api<Run[]>('/runs'),
    refetchInterval: 2500,
  })
  const [uploadOpen, setUploadOpen] = useState(false)
  const data = runs.data || []
  const quota = user.free_quota
  const canUpload = user.is_admin || !quota || quota.remaining > 0
  const counts = [
    data.filter((r) => active(r.status)).length,
    data.filter((r) => r.status === 'waiting_user').length,
    data.filter((r) => r.status === 'completed').length,
  ]
  return (
    <div className="page dashboard-page">
      <section className="hero dashboard-hero">
        <div className="tagline-copy">
          <h1 className="refined-tagline">
            从语句到逻辑，<span>paperfixer</span>帮你精修论文
          </h1>
          <p className="tagline-en">
            From sentences to logic, PaperFixer helps you polish your paper
          </p>
        </div>
        <button className="primary" disabled={!canUpload} onClick={() => setUploadOpen(true)}>
          <Plus size={18} />
          上传论文
        </button>
      </section>
      <div className="stats-grid">
        {[
          ['处理中', counts[0], Clock3],
          ['待你确认', counts[1], CircleHelp],
          ['已完成', counts[2], CheckCircle2],
        ].map(([title, value, Icon]) => {
          const Component = Icon as typeof Clock3
          return (
            <div className="stat" key={String(title)}>
              <Component size={20} />
              <span>{String(title)}</span>
              <strong>{String(value).padStart(2, '0')}</strong>
            </div>
          )
        })}
      </div>
      <div className="section-title">
        <h2>
          论文任务 <span>{data.length}</span>
        </h2>
        <small>每日最多提交 {user.daily_run_limit} 个任务</small>
      </div>
      <ErrorText error={runs.error} />
      {!data.length && !runs.isPending ? (
        <div className="empty">
          <FileText size={38} />
          <h3>第一份修订稿，从这里开始</h3>
          <p>上传Word文件，原稿会单独保留。</p>
          <button className="secondary" disabled={!canUpload} onClick={() => setUploadOpen(true)}>
            上传第一篇论文
            <ArrowRight size={16} />
          </button>
        </div>
      ) : (
        <div className="run-list">
          {data.map((run) => (
            <Link className="run-card" to={`/runs/${run.id}`} key={run.id}>
              <div className="file-icon">
                <FileText size={23} />
              </div>
              <div className="run-title">
                <h3>{run.document_name}</h3>
                <p>
                  {stages[run.stage] || labels[run.status]}
                  <span>·</span>
                  {date(run.created_at)}
                </p>
              </div>
              <Status value={run.status} />
              <ArrowRight className="muted" size={19} />
            </Link>
          ))}
        </div>
      )}
      {uploadOpen && <Upload onClose={() => setUploadOpen(false)} />}
    </div>
  )
}
