export type User = {
  id: string
  username: string
  is_admin: boolean
  active: boolean
  daily_run_limit: number
  free_quota?: { total: number; completed: number; reserved: number; remaining: number }
  csrf_token?: string
  demo?: boolean
}
export type Issue = {
  id: number
  problem: string
  location?: string
  span_label?: string
  evidence?: string
  options?: string[]
  display?: { problem: string; position: string; display_options: string[] }
}
export type PreviewRect = { page: number; x: number; y: number; width: number; height: number }
export type Preview = {
  fingerprint: string
  error?: string
  pages: { number: number; width: number; height: number }[]
}
export type Unfinished = {
  title: string
  reason: string
  position?: string
  kind?: string
  blocked_reason?: string
  suggestion?: string
  evidence?: string
  manual_id?: string
}
export type Patch = {
  patch_id: string
  patch_ids?: string[]
  number?: number
  rects?: PreviewRect[]
  position?: string
  span_label?: string
  count?: number
  problem?: string
  before?: string
  replace?: string
  rationale?: string
  status?: string
  conflict_reason?: string
  action?: string
  params?: { source_verification?: { verification?: string; source_url?: string } }
  review_note?: string
}
export type HumanRequest = {
  type: string
  visual?: boolean
  preview?: Preview
  unavailable_count?: number
  manual_items?: Unfinished[]
  instruction?: string
  issues?: Issue[]
  patches?: Patch[]
  defaults?: { excluded_ids?: string[] }
}
export type Run = {
  id: string
  document_name: string
  request: string
  status: string
  stage: string
  version: number
  free_quota?: boolean
  pending: { id: string; request: HumanRequest } | null
  summary: {
    changes?: number
    written?: number
    unfinished?: Unfinished[]
    excluded?: number
    issues?: number
    review?: string
    review_summary?: string
    warnings?: string[]
  }
  error: string
  has_result: boolean
  has_report: boolean
  created_at: string
  updated_at: string
}
export type Event = { id: number; kind: string; message: string; created_at: string }
let csrfToken = ''
export function setCsrf(token: string) {
  csrfToken = token
}
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData))
    headers.set('Content-Type', 'application/json')
  if (options.method && !['GET', 'HEAD'].includes(options.method))
    headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: 'same-origin',
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(
      response.status,
      typeof body.detail === 'string' ? body.detail : '请求失败，请稍后再试',
    )
  }
  return response.json() as Promise<T>
}
export const labels: Record<string, string> = {
  queued: '排队中',
  running: '处理中',
  waiting_user: '待你确认',
  completed: '已完成',
  failed: '处理失败',
  cancelled: '已取消',
  cancel_requested: '正在停止',
}
export const stages: Record<string, string> = {
  queued: '等待处理',
  planning: '理解修改要求',
  orchestrator: '理解修改要求',
  audit: '审查论文',
  revision: '生成修改',
  review: '复核修改',
  waiting_user: '等待你的选择',
  done: '文件已就绪',
}
export const active = (status: string) => ['queued', 'running', 'cancel_requested'].includes(status)
export const date = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
