import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { api, type User } from '../api'
import { ErrorText } from '../components/Feedback'

export function Accounts() {
  const client = useQueryClient(),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [daily, setDaily] = useState(5)
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<User[]>('/admin/users') })
  const create = useMutation({
    mutationFn: () =>
      api('/admin/users', {
        method: 'POST',
        body: JSON.stringify({ username, password, daily_run_limit: daily }),
      }),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      client.invalidateQueries({ queryKey: ['users'] })
    },
  })
  return (
    <div className="page">
      <span className="eyebrow">MANAGE ACCOUNTS</span>
      <h1>用户管理</h1>
      <section className="panel">
        <h2>创建账号</h2>
        <form
          className="invite-form"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <label>
            账号
            <input required value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <label>
            初始密码
            <input
              required
              minLength={6}
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少6位"
            />
          </label>
          <label>
            每日任务
            <input
              type="number"
              min={1}
              max={1000}
              value={daily}
              onChange={(e) => setDaily(Number(e.target.value))}
            />
          </label>
          <button className="primary" disabled={create.isPending}>
            创建账号
            <Plus size={16} />
          </button>
        </form>
        <ErrorText error={create.error} />
        {create.isSuccess && (
          <p className="success" role="status">
            账号已创建。
          </p>
        )}
      </section>
      <div className="section-title">
        <h2>已有用户</h2>
        <span className="muted">{users.data?.length || 0} 个账号</span>
      </div>
      <ErrorText error={users.error} />
      <div className="account-list">
        {users.data?.map((user) => (
          <UserRow key={user.id} user={user} />
        ))}
      </div>
    </div>
  )
}

function UserRow({ user }: { user: User }) {
  const client = useQueryClient(),
    [daily, setDaily] = useState(user.daily_run_limit)
  const update = useMutation({
    mutationFn: (values: object) =>
      api(`/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify(values) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['users'] }),
  })
  return (
    <div className="account-row">
      <div>
        <strong>{user.username}</strong>
        <small>{user.is_admin ? '管理员' : user.active ? '注册用户' : '已停用'}</small>
      </div>
      <label>
        每日任务
        <input
          aria-label={`${user.username}每日任务`}
          type="number"
          min={1}
          value={daily}
          onChange={(e) => setDaily(Number(e.target.value))}
        />
      </label>
      <button
        className="secondary"
        disabled={update.isPending}
        onClick={() => update.mutate({ daily_run_limit: daily })}
      >
        保存限制
      </button>
      {!user.is_admin && (
        <button className="text-button" onClick={() => update.mutate({ active: !user.active })}>
          {user.active ? '停用' : '启用'}
        </button>
      )}
      <ErrorText error={update.error} />
    </div>
  )
}
