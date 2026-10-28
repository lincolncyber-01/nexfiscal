"""Persistência e manutenção da memória dos portais (SQLite)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..database.database import Database
from ..database.models import Portal
from ..database.repositories import MemoryRepository, PortalRepository
from ..portal.portal_memory import PortalProfile

log = logging.getLogger("nexfiscal.memory")


class PortalMemoryManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.portals = PortalRepository(db)
        self.repo = MemoryRepository(db)

    # ------------------------------------------------------- carregar/salvar ---
    def load_profile(self, portal: Portal) -> PortalProfile:
        profile = PortalProfile(
            portal_id=portal.id, url=portal.url, cities=list(portal.cidades),
            selectors=self.repo.load_selectors(portal.id), flows=self.repo.load_flows(portal.id),
            notes=self.repo.load_notes(portal.id), signature=portal.assinatura,
            last_recognition=portal.ultimo_reconhecimento)
        return profile

    def save_profile(self, profile: PortalProfile, *, recognized_stage: str | None = None,
                     force: bool = False) -> None:
        """Persist changed knowledge, or force a full rewrite when the operator requests it."""
        if not profile.dirty and recognized_stage is None and not force:
            return
        with self.db.transaction():
            for (stage, key), strategies in profile.selectors.items():
                self.repo.save_selector(profile.portal_id, stage, key, strategies)
            for name, steps in profile.flows.items():
                self.repo.save_flow(profile.portal_id, name, steps)
            for change in profile.pending_changes:
                self.repo.add_change(profile.portal_id, change.stage, change.key, change.old, change.new)
            for note in profile.pending_notes:
                self.repo.add_note(profile.portal_id, note)
            stages = sorted({st for st, _ in profile.selectors})
            self.portals.update_recognition(
                profile.portal_id, profile.signature,
                json.dumps({"stages_reconhecidos": stages}, ensure_ascii=False))
        profile.pending_changes.clear()
        profile.pending_notes.clear()
        profile.dirty = False

    # --------------------------------------------------------- manutenção ----
    def clear(self, portal_id: int | None = None) -> None:
        self.repo.clear(portal_id)
        log.info("Memória dos portais limpa%s.", "" if portal_id is None else f" (portal {portal_id})")

    def summary(self, portal: Portal) -> dict[str, int]:
        out: dict[str, int] = {}
        for (stage, _key) in self.repo.load_selectors(portal.id):
            out[stage] = out.get(stage, 0) + 1
        return out

    def export_json(self, path: Path) -> None:
        data = []
        for portal in self.portals.list_all():
            data.append({
                "url": portal.url, "cidades": portal.cidades, "assinatura": portal.assinatura,
                "seletores": {f"{s}/{k}": v for (s, k), v in self.repo.load_selectors(portal.id).items()},
                "fluxos": self.repo.load_flows(portal.id), "notas": self.repo.load_notes(portal.id),
            })
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def import_json(self, path: Path) -> int:
        count = 0
        for item in json.loads(path.read_text("utf-8")):
            portal = self.portals.register(item.get("cidades", []), item["url"])
            for composite, strategies in item.get("seletores", {}).items():
                stage, key = composite.split("/", 1)
                self.repo.save_selector(portal.id, stage, key, strategies)
            for name, steps in item.get("fluxos", {}).items():
                self.repo.save_flow(portal.id, name, steps)
            count += 1
        return count
