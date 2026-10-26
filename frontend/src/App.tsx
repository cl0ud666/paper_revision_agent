import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, NavLink, Routes, Route, Navigate } from 'react-router-dom'
import { BookOpen, LayoutDashboard, Users, LogOut, Loader2 } from 'lucide-react'
import { api, ApiError, setCsrf, type User } from './api'
import { Login } from './Auth'
import { ErrorText, WAIT_MESSAGE } from './components/Feedback'
import { Dashboard } from './pages/Dashboard'
import { Detail } from './pages/RunDetail'
import { Accounts } from './pages/Accounts'

export default function App() {
  const client = useQueryClient()
  const user = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      try {
        const value = await api<User>('/users/me')
        setCsrf(value.csrf_token || '')
        return value
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
    retry: false,
    refetchInterval: 60000,
  })
  const logout = useMutation({
    mutationFn: () => api('/auth/logout', { method: 'POST' }),
    onSuccess: () => {
      setCsrf('')
      client.clear()
      client.setQueryData(['me'], null)
    },
  })
  if (user.isPending)
    return (
      <div className="loading-page">
        <Loader2 className="spin" />
        {WAIT_MESSAGE}
      </div>
    )
  if (user.error)
    return (
      <div className="loading-page">
        <ErrorText error={user.error} />
        <button onClick={() => user.refetch()}>重新连接</button>
      </div>
    )
  if (!user.data) return <Login />
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link to="/" className="brand">
          <BookOpen size={29} />
          <span>
            paperfixer<small>PAPER WORKSPACE</small>
          </span>
        </Link>
        <p className="nav-caption">你的研究工作台</p>
        <nav>
          <NavLink to="/" end>
            <LayoutDashboard size={18} />
            论文任务
          </NavLink>
          {user.data.is_admin && (
            <NavLink to="/users">
              <Users size={18} />
              用户管理
            </NavLink>
          )}
        </nav>
        <div className="profile">
          <div className="avatar">{user.data.username[0].toUpperCase()}</div>
          <div>
            {user.data.username}
            <small>{user.data.is_admin ? '管理员' : '注册用户'}</small>
          </div>
          <button className="icon-button" aria-label="退出登录" onClick={() => logout.mutate()}>
            <LogOut size={17} />
          </button>
        </div>
        <ErrorText error={logout.error} />
      </aside>
      <main className="main">
        {user.data.demo && (
          <header className="topbar">
            <span className="invited">
              <span className="dot" />
              演示模式 · 不调用真实模型
            </span>
          </header>
        )}
        <Routes>
          <Route path="/" element={<Dashboard user={user.data} />} />
          <Route path="/runs/:id" element={<Detail />} />
          <Route path="/users" element={user.data.is_admin ? <Accounts /> : <Navigate to="/" />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </div>
  )
}
