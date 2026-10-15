import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, NavLink, Routes, Route, Navigate, useNavigate, useParams } from 'react-router-dom'
import { ArrowRight, ArrowLeft, UploadCloud, FileText, BookOpen, LayoutDashboard, Users, LogOut, Download, CheckCircle2, Clock3, CircleHelp, Loader2, Plus, ShieldCheck, X } from 'lucide-react'
import { api, ApiError, setCsrf, labels, stages, active, date, type User, type Run, type Event, type HumanRequest, type Patch } from './api'

function ErrorText({ error }: { error: unknown }) { return error ? <div className="error" role="alert">{error instanceof Error ? error.message : '操作失败，请稍后再试'}</div> : null }
function Status({ value }: { value: string }) { return <span className={`status ${value}`}>{value === 'running' ? <Loader2 size={12} className="spin" /> : null}{labels[value] || value}</span> }

export default function App() {
  const client = useQueryClient()
  const user = useQuery({ queryKey: ['me'], queryFn: async () => {
    try { const value = await api<User>('/users/me'); setCsrf(value.csrf_token || ''); return value }
    catch (error) { if (error instanceof ApiError && error.status === 401) return null; throw error }
  }, retry: false, refetchInterval: 60000 })
  const logout = useMutation({ mutationFn: () => api('/auth/logout', { method: 'POST' }), onSuccess: () => { setCsrf(''); client.clear(); client.setQueryData(['me'], null) } })
  if (user.isPending) return <div className="loading-page"><Loader2 className="spin" /> 正在打开工作台</div>
  if (user.error) return <div className="loading-page"><ErrorText error={user.error} /><button onClick={() => user.refetch()}>重新连接</button></div>
  if (!user.data) return <Login />
  return <div className="app-shell">
    <aside className="sidebar"><Link to="/" className="brand"><BookOpen size={29} /><span>研稿<small>PAPER WORKSPACE</small></span></Link>
      <p className="nav-caption">你的研究工作台</p><nav><NavLink to="/" end><LayoutDashboard size={18} />论文任务</NavLink>{user.data.is_admin && <NavLink to="/users"><Users size={18} />内测账号</NavLink>}</nav>
      <div className="side-note"><ShieldCheck size={20} /><p>原稿始终保留<small>每一处修改都有修订痕迹，最终口径由你决定。</small></p></div>
      <div className="profile"><div className="avatar">{user.data.username[0].toUpperCase()}</div><div>{user.data.username}<small>{user.data.is_admin ? '管理员' : '内测成员'}</small></div><button className="icon-button" aria-label="退出登录" onClick={() => logout.mutate()}><LogOut size={17} /></button></div><ErrorText error={logout.error} />
    </aside>
    <main className="main"><header className="topbar"><span>让注意力回到研究本身</span><span className="invited"><span className="dot" />{user.data.demo ? '演示模式 · 不调用真实模型' : '邀请制内测'}</span></header>
      <Routes><Route path="/" element={<Dashboard user={user.data} />} /><Route path="/runs/:id" element={<Detail />} /><Route path="/users" element={user.data.is_admin ? <Accounts /> : <Navigate to="/" />} /><Route path="*" element={<Navigate to="/" />} /></Routes>
    </main>
  </div>
}

