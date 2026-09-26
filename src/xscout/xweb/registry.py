"""Operation registry: queryId + features per operation with a fallback chain (spec §6, plan 2.2).

Order: live discovery -> last good discovery stored in the DB -> hardcoded fallback file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from importlib import resources

from xscout.store.operations import OperationRecord, OperationRepository
from xscout.xweb.constants import FIELD_TOGGLES, WANTED_OPS
from xscout.xweb.discovery import DiscoveryResult, FetchText, discover

log = logging.getLogger("xscout.registry")

MISSING_FEATURES_RE = re.compile(r"cannot be null:\s*(.+)$", re.I)


@dataclass(frozen=True)
class OpSpec:
    name: str
    query_id: str
    features: dict[str, object]
    field_toggles: dict[str, bool]
    source: str


def load_fallback() -> dict[str, OpSpec]:
    data = json.loads(resources.files("xscout.xweb").joinpath("fallback_ops.json").read_text())
    sets = data["feature_sets"]
    return {
        name: OpSpec(
            name=name,
            query_id=op["query_id"],
            features=dict(sets[op["features"]]),
            field_toggles=dict(FIELD_TOGGLES.get(name, {})),
            source="fallback",
        )
        for name, op in data["operations"].items()
    }


def parse_missing_features(message: str) -> list[str]:
    """Feature names from an X error 336 message ("... cannot be null: a, b")."""
    m = MISSING_FEATURES_RE.search(message)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []


class UnknownOperation(KeyError):
    pass


class OperationRegistry:
    def __init__(self, repo: OperationRepository | None = None):
        self._repo = repo
        self._specs: dict[str, OpSpec] = load_fallback()
        self.last_discovery: DiscoveryResult | None = None
        self.last_error: str | None = None

    def get(self, name: str) -> OpSpec:
        try:
            return self._specs[name]
        except KeyError as e:
            raise UnknownOperation(name) from e

    def snapshot(self) -> dict[str, OpSpec]:
        return dict(self._specs)

    async def load(self) -> None:
        """Overlay the last good discovery stored in the DB on the fallback constants."""
        if self._repo is None:
            return
        for name, rec in (await self._repo.active()).items():
            self._specs[name] = OpSpec(
                name=name,
                query_id=rec.query_id,
                features=rec.features,
                field_toggles=dict(FIELD_TOGGLES.get(name, {})),
                source=f"db:{rec.source}",
            )

    async def refresh(self, fetch: FetchText) -> list[str]:
        """Run discovery; on success replace specs and persist. Returns operations whose query id changed."""
        try:
            result = await discover(fetch)
        except Exception as e:
            self.last_error = f"discovery failed: {e}"
            log.warning(self.last_error)
            return []
        self.last_discovery = result
        self.last_error = f"missing operations: {result.missing}" if result.missing else None
        records: list[OperationRecord] = []
        changed: list[str] = []
        for name in WANTED_OPS:
            op = result.ops.get(name)
            if op is None:
                continue
            features = result.resolved_features(name)
            if features is None:  # switches not found next to the query id: keep the known features
                features = self._specs[name].features if name in self._specs else {}
            if name in self._specs and self._specs[name].query_id != op.query_id:
                changed.append(name)
            self._specs[name] = OpSpec(
                name=name,
                query_id=op.query_id,
                features=features,
                field_toggles=dict(FIELD_TOGGLES.get(name, {})),
                source="discovery",
            )
            records.append(
                OperationRecord(name, op.query_id, features, op.feature_switches, op.field_toggles, "discovery")
            )
        if self._repo is not None and records:
            await self._repo.save_active(records)
        if changed:
            log.info("query ids changed", extra={"fields": {"operations": changed}})
        return changed

    async def heal_missing_features(self, name: str, message: str) -> bool:
        """Error 336: set the features X says are missing to true. Returns False when nothing new to try."""
        missing = parse_missing_features(message)
        spec = self.get(name)
        if not missing or all(spec.features.get(k) is True for k in missing):
            return False
        features = {**spec.features, **dict.fromkeys(missing, True)}
        self._specs[name] = replace(spec, features=features)
        if self._repo is not None:
            await self._repo.patch_features(name, spec.query_id, features)
        log.info("features healed", extra={"fields": {"operation": name, "added": missing}})
        return True
