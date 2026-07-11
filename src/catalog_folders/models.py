from __future__ import annotations

from dataclasses import asdict, dataclass


CLASSIFIER_SKILL_VERSION = "1.0"


@dataclass(frozen=True)
class Category:
    category_id: str
    keyword_zh: str
    normalized_keyword_zh: str
    directory_name: str
    source_notebook: str
    definition_sha256: str
    classification_enabled: bool = True
    retired_at: str | None = None
    guidance_zh: str | None = None
    aliases_zh: tuple[str, ...] = ()
    exclusions_zh: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        value = asdict(self)
        value["aliases_zh"] = list(self.aliases_zh)
        value["exclusions_zh"] = list(self.exclusions_zh)
        return {key: item for key, item in value.items() if item is not None}
