"""Discovery v4 stores — persistent state management.

All writes use ``atomic_write_json_fsync()`` (tmp + flush + fsync +
os.replace + fsync parent dir).  Stores accept a ``DiscoveryWorkspace``
and resolve paths through it — no store reads ``config.settings``
discovery paths directly.
"""
