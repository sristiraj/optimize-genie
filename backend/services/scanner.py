"""IQ scoring service — thin wrapper around the GSO iq_scan package.

Historical note: the pure scoring logic (``calculate_score``,
``get_maturity_label``, ``_check``, ``_SQL_IN_TEXT_RE``, ``CONFIG_CHECK_COUNT``)
lived in this module until it was extracted to
``genie_space_optimizer.iq_scan.scoring`` so the GSO optimizer preflight can
share a single source of truth. This module now owns only the backend-specific
IO: UC metadata enrichment, Lakebase persistence, and the async
``scan_space`` orchestration.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from databricks.sdk.errors import NotFound

from backend.services.genie_client import get_genie_space, get_serialized_space
from backend.services.lakebase import save_scan_result, get_latest_score, get_latest_optimization_run

# Re-exported from the GSO iq_scan package so existing import paths keep working.
from genie_space_optimizer.iq_scan.scoring import (  # noqa: F401
    CONFIG_CHECK_COUNT,
    _SQL_IN_TEXT_RE,
    _check,
    calculate_score,
    get_maturity_label,
)

logger = logging.getLogger(__name__)


# Terminal GSO run statuses whose ``best_accuracy`` is meaningful for the IQ
# header's "% benchmark accuracy". Subset of auto_optimize._TERMINAL_RUN_STATUSES.
#
# APPLIED is included: applying an optimization flips the run status to APPLIED
# (integration/apply.py) while preserving best_accuracy, and the applied
# champion IS the live config — so its measured accuracy is exactly what the
# header should report. Omitting it was the "header shows stale pre-GSO score
# after a successful apply" bug: the applied 90% run was skipped and the scan
# fell back to the legacy optimization_runs baseline.
#
# DISCARDED is deliberately EXCLUDED: a discard reverts the live space config,
# so the discarded run's accuracy no longer describes what's deployed — the
# header should fall back to baseline rather than claim it.
# FAILED / CANCELLED are excluded because best_accuracy is absent or unreliable.
_GSO_TERMINAL = {"CONVERGED", "STALLED", "MAX_ITERATIONS", "APPLIED"}

# Shared thread pool for UC metadata fetches — avoids per-scan pool churn.
_uc_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="uc-enrich")


# ── Live-config fingerprinting (cross-run champion) ────────────────────────────

def _inner_serialized_space(cfg) -> dict:
    """Return the inner serialized_space dict from any wrapper shape."""
    import json as _json
    if isinstance(cfg, str):
        try:
            cfg = _json.loads(cfg)
        except Exception:
            return {}
    if not isinstance(cfg, dict):
        return {}
    for key in ("serialized_space", "_parsed_space"):
        v = cfg.get(key)
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except Exception:
                pass
    return cfg


def config_fingerprint(cfg) -> str:
    """Stable identity of a Genie config's serialized_space.

    Excludes the ``benchmarks`` block (regenerated/mutated every run) and any
    runtime ``_``-prefixed keys, so the fingerprint identifies the *agent config*
    itself — independent of which benchmark set it was scored against. Two runs
    that left the space in the same configured state produce the same hash.
    """
    import hashlib
    import json as _json
    ss = _inner_serialized_space(cfg)
    stripped = {k: v for k, v in ss.items() if not k.startswith("_") and k != "benchmarks"}
    return hashlib.sha256(
        _json.dumps(stripped, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _pct_to_fraction(value) -> float | None:
    """GSO stores accuracy as 0-100; normalize to 0.0-1.0."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 1.0 else v


