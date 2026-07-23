"""Discovery v4 runtime — batch context, guard, budgets, telemetry.

``RuntimeContext`` is injected into all batch-owned components.
No component reads the guard via ``hasattr`` or ``_runtime_guard``.

Import directly from submodules:
    from src.discovery.runtime.batch_runtime import DiscoveryBatchRuntime
    from src.discovery.runtime.candidate_drain import CandidateDrainCoordinator
"""
