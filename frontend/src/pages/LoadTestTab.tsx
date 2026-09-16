import { useState, useRef, useCallback } from "react"
import { Play, Square, Users, Clock, AlertTriangle, CheckCircle, XCircle, TrendingUp, Zap } from "lucide-react"
import { streamLoadTest } from "@/lib/api"
import type { LoadTestResult, LoadTestStepSummary } from "@/types"

interface Props {
  spaceId: string
}

type RunState = "idle" | "running" | "done" | "error"

function StatusIcon({ status }: { status: LoadTestResult["status"] }) {
  if (status === "success") return <CheckCircle className="w-4 h-4 text-success shrink-0" />
  if (status === "timeout") return <Clock className="w-4 h-4 text-warning shrink-0" />
  if (status === "rate_limited") return <Zap className="w-4 h-4 text-orange-500 shrink-0" />
  return <XCircle className="w-4 h-4 text-danger shrink-0" />
}

function ProgressBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0
  return (
    <div className="w-full h-1.5 bg-elevated rounded-full overflow-hidden">
      <div className="h-full bg-info rounded-full transition-all duration-300" style={{ width: `${pct}%` }} />
    </div>
  )
}

function ms(v: number) {
  return v > 0 ? `${v.toLocaleString()}ms` : "—"
}

function fmtTs(iso: string) {
  if (!iso) return "—"
  const d = new Date(iso)
  return isNaN(d.getTime()) ? "—" : d.toLocaleString()
}

