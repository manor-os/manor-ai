"""Version complete, authorized catalog projections for client cache validation."""

import hashlib
import json
from typing import Generic, TypeVar

from pydantic import BaseModel

from packages.core.models.user import User

Item = TypeVar("Item", bound=BaseModel)


class CatalogResponse(BaseModel, Generic[Item]):
    version: str
    items: list[Item] | None


def catalog_response(
    items: list[Item],
    *,
    catalog: str,
    user: User,
    filters: dict,
    known_version: str | None,
) -> CatalogResponse[Item]:
    payload = {
        "schema": 1,
        "catalog": catalog,
        "entity_id": user.entity_id,
        "user_id": user.id,
        "filters": filters,
        "items": [item.model_dump(mode="json") for item in items],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    version = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return CatalogResponse(version=version, items=None if version == known_version else items)
