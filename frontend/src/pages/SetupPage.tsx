/**
 * SetupPage — 3-step wizard that lets users configure Genie Workbench for
 * local development without hardcoded .env.local credentials.
 *
 * Step 1: Enter Databricks host + trigger OAuth login via Databricks CLI
 * Step 2: Pick warehouse, catalog, and LLM model from dropdowns
 * Step 3: Done — stores config in localStorage and calls onComplete()
 */

import { useState, useEffect, useRef } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { saveConfig } from "@/lib/config"

interface SetupPageProps {
  onComplete: () => void
}

interface Resources {
  warehouses: { id: string; name: string }[]
  catalogs: string[]
  models: { id: string; name: string }[]
}

type Step = "host" | "resources" | "done"

export function SetupPage({ onComplete }: SetupPageProps) {
  const [step, setStep] = useState<Step>("host")
  const [host, setHost] = useState("")
  const [token, setToken] = useState("")
  const [loginPending, setLoginPending] = useState(false)
  const [loginError, setLoginError] = useState("")
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const sessionIdRef = useRef<string | null>(null)
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const [resources, setResources] = useState<Resources | null>(null)
  const [resourcesError, setResourcesError] = useState("")
  const [warehouseId, setWarehouseId] = useState("")
  const [gsoCatalog, setGsoCatalog] = useState("")
  const [llmModel, setLlmModel] = useState("")

  // Clean up poll timer on unmount
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current)
    }
  }, [])

  // Fetch resources once we have a token + host
  useEffect(() => {
    if (step !== "resources" || !token || !host) return
    setResourcesError("")
    fetch("/api/setup/resources", {
      headers: {
        "x-forwarded-access-token": token,
        "x-databricks-host": host,
      },
    })
      .then((r) => {
        if (!r.ok) return r.json().then((e) => Promise.reject(e.detail || "Failed to load resources"))
        return r.json()
      })
      .then((data: Resources) => {
        setResources(data)
        if (data.warehouses.length > 0) setWarehouseId(data.warehouses[0].id ?? "")
        if (data.catalogs.length > 0) setGsoCatalog(data.catalogs[0])
        if (data.models.length > 0) setLlmModel(data.models[0].id)
      })
      .catch((err: unknown) => {
        setResourcesError(typeof err === "string" ? err : "Could not load workspace resources")
      })
  }, [step, token, host])

  function stopPolling() {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }

  async function handleStartLogin() {
    const trimmedHost = host.trim()
    if (!trimmedHost) {
      setLoginError("Please enter your Databricks workspace URL")
      return
    }
    if (!trimmedHost.startsWith("http")) {
      setLoginError("Host must start with https://")
      return
    }
    setLoginError("")
    setLoginPending(true)
    setAuthUrl(null)

    try {
      const res = await fetch("/api/setup/start-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: trimmedHost }),
      })
      const data = await res.json()
      if (!res.ok) {
        setLoginError(data.detail || "Failed to start login")
        setLoginPending(false)
        return
      }
      sessionIdRef.current = data.session_id
      if (data.auth_url) setAuthUrl(data.auth_url)

      // Poll every 2 seconds for up to 5 minutes
      pollTimerRef.current = setInterval(async () => {
        const sid = sessionIdRef.current
        if (!sid) return
        try {
          const statusRes = await fetch(`/api/setup/login-status/${sid}`)
          const status = await statusRes.json()
          if (status.status === "pending") return
          stopPolling()
          if (status.status === "complete") {
            setToken(status.token)
            setLoginPending(false)
            setStep("resources")
          } else {
            setLoginError(status.error || "Login failed")
            setLoginPending(false)
          }
        } catch {
          // Network hiccup — keep polling
        }
      }, 2000)
    } catch (err: unknown) {
      setLoginError(err instanceof Error ? err.message : "Network error")
      setLoginPending(false)
    }
  }

  function handleSaveConfig() {
    saveConfig({
      host: host.trim(),
      token,
      warehouseId,
      gsoCatalog,
      llmModel,
    })
    setStep("done")
    setTimeout(onComplete, 800)
  }

  return (
    <div className="min-h-screen bg-background text-primary flex items-center justify-center px-4">
      <div className="w-full max-w-md">
        {/* Logo */}
        <div className="flex items-center gap-3 mb-8">
          <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center flex-shrink-0">
            <svg viewBox="0 0 32 32" className="w-5 h-5">
              <svg x="6" y="6" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
              </svg>
              <path d="M7 2l1 3 3 1-3 1-1 3-1-3-3-1 3-1z" fill="white" />
            </svg>
          </div>
          <span className="text-lg font-display font-bold text-primary">Genie Workbench</span>
        </div>

        {/* Step indicator */}
        <div className="flex items-center gap-2 mb-6">
          {(["host", "resources", "done"] as Step[]).map((s, i) => (
            <div key={s} className="flex items-center gap-2">
              <div
                className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${
                  step === s
                    ? "bg-accent text-white"
                    : i < (["host", "resources", "done"] as Step[]).indexOf(step)
                    ? "bg-accent/30 text-accent"
                    : "bg-surface-secondary text-muted"
                }`}
              >
                {i + 1}
              </div>
              {i < 2 && <div className={`h-px w-8 ${i < (["host", "resources", "done"] as Step[]).indexOf(step) ? "bg-accent/30" : "bg-border"}`} />}
            </div>
          ))}
        </div>

        {/* Step 1: Host + OAuth */}
        {step === "host" && (
          <div className="rounded-xl border border-default bg-surface p-6 space-y-5">
            <div>
              <h1 className="text-xl font-bold text-primary">Connect to Databricks</h1>
              <p className="text-sm text-muted mt-1">Enter your workspace URL to authenticate via the Databricks CLI</p>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-secondary">Workspace URL</label>
              <Input
                type="url"
                placeholder="https://your-workspace.azuredatabricks.net"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                disabled={loginPending}
                onKeyDown={(e) => e.key === "Enter" && !loginPending && handleStartLogin()}
              />
            </div>

            {loginError && (
              <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
                {loginError}
              </div>
            )}

            {loginPending && (
              <div className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-3 space-y-2">
                <div className="flex items-center gap-2 text-sm text-accent">
                  <svg className="animate-spin w-4 h-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Waiting for browser login...
                </div>
                {authUrl && (
                  <p className="text-xs text-muted">
                    If your browser didn&apos;t open,{" "}
                    <a href={authUrl} target="_blank" rel="noopener noreferrer" className="text-accent underline">
                      click here
                    </a>
                  </p>
                )}
              </div>
            )}

            <Button
              className="w-full"
              onClick={handleStartLogin}
              disabled={loginPending || !host.trim()}
            >
              {loginPending ? "Authenticating..." : "Login with Databricks"}
            </Button>

            <p className="text-xs text-muted text-center">
              Requires the Databricks CLI. Install:{" "}
              <code className="bg-elevated px-1 rounded text-xs">brew install databricks/tap/databricks</code>
            </p>
          </div>
        )}

        {/* Step 2: Resource selection */}
        {step === "resources" && (
          <div className="rounded-xl border border-default bg-surface p-6 space-y-5">
            <div>
              <h1 className="text-xl font-bold text-primary">Configure Resources</h1>
              <p className="text-sm text-muted mt-1">Select the workspace resources to use</p>
            </div>

            {resourcesError && (
              <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
                {resourcesError}
              </div>
            )}

            {!resources && !resourcesError && (
              <div className="flex items-center gap-2 text-sm text-muted py-2">
                <svg className="animate-spin w-4 h-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Loading workspace resources...
              </div>
            )}

            {resources && (
              <>
                <div className="space-y-2">
                  <label className="text-sm font-medium text-secondary">SQL Warehouse</label>
                  {resources.warehouses.length === 0 ? (
                    <p className="text-sm text-muted">No warehouses found. You can add this later.</p>
                  ) : (
                    <select
                      className="w-full h-11 rounded-lg border border-default bg-surface px-4 py-2 text-sm text-primary focus:outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent"
                      value={warehouseId}
                      onChange={(e) => setWarehouseId(e.target.value)}
                    >
                      {resources.warehouses.map((wh) => (
                        <option key={wh.id} value={wh.id ?? ""}>
                          {wh.name} ({wh.id})
                        </option>
                      ))}
                    </select>
                  )}
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-secondary">Unity Catalog (for GSO)</label>
                  {resources.catalogs.length === 0 ? (
                    <Input
                      placeholder="e.g. main"
                      value={gsoCatalog}
                      onChange={(e) => setGsoCatalog(e.target.value)}
                    />
                  ) : (
                    <select
                      className="w-full h-11 rounded-lg border border-default bg-surface px-4 py-2 text-sm text-primary focus:outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent"
                      value={gsoCatalog}
                      onChange={(e) => setGsoCatalog(e.target.value)}
                    >
                      {resources.catalogs.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  )}
                </div>

                <div className="space-y-2">
                  <label className="text-sm font-medium text-secondary">LLM Model</label>
                  <select
                    className="w-full h-11 rounded-lg border border-default bg-surface px-4 py-2 text-sm text-primary focus:outline-none focus:ring-2 focus:ring-accent/20 focus:border-accent"
                    value={llmModel}
                    onChange={(e) => setLlmModel(e.target.value)}
                  >
                    {resources.models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                </div>

                <Button
                  className="w-full"
                  onClick={handleSaveConfig}
                  disabled={!warehouseId}
                >
                  Save and Continue
                </Button>
              </>
            )}

            {resourcesError && (
              <Button
                variant="secondary"
                className="w-full"
                onClick={() => {
                  saveConfig({
                    host: host.trim(),
                    token,
                    warehouseId: "",
                    gsoCatalog: "",
                    llmModel: "",
                  })
                  onComplete()
                }}
              >
                Skip and Continue Anyway
              </Button>
            )}
          </div>
        )}

        {/* Step 3: Done */}
        {step === "done" && (
          <div className="rounded-xl border border-default bg-surface p-6 text-center space-y-4">
            <div className="w-12 h-12 rounded-full bg-accent/10 flex items-center justify-center mx-auto">
              <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <h1 className="text-xl font-bold text-primary">All set!</h1>
            <p className="text-sm text-muted">Your Databricks credentials have been saved. Loading Genie Workbench...</p>
          </div>
        )}
      </div>
    </div>
  )
}