function fmtDuration(msVal: number) {
  if (msVal <= 0) return "—"
  const s = msVal / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${Math.round(s % 60)}s`
}

/** Color the p95 cell by latency tier so degradation is visible at a glance. */
function latencyClass(v: number) {
  if (v <= 0) return "text-muted"
  if (v < 5000) return "text-success"
  if (v < 15000) return "text-warning"
  return "text-danger"
}

const INPUT_CLASS =
  "w-full rounded-lg border border-default bg-elevated px-3 py-2 text-sm text-primary " +
  "placeholder:text-slate-400 focus:outline-none focus:border-info/60 disabled:opacity-40"

export function LoadTestTab({ spaceId }: Props) {
  const [startUsers, setStartUsers] = useState(2)
  const [maxUsers, setMaxUsers] = useState(10)
  const [step, setStep] = useState(2)
  const [customQuestion, setCustomQuestion] = useState("")

  const [runState, setRunState] = useState<RunState>("idle")
  const [levels, setLevels] = useState<number[]>([])
  const [currentStep, setCurrentStep] = useState<{ index: number; users: number } | null>(null)
  const [stepProgress, setStepProgress] = useState({ completed: 0, total: 0 })
  const [stepSummaries, setStepSummaries] = useState<LoadTestStepSummary[]>([])
  const [liveResults, setLiveResults] = useState<LoadTestResult[]>([])
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [timing, setTiming] = useState<{ startedAt: string; finishedAt: string; durationMs: number } | null>(null)
  const stopRef = useRef<(() => void) | null>(null)

  // Preview of the concurrency waves for the current inputs.
  const previewLevels = (() => {
    const s = Math.max(1, Math.min(startUsers, 50))
    const m = Math.max(s, Math.min(maxUsers, 50))
    const st = Math.max(1, step)
    const out: number[] = []
    for (let u = s; u <= m; u += st) out.push(u)
    if (out.length === 0 || out[out.length - 1] !== m) out.push(m)
    return out
  })()

  const start = useCallback(() => {
    setRunState("running")
    setLevels([])
    setStepSummaries([])
    setLiveResults([])
    setCurrentStep(null)
    setStepProgress({ completed: 0, total: 0 })
    setErrorMsg(null)
    setTiming(null)

    const stop = streamLoadTest(
      spaceId,
      { startUsers, maxUsers, step, question: customQuestion.trim() || null },
      {
        onStart: (lvls, _total, startedAt) => {
          setLevels(lvls)
          setTiming({ startedAt, finishedAt: "", durationMs: 0 })
        },
        onStepStart: (index, users) => {
          setCurrentStep({ index, users })
          setStepProgress({ completed: 0, total: users })
          setLiveResults([])
        },
        onProgress: (_stepIndex, _users, completed, total, result) => {
          setStepProgress({ completed, total })
          setLiveResults(prev => [...prev, result])
        },
        onStepSummary: (summary) => {
          setStepSummaries(prev => [...prev, summary])
        },
        onDone: (_steps, t) => {
          setRunState("done")
          setCurrentStep(null)
          setTiming(t)
        },
        onError: (msg) => { setErrorMsg(msg); setRunState("error") },
      }
    )
    stopRef.current = stop
  }, [spaceId, startUsers, maxUsers, step, customQuestion])

  const stop = useCallback(() => {
    stopRef.current?.()
    setRunState("idle")
    setCurrentStep(null)
  }, [])

  const isRunning = runState === "running"
  const peak = previewLevels[previewLevels.length - 1] ?? maxUsers

  return (
    <div className="space-y-6">
      {/* Config panel */}
      <div className="rounded-xl border border-default bg-surface p-5 space-y-4">
        <h3 className="text-sm font-semibold text-secondary flex items-center gap-2">
          <TrendingUp className="w-4 h-4 text-info" />
          Stepped Ramp Configuration
        </h3>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs text-muted mb-1.5">Start users</label>
            <input
              type="number" min={1} max={50} value={startUsers} disabled={isRunning}
              onChange={e => setStartUsers(Math.min(50, Math.max(1, parseInt(e.target.value) || 1)))}
              className={INPUT_CLASS}
            />
          </div>
          <div>
            <label className="block text-xs text-muted mb-1.5">Max users</label>
            <input
              type="number" min={1} max={50} value={maxUsers} disabled={isRunning}
              onChange={e => setMaxUsers(Math.min(50, Math.max(1, parseInt(e.target.value) || 1)))}
              className={INPUT_CLASS}
            />
          </div>
          <div>
            <label className="block text-xs text-muted mb-1.5">Step</label>
            <input
              type="number" min={1} max={50} value={step} disabled={isRunning}
              onChange={e => setStep(Math.min(50, Math.max(1, parseInt(e.target.value) || 1)))}
              className={INPUT_CLASS}
            />
          </div>
          <div>
            <label className="block text-xs text-muted mb-1.5">Custom question <span className="text-muted/70">(optional)</span></label>
            <input
              type="text" placeholder="benchmark questions by default"
              value={customQuestion} disabled={isRunning}
              onChange={e => setCustomQuestion(e.target.value)}
              className={INPUT_CLASS}
            />
          </div>
        </div>

        {/* Ramp preview */}
        <div className="flex items-center gap-2 flex-wrap text-xs">
          <span className="text-muted">Waves:</span>
          {previewLevels.map((lvl, i) => (
            <span
              key={i}
              className={`px-2 py-0.5 rounded-full border font-mono ${
                currentStep?.index === i && isRunning
                  ? "border-info/60 bg-info/20 text-info"
                  : stepSummaries.some(s => s.step_index === i)
                    ? "border-success/30 bg-success/10 text-success"
                    : "border-default bg-elevated text-muted"
              }`}
            >
              {lvl}
            </span>
          ))}
          <span className="text-muted">· {previewLevels.reduce((a, b) => a + b, 0)} total requests</span>
        </div>

        <div className="flex items-center gap-3">
          {!isRunning ? (
            <button onClick={start} className="flex items-center gap-2 rounded-lg bg-info hover:brightness-110 px-4 py-2 text-sm font-medium text-white transition-colors">
              <Play className="w-4 h-4" />
              Run Ramp Test
            </button>
          ) : (
            <button onClick={stop} className="flex items-center gap-2 rounded-lg bg-danger hover:brightness-110 px-4 py-2 text-sm font-medium text-white transition-colors">
              <Square className="w-4 h-4" />
              Stop
            </button>
          )}

          {isRunning && currentStep && (
            <div className="flex-1 space-y-1">
              <div className="flex justify-between text-xs text-muted">
                <span>Wave {currentStep.index + 1}/{levels.length || previewLevels.length} · {currentStep.users} concurrent · {stepProgress.completed}/{stepProgress.total}</span>
                <span>{Math.round((stepProgress.completed / Math.max(stepProgress.total, 1)) * 100)}%</span>
              </div>
              <ProgressBar value={stepProgress.completed} max={stepProgress.total} />
            </div>
          )}
        </div>

        {peak > 20 && (
          <p className="flex items-center gap-2 text-xs text-warning">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            Peak concurrency {peak} may hit Genie API rate limits.
          </p>
        )}
      </div>

      {/* Timing */}
      {timing && (
        <div className="rounded-xl border border-default bg-surface px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs">
          <span className="flex items-center gap-1.5 text-secondary">
            <Clock className="w-3.5 h-3.5 text-info" />
            <span className="text-muted">Started:</span> <span className="font-mono text-primary">{fmtTs(timing.startedAt)}</span>
          </span>
          <span className="text-secondary">
            <span className="text-muted">Ended:</span> <span className="font-mono text-primary">{timing.finishedAt ? fmtTs(timing.finishedAt) : (isRunning ? "running…" : "—")}</span>
          </span>
          <span className="text-secondary">
            <span className="text-muted">Duration:</span> <span className="font-mono text-primary">{fmtDuration(timing.durationMs)}</span>
          </span>
        </div>
      )}

      {/* Error */}
      {runState === "error" && errorMsg && (
        <div className="rounded-xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">{errorMsg}</div>
      )}

      {/* Per-wave results table */}
      {stepSummaries.length > 0 && (
        <div className="rounded-xl border border-default bg-surface overflow-hidden">
          <div className="px-4 py-3 border-b border-default flex items-center justify-between">
            <h3 className="text-sm font-semibold text-secondary flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-info" />
              Latency by Concurrency
            </h3>
            <span className="text-xs text-muted">{stepSummaries.length} waves</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-default text-muted">
                  <th className="px-4 py-2 text-left">Concurrent</th>
                  <th className="px-4 py-2 text-right">Success</th>
                  <th className="px-4 py-2 text-right">Throttled</th>
                  <th className="px-4 py-2 text-right">p50</th>
                  <th className="px-4 py-2 text-right">p95</th>
                  <th className="px-4 py-2 text-right">p99</th>
                  <th className="px-4 py-2 text-right">avg</th>
                  <th className="px-4 py-2 text-right">min</th>
                  <th className="px-4 py-2 text-right">max</th>
                </tr>
              </thead>
              <tbody>
                {stepSummaries.map(s => {
                  const rate = s.total > 0 ? Math.round((s.success / s.total) * 100) : 0
                  return (
                    <tr key={s.step_index} className="border-b border-default hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
                      <td className="px-4 py-2 font-mono text-primary font-semibold">{s.concurrent_users}</td>
                      <td className={`px-4 py-2 text-right font-mono ${rate >= 90 ? "text-success" : rate >= 70 ? "text-warning" : "text-danger"}`}>
                        {s.success}/{s.total} ({rate}%)
                      </td>
                      <td className={`px-4 py-2 text-right font-mono ${s.rate_limited > 0 ? "text-orange-500 font-semibold" : "text-muted"}`}>
                        {s.rate_limited > 0 ? s.rate_limited : "—"}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-secondary">{ms(s.p50_ms)}</td>
                      <td className={`px-4 py-2 text-right font-mono font-semibold ${latencyClass(s.p95_ms)}`}>{ms(s.p95_ms)}</td>
                      <td className="px-4 py-2 text-right font-mono text-secondary">{ms(s.p99_ms)}</td>
                      <td className="px-4 py-2 text-right font-mono text-muted">{ms(s.avg_ms)}</td>
                      <td className="px-4 py-2 text-right font-mono text-muted">{ms(s.min_ms)}</td>
                      <td className="px-4 py-2 text-right font-mono text-muted">{ms(s.max_ms)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Live per-request feed for the current wave */}
      {isRunning && liveResults.length > 0 && (
        <div className="rounded-xl border border-default bg-surface overflow-hidden">
          <div className="px-4 py-3 border-b border-default flex items-center justify-between">
            <h3 className="text-sm font-semibold text-secondary">Current Wave — {currentStep?.users} concurrent</h3>
            <span className="text-xs text-muted">{liveResults.length} done</span>
          </div>
          <div className="overflow-x-auto max-h-64 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-surface">
                <tr className="border-b border-default text-muted">
                  <th className="px-4 py-2 text-left w-8">#</th>
                  <th className="px-4 py-2 text-left">Question</th>
                  <th className="px-4 py-2 text-right">Latency</th>
                  <th className="px-4 py-2 text-right">TTFR</th>
                  <th className="px-4 py-2 text-center">Status</th>
                </tr>
              </thead>
              <tbody>
                {liveResults.map(r => (
                  <tr key={r.request_id} className="border-b border-default">
                    <td className="px-4 py-2 text-muted font-mono">{r.request_id}</td>
                    <td className="px-4 py-2 text-secondary max-w-xs truncate">{r.question}</td>
                    <td className="px-4 py-2 text-right font-mono text-primary">{r.status !== "error" ? ms(r.latency_ms) : "—"}</td>
                    <td className="px-4 py-2 text-right font-mono text-muted">{r.ttfr_ms != null ? ms(r.ttfr_ms) : "—"}</td>
                    <td className="px-4 py-2">
                      <div className="flex items-center justify-center gap-1.5">
                        <StatusIcon status={r.status} />
                        {r.status === "error" && r.detail && (
                          <span className="text-danger truncate max-w-[140px]" title={r.detail}>{r.detail}</span>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Idle empty state */}
      {runState === "idle" && stepSummaries.length === 0 && (
        <div className="rounded-xl border border-default bg-surface py-12 text-center">
          <Users className="w-8 h-8 text-muted mx-auto mb-3" />
          <p className="text-sm text-muted">Configure a stepped ramp and run to measure how latency scales with concurrency</p>
          <p className="text-xs text-muted mt-1">Runs sequential waves (e.g. 2 → 4 → 6 → 8 → 10 users) and reports p50 / p95 / p99 per wave</p>
        </div>
      )}
    </div>
  )
}