async def _resolve_live_champion(space_id: str) -> dict | None:
    """Match the live config fingerprint to a stored iteration → the cross-run champion.

    Fetches the live config via the SAME path that produced the stored snapshots
    (``fetch_space_config``) so fingerprints are comparable — the backend's
    ``get_serialized_space`` returns a different shape, and the scanner further
    mutates it with UC enrichment, so neither can be fingerprinted for a match.

    Returns a dict with the LIVE config's accuracy (if a snapshot matched), its
    fingerprint, and the best config ever produced across runs, and records the
    champion pointer in Delta. ``None`` when GSO storage isn't configured.
    """
    from backend.services.request_config import get_request_config
    rc = get_request_config()
    catalog = rc.gso_catalog or os.environ.get("GSO_CATALOG", "")
    schema = os.environ.get("GSO_SCHEMA", "genie_space_optimizer")
    wh_id = rc.warehouse_id or os.environ.get("GSO_WAREHOUSE_ID") or os.environ.get("SQL_WAREHOUSE_ID", "")
    if not (catalog and wh_id):
        return None

    def _work():
        from genie_space_optimizer.common.warehouse import sql_warehouse_query
        from genie_space_optimizer.common.genie_client import fetch_space_config
        from backend.services.auth import get_service_principal_client
        from backend.services import delta_store

        def _q(s: str) -> str:
            return f"`{s.replace('`', '``')}`"

        ws = get_service_principal_client()
        # Fingerprint the live config via the GSO fetch path (parity with snapshots).
        try:
            live_fp = config_fingerprint(fetch_space_config(ws, space_id))
        except Exception as e:
            logger.warning("could not fetch live config for fingerprint (%s): %s", space_id, e)
            return None
        # Candidate configs that could be live: champions, baselines, and accepted
        # (non-rolled-back) iterations across the space's runs.
        df = sql_warehouse_query(
            ws, wh_id,
            f"SELECT i.run_id, i.iteration, i.overall_accuracy, i.is_champion, "
            f"       i.observed_config_json, i.config_json "
            f"FROM {_q(catalog)}.{_q(schema)}.`genie_opt_iterations` i "
            f"JOIN {_q(catalog)}.{_q(schema)}.`genie_opt_runs` r ON i.run_id = r.run_id "
            f"WHERE r.space_id = '{space_id}' AND i.eval_scope = 'full' "
            f"  AND (i.is_champion = true OR i.iteration = 0 OR i.rolled_back = false) "
            f"ORDER BY r.started_at DESC LIMIT 200"
        )
        if df.empty:
            return None
        rows = df.to_dict(orient="records")

        # Best config ever produced across candidates.
        best = None
        for row in rows:
            acc = _pct_to_fraction(row.get("overall_accuracy"))
            if acc is None:
                continue
            if best is None or acc > best["accuracy"]:
                best = {"accuracy": acc, "run_id": row.get("run_id"),
                        "iteration": int(row.get("iteration") or 0)}

        # The iteration whose config matches the LIVE fingerprint.
        matched = None
        for row in rows:
            for col in ("observed_config_json", "config_json"):
                cfg = row.get(col)
                if cfg and config_fingerprint(cfg) == live_fp:
                    matched = row
                    break
            if matched:
                break

        result = {
            "live_fingerprint": live_fp,
            "best_accuracy": best["accuracy"] if best else None,
            "best_run_id": best["run_id"] if best else None,
            "best_iteration": best["iteration"] if best else None,
            "matched": matched is not None,
        }
        if matched:
            live_acc = _pct_to_fraction(matched.get("overall_accuracy"))
            result.update({
                "accuracy": live_acc,
                "run_id": matched.get("run_id"),
                "iteration": int(matched.get("iteration") or 0),
            })
            delta_store.set_space_champion(
                space_id,
                run_id=matched.get("run_id"),
                iteration=int(matched.get("iteration") or 0),
                accuracy=live_acc,
                config_fingerprint=live_fp,
                source="live_verified",
                best_run_id=result["best_run_id"],
                best_iteration=result["best_iteration"],
                best_accuracy=result["best_accuracy"],
            )
        return result

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        logger.warning("Live-champion resolution failed for %s: %s", space_id, e)
        return None


def _parse_identifier(identifier: str) -> tuple[str, str, str]:
    """Parse a 3-part table identifier into (catalog, schema, table)."""
    parts = identifier.replace("`", "").split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", parts[0] if parts else ""


