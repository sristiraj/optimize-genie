/**
 * Side-by-side comparison of two optimization runs — scores, champion config
 * diff, and applied patches. Reached from the "Compare" action in
 * RunHistoryTable (History tab).
 */
import { useEffect, useState } from "react"
import { ArrowLeft } from "lucide-react"
import ReactDiffViewer, { DiffMethod } from "react-diff-viewer-continued"
import { Badge } from "@/components/ui/badge"
import { diffViewerStyles } from "@/lib/diffViewerStyles"
import { useTheme } from "@/hooks/useTheme"
import {
  getAutoOptimizeRun,
  getAutoOptimizeIterations,
  getAutoOptimizePatches,
} from "@/lib/api"
import type { GSOPipelineRun, GSOIterationResult, GSOPatch } from "@/types"

interface RunCompareViewProps {
  runIdA: string
  runIdB: string
  onBack: () => void
}

const STATUS_VARIANT: Record<string, "default" | "success" | "warning" | "danger" | "info" | "secondary"> = {
  CONVERGED: "success",
  APPLIED: "success",
  STALLED: "warning",
  MAX_ITERATIONS: "warning",
  FAILED: "danger",
  CANCELLED: "secondary",
  DISCARDED: "secondary",
  SKIPPED: "warning",
  IN_PROGRESS: "info",
  RUNNING: "info",
  QUEUED: "secondary",
}

const LEVER_NAMES: Record<number, string> = {
  0: "genie_config",
  1: "Tables & Columns",
  2: "Metric Views",
  3: "Table-Valued Functions",
  4: "Join Specifications",
  5: "Instructions & Examples",
  6: "SQL Expressions",
}

