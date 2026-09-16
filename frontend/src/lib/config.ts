/**
 * Setup wizard config — persisted to localStorage so users only have to
 * authenticate once.  The stored token and host are injected as request
 * headers by api.ts for every backend call.
 */

const CONFIG_KEY = "genie-workbench-config"

export interface WorkbenchConfig {
  host: string
  token: string
  warehouseId: string
  gsoCatalog: string
  llmModel: string
}

export function getConfig(): WorkbenchConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY)
    if (!raw) return null
    return JSON.parse(raw) as WorkbenchConfig
  } catch {
    return null
  }
}

export function saveConfig(config: WorkbenchConfig): void {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config))
}

export function clearConfig(): void {
  localStorage.removeItem(CONFIG_KEY)
}

export function isConfigured(): boolean {
  const c = getConfig()
  return !!(c?.host && c?.token && c?.warehouseId)
}
