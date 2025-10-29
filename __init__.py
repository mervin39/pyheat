"""
Pyheat application bootstrap (pyscript/apps/pyheat/__init__.py)

Responsibilities:
- Minimal bootstrap only: instantiate the orchestrator and hand it to the HA adapters
- Do not contain business logic or heavy imports that must exist at import time
- Provide a safe shutdown path

This module attempts to import `core`, `ha_triggers` and `ha_services` and initialize
them if available. If any of those modules are missing (we're scaffolding the project),
it will log a warning and defer full initialization until those modules are present.

The actual orchestrator creation is delegated to `core.create_orchestrator(hass, logger)`
if available. This keeps the bootstrap lightweight and testable.
"""
from __future__ import annotations

from typing import Optional

# Public singletons created at bootstrap time
orchestrator = None
_triggers = None
_services = None


def _safe_import(name: str):
    try:
        module = __import__(f".{name}", globals(), locals(), ["*"], 1)
        return module
    except Exception:  # pragma: no cover - defensive import
        log.debug(f"Optional module '{name}' not available yet")
        return None


def setup() -> None:
    """Initialize the pyheat orchestrator and HA adapters.

    This is intentionally idempotent and safe to call multiple times. If the
    implementation modules are not available yet, setup will log and return.
    """
    global orchestrator, _triggers, _services

    if orchestrator is not None:
        log.debug("Pyheat already initialized")
        return

    core = _safe_import("core")
    ha_triggers = _safe_import("ha_triggers")
    ha_services = _safe_import("ha_services")

    if core is None:
        log.warning("pyheat.core not present; bootstrap deferred until core is implemented")
        return

    # create orchestrator via factory if provided, otherwise try common constructors
    try:
        if hasattr(core, "create_orchestrator"):
            orchestrator = core.create_orchestrator()
        elif hasattr(core, "Orchestrator"):
            orchestrator = core.Orchestrator()
        elif hasattr(core, "PyHeatApp"):
            orchestrator = core.PyHeatApp()
        else:
            # fallback: attempt to use a generic 'Orchestrator' symbol
            orchestrator = getattr(core, "Orchestrator", None)
            if callable(orchestrator):
                orchestrator = orchestrator()
            else:
                log.error("core module present but no known factory/class found")
                orchestrator = None
    except Exception as e:
        log.error(f"Failed to create pyheat orchestrator: {e}")
        orchestrator = None

    if orchestrator is None:
        log.warning("Orchestrator was not created; aborting adapter initialization")
        return

    # Initialize adapters if available. They may return handle objects or None.
    try:
        if ha_triggers and hasattr(ha_triggers, "init"):
            _triggers = ha_triggers.init(orchestrator)
        else:
            log.info("ha_triggers adapter not present; triggers not registered")
    except Exception as e:
        log.error(f"Failed to initialize ha_triggers: {e}")

    try:
        if ha_services and hasattr(ha_services, "init"):
            _services = ha_services.init(orchestrator)
        else:
            log.info("ha_services adapter not present; services not registered")
    except Exception as e:
        log.error(f"Failed to initialize ha_services: {e}")

    log.info("pyheat bootstrap complete")


def shutdown() -> None:
    """Attempt to cleanly shut down adapters and orchestrator.

    This is best-effort and will swallow exceptions to avoid crashing the host
    during HA shutdown sequences.
    """
    global orchestrator, _triggers, _services

    # adapters may provide a shutdown() method
    try:
        if _triggers and hasattr(_triggers, "shutdown"):
            _triggers.shutdown()
        elif _triggers and hasattr(_triggers, "close"):
            _triggers.close()
    except Exception as e:
        log.error(f"Error shutting down triggers: {e}")

    try:
        if _services and hasattr(_services, "shutdown"):
            _services.shutdown()
    except Exception as e:
        log.error(f"Error shutting down services: {e}")

    try:
        if orchestrator and hasattr(orchestrator, "shutdown"):
            orchestrator.shutdown()
    except Exception as e:
        log.error(f"Error shutting down orchestrator: {e}")


# The startup_load_config function is triggered by @time_trigger("startup")
# No need to manually call setup() - pyscript will auto-run the trigger function
# when the module loads and Home Assistant starts
