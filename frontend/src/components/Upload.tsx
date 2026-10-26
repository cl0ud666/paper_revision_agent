import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, UploadCloud, X } from 'lucide-react'
import { api, type Run } from '../api'
import { ErrorText } from './Feedback'

export function Upload({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate(),
    client = useQueryClient()
  const [file, setFile] = useState<File | null>(null),
    [request, setRequest] = useState('全面检查并修改这篇论文'),
    [rules, setRules] = useState('不要修改实验结果与数据\n保持作者原有表达风格')
  const create = useMutation({
    mutationFn: async () => {
      const form = new FormData()
      form.set('file', file!)
      form.set('request', request)
      form.set(
        'rules',
        JSON.stringify(
          rules
            .split('\n')
            .map((r) => r.trim())
            .filter(Boolean),
        ),
      )
      return api<Run>('/runs', { method: 'POST', body: form })
    },
    onSuccess: (run) => {
      client.invalidateQueries({ queryKey: ['runs'] })
      client.invalidateQueries({ queryKey: ['me'] })
      navigate(`/runs/${run.id}`)
      onClose()
    },
  })
  return (
    <div className="modal-backdrop">
      <section className="modal" role="dialog" aria-modal="true" aria-label="上传论文">
        <div className="section-title">
          <h2>准备你的论文</h2>
          <button
            className="icon-button"
            aria-label="关闭上传"
            disabled={create.isPending}
            onClick={onClose}
          >
            <X size={20} />
          </button>
        </div>
        <p className="muted">提交后会在后台处理，进展会保存在任务详情中。</p>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <label className="upload-zone">
            <UploadCloud size={34} />
            <strong>{file ? file.name : '点击选择 Word 论文'}</strong>
            <span>DOCX / DOC / WPS / RTF / ODT · 最大 20 MB</span>
            <input
              type="file"
              accept=".docx,.doc,.wps,.rtf,.odt"
              aria-label="选择论文文件"
              required
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </label>
          <p className="muted small">
            非 DOCX 文件会在后台转换，原稿保留，输出统一为 DOCX。仅支持 Word 兼容的 WPS
            文件；复杂排版可能变化，建议优先上传 DOCX。
          </p>
          <label>
            修改目标
            <textarea
              value={request}
              onChange={(e) => setRequest(e.target.value)}
              required
              maxLength={4000}
              rows={2}
            />
          </label>
          <label>
            需要遵守的要求<small>每行一条</small>
            <textarea value={rules} onChange={(e) => setRules(e.target.value)} rows={3} />
          </label>
          <ErrorText error={create.error} />
          <button className="primary full" disabled={!file || create.isPending}>
            {create.isPending ? '正在保存论文…' : '提交并开始处理'}
            <ArrowRight size={17} />
          </button>
        </form>
      </section>
    </div>
  )
}