def _enrich_with_uc_descriptions(space_data: dict, ws) -> int:
    """Fetch UC table/column descriptions and merge into *space_data* in-place.

    Only fills blanks — never overwrites existing ``description`` or ``comment``
    values in the Genie Agent config.  Returns the number of enriched items.
    """
    ds = space_data.get("data_sources", {})
    all_sources = list(ds.get("tables", [])) + list(ds.get("metric_views", []))
    if not all_sources:
        return 0

    # Build (cat, sch, tbl) refs + index back to space_data items
    refs: list[tuple[str, str, str]] = []
    source_by_fqn: dict[str, dict] = {}
    for src in all_sources:
        ident = src.get("identifier", "")
        if not ident:
            continue
        cat, sch, tbl = _parse_identifier(ident)
        if cat and sch and tbl:
            fqn = f"{cat}.{sch}.{tbl}"
            refs.append((cat, sch, tbl))
            source_by_fqn[fqn] = src

    if not refs:
        return 0

    # Fetch UC metadata in parallel (sync SDK calls)
    table_infos: dict[str, object] = {}

    def _fetch_one(ref: tuple[str, str, str]):
        cat, sch, tbl = ref
        fqn = f"{cat}.{sch}.{tbl}"
        try:
            return fqn, ws.tables.get(full_name=fqn)
        except NotFound:
            logger.debug("UC table not found: %s", fqn)
            return fqn, None
        except Exception as exc:
            logger.warning("UC metadata fetch failed for %s: %s", fqn, exc)
            return fqn, None

    for fqn, info in _uc_pool.map(_fetch_one, refs):
        if info is not None:
            table_infos[fqn] = info

    if not table_infos:
        return 0

    enriched = 0

    for fqn, info in table_infos.items():
        src = source_by_fqn.get(fqn)
        if not src:
            continue

        # Enrich table-level description
        if not (src.get("description") or src.get("comment")):
            tbl_comment = getattr(info, "comment", None) or ""
            if tbl_comment:
                src["comment"] = tbl_comment
                enriched += 1

        # Enrich column-level descriptions
        uc_cols = {
            getattr(c, "name", "").lower(): getattr(c, "comment", None) or ""
            for c in (getattr(info, "columns", None) or [])
        }
        if not uc_cols:
            continue
        for col in src.get("column_configs", []) + src.get("columns", []):
            if col.get("description") or col.get("comment"):
                continue
            col_name = (col.get("column_name") or col.get("name", "")).lower()
            uc_comment = uc_cols.get(col_name, "")
            if uc_comment:
                col["comment"] = uc_comment
                enriched += 1

    return enriched


