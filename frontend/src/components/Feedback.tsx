import { Loader2 } from 'lucide-react'
import { labels } from '../api'

export function ErrorText({ error }: { error: unknown }) {
  return error ? (
    <div className="error" role="alert">
      {error instanceof Error ? error.message : '操作失败，请稍后再试'}
    </div>
  ) : null
}
export const WAIT_MESSAGE = '处理需要一些时间，请稍等。'

export function Status({ value }: { value: string }) {
  return (
    <span className={`status ${value}`}>
      {value === 'running' ? (
        <>
          <Loader2 size={12} className="spin" />
          {WAIT_MESSAGE}
        </>
      ) : (
        labels[value] || value
      )}
    </span>
  )
}
