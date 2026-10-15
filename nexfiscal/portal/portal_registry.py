"""Cadastro cidade -> portal."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from ..database.database import Database
from ..database.models import Portal
from ..database.repositories import PortalRepository
from ..exceptions import PortalNotRegisteredError
from ..utils import normalize_city

log = logging.getLogger("nexfiscal.portal")


class PortalRegistry:
    def __init__(self, db: Database) -> None:
        self.repo = PortalRepository(db)

    def register(self, cities: Iterable[str] | str, url: str, nome: str | None = None) -> Portal:
        cities = [cities] if isinstance(cities, str) else list(cities)
        portal = self.repo.register(cities, url, nome)
        log.info("Portal cadastrado: %s -> %s", ", ".join(cities), portal.url)
        return portal

    def resolve(self, city: str) -> Portal | None:
        return self.repo.get_by_city(city)

    def require(self, city: str) -> Portal:
        portal = self.resolve(city)
        if portal is None:
            raise PortalNotRegisteredError(f"Portal não cadastrado para a cidade '{city}'.")
        return portal

    def known_cities(self) -> set[str]:
        return self.repo.known_cities()

    def missing(self, cities: Iterable[str]) -> list[str]:
        known = self.known_cities()
        return [c for c in cities if normalize_city(c) not in known]

    def ensure_portals(self, cities: Iterable[str],
                       ask_url: Callable[[str], str | None]) -> list[str]:
        """Pede a URL das cidades sem portal. Devolve as cidades que continuam sem portal."""
        still_missing: list[str] = []
        for city in self.missing(cities):
            url = ask_url(city)
            if url and url.strip():
                self.register([city], url)
            else:
                still_missing.append(city)
        return still_missing

    def list_all(self) -> list[Portal]:
        return self.repo.list_all()