async def scan_space(space_id: str, user_token: Optional[str] = None) -> dict:
    """Fetch space config, calculate IQ score, and persist to Lakebase.

    Args:
        space_id: The Genie Agent ID
        user_token: Optional user token for OBO auth (not used directly, SDK handles this)

    Returns:
        ScanResult dict with score, maturity, breakdown, checks, findings, next_steps
    """
    logger.info(f"Scanning agent: {space_id}")

    try:
        space_data = get_serialized_space(space_id, include_top_level_description=True)
    except Exception as e:
        logger.error(f"Failed to fetch agent {space_id}: {e}")
        raise ValueError(f"Cannot scan agent {space_id}: {e}")

    # Enrich with UC descriptions so checks 2-3 reflect upstream metadata (#62)
    try:
        from backend.services.auth import get_workspace_client, run_in_context
        ws = get_workspace_client()
        loop = asyncio.get_event_loop()
        n = await loop.run_in_executor(None, run_in_context(_enrich_with_uc_descriptions, space_data, ws))
        if n:
            logger.info("Enriched %d descriptions from Unity Catalog for %s", n, space_id)
    except Exception as e:
        logger.warning("UC description enrichment skipped for %s: %s", space_id, e)

    # Fetch optimization runs from both sources concurrently
    async def _fetch_opt_run():
        try:
            return await get_latest_optimization_run(space_id)
        except Exception as e:
            logger.warning(f"Failed to fetch optimization run for {space_id}: {e}")
            return None

    async def _fetch_gso_runs():
        try:
            from backend.services import gso_lakebase
            runs = await gso_lakebase.load_gso_runs_for_space(space_id)
            # Delta table fallback when Lakebase synced tables are empty
            if not runs:
                from backend.services.request_config import get_request_config
                rc = get_request_config()
                catalog = rc.gso_catalog or os.environ.get("GSO_CATALOG", "")
                schema = os.environ.get("GSO_SCHEMA", "genie_space_optimizer")
                wh_id = rc.warehouse_id or os.environ.get("GSO_WAREHOUSE_ID") or os.environ.get("SQL_WAREHOUSE_ID", "")
                if catalog and wh_id:
                    try:
                        from genie_space_optimizer.common.warehouse import sql_warehouse_query
                        from backend.services.auth import get_service_principal_client
                        def _q(s: str) -> str:
                            return f"`{s.replace('`', '``')}`"
                        ws = get_service_principal_client()
                        df = sql_warehouse_query(
                            ws, wh_id,
                            f"SELECT run_id, space_id, status, best_accuracy, completed_at, started_at "
                            f"FROM {_q(catalog)}.{_q(schema)}.`genie_opt_runs` "
                            f"WHERE space_id = '{space_id}' ORDER BY started_at DESC"
                        )
                        if not df.empty:
                            runs = df.to_dict(orient="records")
                    except Exception as e:
                        logger.warning(f"GSO Delta fallback failed for {space_id}: {e}")
            return runs or []
        except Exception as e:
            logger.warning(f"Failed to check GSO runs for {space_id}: {e}")
            return []

    optimization_run, gso_runs = await asyncio.gather(
        _fetch_opt_run(), _fetch_gso_runs()
    )

    # Resolve the accuracy of the config CURRENTLY LIVE in the agent by matching
    # its fingerprint to a stored iteration snapshot (the cross-run champion).
    # This is authoritative — it reports what's actually deployed, not merely the
    # most recent run — and records the champion pointer for the space.
    champion_info: dict | None = None
    try:
        champion_info = await _resolve_live_champion(space_id)
    except Exception as e:
        logger.warning("champion resolution error for %s: %s", space_id, e)
    live_fp = champion_info.get("live_fingerprint") if champion_info else None

    if champion_info and champion_info.get("matched") and champion_info.get("accuracy") is not None:
        # Live config matched a snapshot → use its measured accuracy.
        optimization_run = {"accuracy": champion_info["accuracy"], "created_at": None}
    else:
        # Fallback 1: a previously-recorded champion pointer whose fingerprint
        # still matches the live config.
        pointer = None
        try:
            from backend.services import delta_store
            loop = asyncio.get_event_loop()
            pointer = await loop.run_in_executor(None, delta_store.get_space_champion, space_id)
        except Exception:
            pointer = None
        if pointer and live_fp and pointer.get("config_fingerprint") == live_fp and pointer.get("accuracy") is not None:
            optimization_run = {"accuracy": float(pointer["accuracy"]), "created_at": None}
        else:
            # Fallback 2: legacy heuristic — most recent terminal run's best_accuracy.
            for gso_run in gso_runs:  # already sorted most recent first
                status = str(gso_run.get("status", "")).upper()
                best_acc = gso_run.get("best_accuracy")
                if status in _GSO_TERMINAL and best_acc is not None:
                    acc = _pct_to_fraction(best_acc)
                    gso_as_opt = {
                        "accuracy": acc,
                        "created_at": gso_run.get("completed_at") or gso_run.get("started_at"),
                    }
                    if optimization_run is None or gso_as_opt["accuracy"] > optimization_run.get("accuracy", 0):
                        optimization_run = gso_as_opt
                    break

    scan_result = calculate_score(space_data, optimization_run=optimization_run)
    scan_result["space_id"] = space_id

    # Surface cross-run champion context so the UI can show "live vs best available".
    if champion_info:
        best_acc = champion_info.get("best_accuracy")
        live_acc = champion_info.get("accuracy") if champion_info.get("matched") else None
        scan_result["champion"] = {
            "verified": bool(champion_info.get("matched")),
            "live_accuracy": live_acc,
            "live_run_id": champion_info.get("run_id"),
            "live_iteration": champion_info.get("iteration"),
            "best_accuracy": best_acc,
            "best_run_id": champion_info.get("best_run_id"),
            "best_iteration": champion_info.get("best_iteration"),
            "live_is_best": (
                live_acc is not None and best_acc is not None and live_acc >= best_acc
            ),
        }

    # Persist to Lakebase
    try:
        await save_scan_result(space_id, scan_result)
        logger.info(f"Scan result saved for {space_id}: score={scan_result['score']}")
    except Exception as e:
        logger.warning(f"Failed to persist scan result for {space_id}: {e}")

    return scan_result