function Login() {
  const client = useQueryClient()
  const [username, setUsername] = useState(''), [password, setPassword] = useState('')
  const login = useMutation({ mutationFn: () => api<User>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }), onSuccess: value => { setCsrf(value.csrf_token || ''); client.setQueryData(['me'], value) } })
  return <div className="login-page"><section className="login-story"><div className="brand"><BookOpen size={32} /><span>研稿<small>PAPER WORKSPACE</small></span></div><div><span className="eyebrow">FROM DRAFT TO CLARITY</span><h1>把论文的每一次修改，<br />变成有据可循的进步。</h1><p>从全文审查到逐处修订，<br />保留你的研究，也保留你的决定。</p><div className="story-steps"><span>01 审查</span><span>02 确认</span><span>03 修订</span></div></div><small>面向研究者的论文修改工作台</small></section>
    <section className="login-form"><div className="login-card"><span className="eyebrow">WELCOME BACK</span><h2>登录你的工作台</h2><p className="muted">使用管理员为你创建的内测账号。</p><form onSubmit={e => { e.preventDefault(); login.mutate() }}><label>账号<input autoComplete="username" required value={username} onChange={e => setUsername(e.target.value)} placeholder="输入你的账号" /></label><label>密码<input type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} placeholder="输入密码" /></label><ErrorText error={login.error} /><button className="primary full" disabled={login.isPending}>{login.isPending ? '正在登录…' : '进入工作台'}<ArrowRight size={17} /></button></form><p className="login-foot">暂未开放自行注册。需要账号或重置密码，请联系管理员。</p></div></section>
  </div>
}

function Dashboard({ user }: { user: User }) {
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => api<Run[]>('/runs'), refetchInterval: 2500 })
  const [uploadOpen, setUploadOpen] = useState(false)
  const data = runs.data || []
  const counts = [data.filter(r => active(r.status)).length, data.filter(r => r.status === 'waiting_user').length, data.filter(r => r.status === 'completed').length]
  return <div className="page"><section className="hero"><div><span className="eyebrow">YOUR RESEARCH, REFINED</span><h1>从初稿，到更清晰的表达。</h1><p>上传论文，查看问题，确认口径。我们把修改落实到新的 Word 文件。</p></div><button className="primary" onClick={() => setUploadOpen(true)}><Plus size={18} />上传论文</button></section>
    <div className="stats-grid">{[['处理中', counts[0], Clock3], ['待你确认', counts[1], CircleHelp], ['已完成', counts[2], CheckCircle2]].map(([title, value, Icon]) => { const Component = Icon as typeof Clock3; return <div className="stat" key={String(title)}><Component size={20} /><span>{String(title)}</span><strong>{String(value).padStart(2, '0')}</strong></div> })}</div>
    <div className="section-title"><h2>论文任务 <span>{data.length}</span></h2><small>每日可提交 {user.daily_run_limit} 个任务 · 每篇额度 {user.run_token_limit.toLocaleString()} token</small></div>
    <ErrorText error={runs.error} />
    {!data.length && !runs.isPending ? <div className="empty"><FileText size={38} /><h3>第一份修订稿，从这里开始</h3><p>上传 DOCX 文件，原稿会单独保留。</p><button className="secondary" onClick={() => setUploadOpen(true)}>上传第一篇论文<ArrowRight size={16} /></button></div> : <div className="run-list">{data.map(run => <Link className="run-card" to={`/runs/${run.id}`} key={run.id}><div className="file-icon"><FileText size={23} /></div><div className="run-title"><h3>{run.document_name}</h3><p>{stages[run.stage] || labels[run.status]}<span>·</span>{date(run.created_at)}</p></div><Status value={run.status} /><ArrowRight className="muted" size={19} /></Link>)}</div>}
    <div className="bottom-note"><ShieldCheck size={17} />术语与事实需要选择时会暂停。你可以关闭页面，稍后回来继续。</div>
    {uploadOpen && <Upload onClose={() => setUploadOpen(false)} />}
  </div>
}

