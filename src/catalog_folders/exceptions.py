from __future__ import annotations


class InvalidChineseKeyword(ValueError):
    """Raised when a Chinese keyword is structurally invalid (empty, too long,
    has leading/trailing whitespace, trailing dots, control characters,
    or non-NFC unicode)."""


class NotebookSchemaError(ValueError):
    """Raised when a keyword notebook is missing required fields or has an
    invalid schema."""


class InvalidKeywordId(ValueError):
    """Raised when a notebook's keyword_id is not a valid 16-digit hex string."""


class DuplicateKeywordId(ValueError):
    """Raised when two notebooks share the same keyword_id."""


class DuplicateKeyword(ValueError):
    """Raised when two notebooks with different keyword_ids share the same
    Chinese keyword (collision)."""


class FilesystemNameCollision(ValueError):
    """Raised when a keyword would produce a directory name that collides with
    a reserved name, a Windows reserved name, or contains unsafe filesystem
    characters."""
