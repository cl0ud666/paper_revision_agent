import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, BookOpen } from 'lucide-react'
import { api, setCsrf, type User } from './api'

export function Login() {
  const client = useQueryClient()
  const [register, setRegister] = useState(false),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [confirm, setConfirm] = useState('')
  const options = useQuery({
    queryKey: ['auth-options'],
    queryFn: () => api<{ registration_open: boolean }>('/auth/options'),
    staleTime: 30000,
  })
  const submit = useMutation({
    mutationFn: () =>
      api<User>(register ? '/auth/register' : '/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      }),
    onSuccess: (value) => {
      setCsrf(value.csrf_token || '')
      client.setQueryData(['me'], value)
    },
  })
  return (
    <div className="login-page">
      <section className="login-story">
        <div className="brand">
          <BookOpen size={32} />
          <span>
            paperfixer<small>PAPER WORKSPACE</small>
          </span>
        </div>
        <div>
          <div className="tagline-copy">
            <h1 className="refined-tagline">
              从语句到逻辑，
              <br />
              paperfixer帮你精修论文
            </h1>
            <p className="tagline-en">
              From sentences to logic, PaperFixer helps you polish your paper
            </p>
          </div>
          <div className="story-steps">
            <span>01 审查</span>
            <span>02 确认</span>
            <span>03 修订</span>
          </div>
        </div>
        <small>面向研究者的论文修改智能体</small>
      </section>
      <section className="login-form">
        <div className="login-card">
          <span className="eyebrow">PAPER WORKSPACE</span>
          <h2>{register ? '创建你的账号' : '登录你的工作台'}</h2>
          {!register && <p className="muted">使用账号和密码登录，继续处理你的论文。</p>}
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (!register || password === confirm) submit.mutate()
            }}
          >
            <label>
              账号
              <input
                autoComplete="username"
                required
                minLength={3}
                maxLength={80}
                value={username}
                disabled={submit.isPending}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="字母、数字或 _ . @ -"
              />
            </label>
            <label>
              密码
              <input
                type="password"
                autoComplete={register ? 'new-password' : 'current-password'}
                required
                minLength={register ? 6 : 1}
                maxLength={128}
                value={password}
                disabled={submit.isPending}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={register ? '至少 6 位' : '输入密码'}
              />
            </label>
            {register && (
              <>
                <label>
                  确认密码
                  <input
                    type="password"
                    autoComplete="new-password"
                    required
                    value={confirm}
                    disabled={submit.isPending}
                    aria-describedby={
                      confirm && password !== confirm ? 'password-mismatch' : undefined
                    }
                    onChange={(e) => setConfirm(e.target.value)}
                  />
                </label>
                {confirm && password !== confirm && (
                  <p id="password-mismatch" className="error">
                    两次密码不一致
                  </p>
                )}
              </>
            )}
            {submit.error && (
              <p role="alert" className="error">
                {submit.error.message}
              </p>
            )}
            <button
              className="primary full"
              disabled={
                submit.isPending ||
                (register && (!options.data?.registration_open || password !== confirm))
              }
            >
              {submit.isPending ? '正在验证…' : register ? '注册并进入工作台' : '进入工作台'}
              <ArrowRight size={17} />
            </button>
          </form>
          <p className="login-foot">
            <button
              className="text-button"
              disabled={submit.isPending || (!register && !options.data?.registration_open)}
              onClick={() => {
                setRegister(!register)
                setPassword('')
                setConfirm('')
                submit.reset()
              }}
            >
              {register ? '已有账号？返回登录' : '没有账号？立即注册'}
            </button>
          </p>
          {options.error && (
            <p role="alert" className="error">
              暂时无法读取注册配置，请刷新后再试。
            </p>
          )}
          {options.data && !options.data.registration_open && (
            <p className="muted small">注册暂未开放，请联系管理员。</p>
          )}
          <p className="muted small">请妥善保存账号和密码；忘记密码需联系管理员。</p>
        </div>
      </section>
    </div>
  )
}