function Upload({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate(), client = useQueryClient()
  const [file, setFile] = useState<File | null>(null), [request, setRequest] = useState('全面检查并修改这篇论文'), [rules, setRules] = useState('不要修改实验结果与数据\n保持作者原有表达风格')
  const create = useMutation({ mutationFn: async () => { const form = new FormData(); form.set('file', file!); form.set('request', request); form.set('rules', JSON.stringify(rules.split('\n').map(r => r.trim()).filter(Boolean))); return api<Run>('/runs', { method: 'POST', body: form }) }, onSuccess: run => { client.invalidateQueries({ queryKey: ['runs'] }); navigate(`/runs/${run.id}`); onClose() } })
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label="上传论文"><div className="section-title"><h2>准备你的论文</h2><button className="icon-button" aria-label="关闭上传" disabled={create.isPending} onClick={onClose}><X size={20} /></button></div><p className="muted">提交后会在后台处理，进展会保存在任务详情中。</p><form onSubmit={e => { e.preventDefault(); create.mutate() }}><label className="upload-zone"><UploadCloud size={34} /><strong>{file ? file.name : '点击选择 Word 论文'}</strong><span>DOCX / DOC / WPS / RTF / ODT · 最大 20 MB</span><input type="file" accept=".docx,.doc,.wps,.rtf,.odt" aria-label="选择论文文件" required onChange={e => setFile(e.target.files?.[0] || null)} /></label><p className="muted small">非 DOCX 文件会在后台转换，原稿保留，输出统一为 DOCX。仅支持 Word 兼容的 WPS 文件；复杂排版可能变化，建议优先上传 DOCX。</p><label>修改目标<textarea value={request} onChange={e => setRequest(e.target.value)} required maxLength={4000} rows={2} /></label><label>需要遵守的要求<small>每行一条</small><textarea value={rules} onChange={e => setRules(e.target.value)} rows={3} /></label><ErrorText error={create.error} /><button className="primary full" disabled={!file || create.isPending}>{create.isPending ? '正在保存论文…' : '提交并开始处理'}<ArrowRight size={17} /></button></form></section></div>
}

function Detail() {
  const { id } = useParams(), client = useQueryClient(), [report, setReport] = useState(''), [reportError, setReportError] = useState<unknown>(null)
  const detail = useQuery({ queryKey: ['run', id], queryFn: () => api<Run>(`/runs/${id}`), refetchInterval: query => active(query.state.data?.status || '') ? 2000 : query.state.data?.status === 'waiting_user' ? 5000 : false })
  const events = useQuery({ queryKey: ['events', id], queryFn: () => api<{ events: Event[] }>(`/runs/${id}/events`), refetchInterval: detail.data && active(detail.data.status) ? 2500 : 5000 })
  const change = useMutation({ mutationFn: (action: string) => api(`/runs/${id}/${action}`, { method: 'POST' }), onSuccess: () => { client.invalidateQueries({ queryKey: ['run', id] }); client.invalidateQueries({ queryKey: ['events', id] }) } })
  if (detail.isPending) return <div className="page"><Loader2 className="spin" /></div>
  if (detail.error || !detail.data) return <div className="page"><Link to="/">返回任务列表</Link><ErrorText error={detail.error} /></div>
  const run = detail.data
  const step = run.status === 'completed' ? 4 : run.pending?.request.type === 'final_confirm' ? 3 : run.status === 'waiting_user' ? 2 : ['revision', 'review'].includes(run.stage) ? 3 : 1
  async function loadReport() { try { const response = await fetch(`/api/v1/runs/${id}/download?kind=report`); if (!response.ok) throw new Error('报告暂时无法读取'); setReport(await response.text()); setReportError(null) } catch (error) { setReportError(error) } }
  return <div className="page"><Link className="back" to="/"><ArrowLeft size={16} />全部论文任务</Link><div className="detail-heading"><div><span className="eyebrow">PAPER REVISION</span><h1>{run.document_name}</h1><p className="muted">{date(run.created_at)} 提交 · {run.request}</p></div><Status value={run.status} /></div>
    <div className="workflow-strip">{['论文上传', '全文审查', '确认口径', '修订与复核', '文件交付'].map((label, index) => <div className={index < step ? 'done' : index === step ? 'current' : ''} key={label}><span>{index < step ? <CheckCircle2 size={16} /> : `0${index + 1}`}</span>{label}</div>)}</div>
    {run.pending ? <DecisionForm key={run.pending.id} run={run} /> : run.status === 'completed' ? <section className="result-card"><div className="result-icon"><CheckCircle2 size={30} /></div><div><span className="eyebrow">READY TO REVIEW</span><h2>你的修订文件已就绪</h2><p>共生成 {run.summary.changes || 0} 处修改，最终剔除 {run.summary.excluded || 0} 处。原稿已保留。</p>{run.summary.review_summary && <p className="muted">{run.summary.review_summary}</p>}<div className="button-row"><a className="primary" href={`/api/v1/runs/${id}/download`}><Download size={17} />下载修订版 Word</a><button className="secondary" onClick={loadReport}>查看修改报告</button><a className="text-button" href={`/api/v1/runs/${id}/download?kind=report`}>下载报告</a></div></div></section> : <section className={`processing-card ${run.status === 'failed' ? 'failure' : ''}`}><div>{active(run.status) ? <Loader2 className="spin" size={27} /> : <CircleHelp size={27} />}</div><h2>{run.status === 'failed' ? '这次处理遇到了问题' : run.status === 'cancelled' ? '任务已取消' : stages[run.stage] || labels[run.status]}</h2><p>{run.error || (active(run.status) ? '你可以离开页面，处理进展会自动保存。' : '')}</p>{run.status === 'failed' && <button className="secondary" disabled={change.isPending} onClick={() => change.mutate('retry')}>从保存的位置重试</button>}</section>}
    {run.status === 'completed' && <p className="warning">批注以“将原文改为新文”合并说明原因。Word / WPS 仍可能分别显示删除和插入修订；删除批注不会撤销正文修改。撤销替换需同时拒绝对应的删除与插入，不能只点一个气泡的 ×。建议在生成前的最终确认中整项剔除。</p>}
    <ErrorText error={change.error || reportError} />
    {(run.summary.warnings || []).length > 0 && <div className="warning"><strong>本次处理存在以下限制</strong>{run.summary.warnings!.map((item, i) => <p key={i}>{item}</p>)}</div>}
    {report && <section className="report"><div className="section-title"><h2>修改报告</h2><button className="text-button" onClick={() => setReport('')}>收起</button></div><pre>{report}</pre></section>}
    <div className="detail-bottom"><section className="panel"><h3>处理记录</h3>{(events.data?.events || []).slice(-20).map(e => <div className="event" key={e.id}><span className="event-dot" /><p>{e.message}<small>{date(e.created_at)}</small></p></div>)}<ErrorText error={events.error} /></section><section className="panel"><h3>本篇任务额度</h3><div className="token-value">{run.used_tokens.toLocaleString()}<small> / {run.token_limit.toLocaleString()} token</small></div><div className="meter"><span style={{ width: `${Math.min(100, run.used_tokens / run.token_limit * 100)}%` }} /></div><p className="muted small">按模型调用用量记账。等待你选择的时间不计入处理时限。</p>{!['completed', 'failed', 'cancelled', 'cancel_requested'].includes(run.status) && <button className="text-button danger" disabled={change.isPending} onClick={() => change.mutate('cancel')}>取消这个任务</button>}</section></div>
  </div>
}

function PatchView({ patch }: { patch: Patch }) { return <div className="patch-body"><span className="patch-location">{patch.position || patch.span_label || '全文'}{patch.count ? ` · 共 ${patch.count} 处` : ''}</span>{patch.problem && <h4>{patch.problem}</h4>}<div className="diff"><div><small>原文</small><p>{patch.before || '—'}</p></div><div><small>修改为</small><p>{patch.replace || '（删除）'}</p></div></div><p className="reason">修改依据：{patch.rationale || '—'}</p>{patch.conflict_reason && <p className="warning">{patch.conflict_reason}</p>}</div> }

function DecisionForm({ run }: { run: Run }) {
  const client = useQueryClient(), pending = run.pending!, request: HumanRequest = pending.request
  const [choices, setChoices] = useState<Record<string, number>>({}), [rules, setRules] = useState('')
  const previous = new Set(request.defaults?.excluded_ids || [])
  const [selected, setSelected] = useState<string[]>(() => request.type === 'final_confirm' ? (request.patches || []).filter(p => (p.patch_ids || [p.patch_id]).some(id => previous.has(id))).map(p => p.patch_id) : (request.patches || []).filter(p => p.status === 'proposed').map(p => p.patch_id))
  const [approved, setApproved] = useState<number[]>((request.issues || []).map(i => i.id))
  const submit = useMutation({ mutationFn: (payload: object) => api(`/runs/${run.id}/decisions`, { method: 'POST', body: JSON.stringify({ pending_id: pending.id, version: run.version, payload }) }), onSuccess: () => { client.invalidateQueries({ queryKey: ['run', run.id] }); client.invalidateQueries({ queryKey: ['events', run.id] }) } })
  function toggle(id: string) { setSelected(items => items.includes(id) ? items.filter(i => i !== id) : [...items, id]) }
  function send(e: FormEvent) { e.preventDefault(); const extra = rules.split('\n').map(r => r.trim()).filter(Boolean); let payload: object
    if (request.type === 'issue_review') payload = { choices, rules: extra }
    else if (request.type === 'patch_review') payload = { accepted_ids: selected }
    else if (request.type === 'final_confirm') payload = { excluded_ids: selected }
    else payload = { approved_issue_ids: approved, constraints: extra, decision: 'approve' }
    submit.mutate(payload)
  }
  if (!['issue_review', 'patch_review', 'final_confirm', 'human_approval'].includes(request.type)) return <ErrorText error={new Error('此确认类型暂不支持，请联系管理员')} />
  return <section className="decision-panel"><div className="decision-title"><CircleHelp size={23} /><div><span className="eyebrow">YOUR CALL</span><h2>{request.type === 'issue_review' ? '这些口径，需要由你决定' : request.type === 'final_confirm' ? '生成文件前，最后确认一次' : '确认本轮修改'}</h2></div><span className="decision-count">{request.issues?.length || request.patches?.length || 0} 项</span></div><p className="muted">{request.type === 'final_confirm' ? '下面列出本次全部改动。勾选你不希望写入文件的修改；不勾选则全部写入。' : '选择后会在后台继续处理。没有把握的项目可以先不修改。'}</p><form onSubmit={send}>
    {request.type === 'issue_review' && (request.issues || []).map((issue, i) => <fieldset className="issue" key={issue.id}><legend><span>{String(i + 1).padStart(2, '0')}</span>{issue.problem}</legend><p className="patch-location">{issue.span_label || issue.location}</p>{issue.evidence && <blockquote>{issue.evidence}</blockquote>}<div className="choice-grid">{(issue.options || []).map((option, index) => <label className={`choice ${choices[issue.id] === index ? 'checked' : ''}`} key={index}><input type="radio" name={`issue-${issue.id}`} checked={choices[issue.id] === index} onChange={() => setChoices({ ...choices, [issue.id]: index })} />{option}</label>)}<label className="choice"><input type="radio" name={`issue-${issue.id}`} checked={choices[issue.id] === undefined} onChange={() => setChoices(old => { const next = { ...old }; delete next[issue.id]; return next })} />先不修改</label></div></fieldset>)}
    {['patch_review', 'final_confirm'].includes(request.type) && (request.patches || []).map(patch => <div className="patch" key={patch.patch_id}><label className="patch-check"><input type="checkbox" checked={selected.includes(patch.patch_id)} disabled={request.type === 'patch_review' && patch.status !== 'proposed'} onChange={() => toggle(patch.patch_id)} />{request.type === 'final_confirm' ? '剔除此项修改' : '接受此项修改'}</label><PatchView patch={patch} /></div>)}
    {request.type === 'human_approval' && (request.issues || []).map(issue => <label key={issue.id} className="choice"><input type="checkbox" checked={approved.includes(issue.id)} onChange={() => setApproved(old => old.includes(issue.id) ? old.filter(i => i !== issue.id) : [...old, issue.id])} />{issue.problem}</label>)}
    {['issue_review', 'human_approval'].includes(request.type) && <label>补充判断或要求<small>可选，每行一条</small><textarea rows={2} value={rules} onChange={e => setRules(e.target.value)} placeholder="例如：温度系数以 0.07 为准" /></label>}
    <ErrorText error={submit.error} /><button className="primary" disabled={submit.isPending}>{submit.isPending ? '正在保存选择…' : request.type === 'final_confirm' ? '确认并生成修订版' : '确认选择并继续'}<ArrowRight size={17} /></button>
  </form></section>
}

function Accounts() {
  const client = useQueryClient(), [username, setUsername] = useState(''), [password, setPassword] = useState(''), [daily, setDaily] = useState(5), [tokens, setTokens] = useState(400000)
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<User[]>('/admin/users') })
  const create = useMutation({ mutationFn: () => api('/admin/users', { method: 'POST', body: JSON.stringify({ username, password, daily_run_limit: daily, run_token_limit: tokens }) }), onSuccess: () => { setUsername(''); setPassword(''); client.invalidateQueries({ queryKey: ['users'] }) } })
  return <div className="page"><span className="eyebrow">INVITE YOUR COLLEAGUES</span><h1>内测账号</h1><p className="muted">创建账号后，将账号与初始密码私下交给成员。额度可以随时调整。</p><section className="panel"><h2>邀请一位成员</h2><form className="invite-form" onSubmit={e => { e.preventDefault(); create.mutate() }}><label>账号<input required value={username} onChange={e => setUsername(e.target.value)} /></label><label>初始密码<input required minLength={12} type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} placeholder="至少 12 位" /></label><label>每日任务<input type="number" min={1} max={1000} value={daily} onChange={e => setDaily(Number(e.target.value))} /></label><label>每篇 token 额度<input type="number" min={1000} max={5000000} step={1000} value={tokens} onChange={e => setTokens(Number(e.target.value))} /></label><button className="primary" disabled={create.isPending}>创建账号<Plus size={16} /></button></form><ErrorText error={create.error} />{create.isSuccess && <p className="success" role="status">账号已创建。</p>}</section><div className="section-title"><h2>已有成员</h2><span className="muted">{users.data?.length || 0} 个账号</span></div><ErrorText error={users.error} /><div className="account-list">{users.data?.map(user => <UserRow key={user.id} user={user} />)}</div></div>
}

function UserRow({ user }: { user: User }) {
  const client = useQueryClient(), [daily, setDaily] = useState(user.daily_run_limit), [tokens, setTokens] = useState(user.run_token_limit)
  const update = useMutation({ mutationFn: (values: object) => api(`/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify(values) }), onSuccess: () => client.invalidateQueries({ queryKey: ['users'] }) })
  return <div className="account-row"><div><strong>{user.username}</strong><small>{user.is_admin ? '管理员' : user.active ? '内测成员' : '已停用'}</small></div><label>每日任务<input aria-label={`${user.username}每日任务`} type="number" min={1} value={daily} onChange={e => setDaily(Number(e.target.value))} /></label><label>每篇额度<input aria-label={`${user.username}每篇额度`} type="number" min={1000} value={tokens} onChange={e => setTokens(Number(e.target.value))} /></label><button className="secondary" disabled={update.isPending} onClick={() => update.mutate({ daily_run_limit: daily, run_token_limit: tokens })}>保存额度</button>{!user.is_admin && <button className="text-button" onClick={() => update.mutate({ active: !user.active })}>{user.active ? '停用' : '启用'}</button>}<ErrorText error={update.error} /></div>
}
