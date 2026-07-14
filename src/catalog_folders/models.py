"""Category model and classifier skill version."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass


CLASSIFIER_SKILL_VERSION = "1.0"


@dataclass(frozen=True)
class Category:
    category_id: str
    keyword_zh: str
    directory_name: str
    source_notebook: str
    definition_sha256: str
    classification_enabled: bool = True
    retired_at: str | None = None
    guidance_zh: str | None = None
    aliases_zh: tuple[str, ...] = ()
    exclusions_zh: tuple[str, ...] = ()
    normalized_keyword_zh: str = ""

    def __post_init__(self):
        """Enforce core invariants on every Category construction.

        Every code path that builds a Category (notebook parsing, legacy
        migration, test fixtures) must satisfy these checks.
        """
        # category_id must be 16 hex chars
        if not re.fullmatch(r"[0-9a-f]{16}", self.category_id):
            raise ValueError(
                f"Category.category_id must be 16 hex chars: {self.category_id!r}"
            )
        # keyword_zh must not be empty
        if not self.keyword_zh or not self.keyword_zh.strip():
            raise ValueError("Category.keyword_zh must not be empty")
        # directory_name must not be empty
        if not self.directory_name or not self.directory_name.strip():
            raise ValueError("Category.directory_name must not be empty")
        # source_notebook must end with .json
        if not self.source_notebook:
            raise ValueError("Category.source_notebook must not be empty")
        if not self.source_notebook.endswith(".json"):
            raise ValueError(
                f"Category.source_notebook must end with .json: "
                f"{self.source_notebook!r}"
            )
        # definition_sha256 must be 64 hex chars
        if not re.fullmatch(r"[0-9a-f]{64}", self.definition_sha256):
            raise ValueError(
                f"Category.definition_sha256 must be 64 hex chars: "
                f"{self.definition_sha256!r}"
            )

    def to_dict(self) -> dict:
        value = asdict(self)
        value["aliases_zh"] = list(self.aliases_zh)
        value["exclusions_zh"] = list(self.exclusions_zh)
        return {key: item for key, item in value.items() if item not in (None, "", [], ())}
