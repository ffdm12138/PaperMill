"""Discovery v4 migration package.

This package is strictly isolated from ``src.discovery`` — production discovery
code must never import from here.  It contains only migration-time modules:
legacy inventory, notebook/candidate readers, archive builder, and the
migration service.
"""