function formatPct(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(1)}%`
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—"
  const d = new Date(value)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })
}

/** Picks the config to represent a run's outcome: the champion iteration's
 * config if one exists, otherwise the highest-numbered ("latest") full
 * iteration, falling back to the baseline (iteration 0). */
function pickRepresentativeConfig(iterations: GSOIterationResult[]): string {
  const full = iterations.filter((it) => String(it.eval_scope ?? "full").toLowerCase() === "full")
  const champion = full.find((it) => it.is_champion)
  const target = champion ?? [...full].sort((a, b) => b.iteration - a.iteration)[0]
  if (!target?.config_json) return ""
  try {
    return JSON.stringify(JSON.parse(target.config_json), null, 2)
  } catch {
    return target.config_json
  }
}

function RunSummaryCard({ run, label }: { run: GSOPipelineRun | null; label: string }) {
  if (!run) {
    return (
      <div className="rounded-xl border border-default bg-surface p-4">
        <p className="text-xs font-semibold uppercase tracking-widest text-muted">{label}</p>
        <p className="mt-2 text-sm text-muted animate-pulse">Loading...</p>
      </div>
    )
  }
  const improvement =
    run.baselineScore != null && run.optimizedScore != null
      ? run.optimizedScore - run.baselineScore
      : null
  return (
    <div className="rounded-xl border border-default bg-surface p-4 space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-widest text-muted">{label}</p>
        <Badge variant={STATUS_VARIANT[run.status] ?? "secondary"}>{run.status}</Badge>
      </div>
      <p className="text-xs text-muted">{formatDate(run.startedAt)}</p>
      <div className="grid grid-cols-3 gap-2 pt-1">
        <div>
          <p className="text-[10px] uppercase text-muted">Baseline</p>
          <p className="text-sm font-semibold text-primary">{formatPct(run.baselineScore)}</p>
        </div>
        <div>
          <p className="text-[10px] uppercase text-muted">Optimized</p>
          <p className="text-sm font-semibold text-indigo-600 dark:text-indigo-400">
            {formatPct(run.optimizedScore)}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase text-muted">Δ</p>
          <p
            className={`text-sm font-semibold ${
              improvement == null || improvement === 0
                ? "text-primary"
                : improvement > 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-red-500"
            }`}
          >
            {improvement == null ? "—" : `${improvement > 0 ? "+" : ""}${improvement.toFixed(1)}%`}
          </p>
        </div>
      </div>
    </div>
  )
}

function PatchesColumn({ label, patches, loading }: { label: string; patches: GSOPatch[]; loading: boolean }) {
  return (
    <div className="rounded-xl border border-default bg-surface p-4">
      <p className="mb-3 text-xs font-semibold uppercase tracking-widest text-muted">{label}</p>
      {loading ? (
        <p className="text-sm text-muted animate-pulse py-4 text-center">Loading patches...</p>
      ) : patches.length === 0 ? (
        <p className="text-sm text-muted py-4 text-center">No patches applied</p>
      ) : (
        <ul className="space-y-2 max-h-[400px] overflow-y-auto">
          {patches.map((patch, i) => (
            <li key={i} className="rounded-lg border border-default p-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-primary">
                  {LEVER_NAMES[patch.lever ?? 0] ?? `Lever ${patch.lever}`}
                </span>
                <Badge
                  variant={
                    patch.status?.toLowerCase() === "accepted" || patch.status?.toLowerCase() === "applied"
                      ? "success"
                      : patch.status?.toLowerCase() === "rejected"
                        ? "danger"
                        : "secondary"
                  }
                >
                  {patch.status}
                </Badge>
              </div>
              <p className="mt-1 truncate text-muted" title={patch.target_object}>
                {patch.patch_type} · {patch.target_object}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function RunCompareView({ runIdA, runIdB, onBack }: RunCompareViewProps) {
  const { isDark } = useTheme()
  const [runA, setRunA] = useState<GSOPipelineRun | null>(null)
  const [runB, setRunB] = useState<GSOPipelineRun | null>(null)
  const [configA, setConfigA] = useState("")
  const [configB, setConfigB] = useState("")
  const [patchesA, setPatchesA] = useState<GSOPatch[]>([])
  const [patchesB, setPatchesB] = useState<GSOPatch[]>([])
  const [patchesLoading, setPatchesLoading] = useState(true)
  const [configLoading, setConfigLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    getAutoOptimizeRun(runIdA).then((r) => !cancelled && setRunA(r)).catch(() => {})
    getAutoOptimizeRun(runIdB).then((r) => !cancelled && setRunB(r)).catch(() => {})

    Promise.all([getAutoOptimizeIterations(runIdA), getAutoOptimizeIterations(runIdB)])
      .then(([a, b]) => {
        if (cancelled) return
        setConfigA(pickRepresentativeConfig(a))
        setConfigB(pickRepresentativeConfig(b))
      })
      .catch(() => {})
      .finally(() => !cancelled && setConfigLoading(false))

    Promise.all([getAutoOptimizePatches(runIdA), getAutoOptimizePatches(runIdB)])
      .then(([a, b]) => {
        if (cancelled) return
        setPatchesA(a)
        setPatchesB(b)
      })
      .catch(() => {})
      .finally(() => !cancelled && setPatchesLoading(false))

    return () => {
      cancelled = true
    }
  }, [runIdA, runIdB])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <button
          onClick={onBack}
          className="p-2 rounded-lg border border-default hover:bg-elevated text-muted hover:text-primary transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <p className="text-lg font-semibold text-primary">Compare optimization runs</p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <RunSummaryCard run={runA} label="Run A" />
        <RunSummaryCard run={runB} label="Run B" />
      </div>

      <div className="rounded-xl border border-default overflow-hidden">
        <div className="flex border-b border-default">
          <div className="flex-1 px-4 py-2 bg-elevated text-sm font-medium text-secondary">
            Champion config — Run A
          </div>
          <div className="flex-1 px-4 py-2 bg-elevated text-sm font-medium text-secondary border-l border-default">
            Champion config — Run B
          </div>
        </div>
        {configLoading ? (
          <p className="text-sm text-muted animate-pulse text-center py-8">Loading configs...</p>
        ) : !configA && !configB ? (
          <p className="text-sm text-muted text-center py-8">No captured config available for either run.</p>
        ) : (
          <ReactDiffViewer
            oldValue={configA}
            newValue={configB}
            splitView={true}
            useDarkTheme={isDark}
            compareMethod={DiffMethod.WORDS}
            styles={diffViewerStyles}
            hideLineNumbers={false}
            showDiffOnly={false}
          />
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <PatchesColumn label="Patches applied — Run A" patches={patchesA} loading={patchesLoading} />
        <PatchesColumn label="Patches applied — Run B" patches={patchesB} loading={patchesLoading} />
      </div>
    </div>
  )
}
