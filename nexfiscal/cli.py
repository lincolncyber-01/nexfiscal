"""Interface de linha de comando (alternativa à GUI) e ponto de entrada."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .app import AppContext, bootstrap
from .database.repositories import CompanyRepository
from .dates import Period
from .events import EventBus, EventKind
from .exceptions import NexFiscalError
from .spreadsheet.validator import validate_spreadsheet
from .utils import normalize_city


def _ask_url(city: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    print(f"\nPortal não cadastrado para: {city}")
    return input("Informe a URL do portal (vazio para ignorar a cidade): ").strip() or None


def _print_event(ev) -> None:  # type: ignore[no-untyped-def]
    if ev.kind is EventKind.CAPTCHA:
        print(f"\n*** {ev.message}\n")


def cmd_run(ctx: AppContext, a: argparse.Namespace) -> int:
    from .controller import BatchRunner
    from .portal.portal_registry import PortalRegistry
    from .reports.export import export

    s = ctx.settings
    if a.saida:
        s.output_root = a.saida
    registry = PortalRegistry(ctx.db)
    if a.planilha:
        rep = validate_spreadsheet(a.planilha, registry.known_cities())
        print(rep.format())
        if rep.has_critical:
            print("\nCorrija os erros acima. O processamento não foi iniciado.")
            return 2
        rows = rep.valid_rows
    else:  # retomada a partir das credenciais armazenadas
        rows = CompanyRepository(ctx.db, ctx.cipher).load_rows()
        if not rows:
            print("Nenhuma empresa armazenada. Informe --planilha.")
            return 2
    wanted = {normalize_city(c) for c in a.cidades.split(",")} if a.cidades else None
    cities = sorted({r.cidade for r in rows if not wanted or r.cidade_norm in wanted})
    for city in registry.ensure_portals(cities, _ask_url):
        print(f"Cidade ignorada (sem portal): {city}")
    period = Period.resolve(a.competencia or s.competence, a.inicio or s.start_date, a.fim or s.end_date)
    print(f"\nCompetência: {period.competence.label} ({period.start_br} a {period.end_br})")
    bus = EventBus()
    bus.subscribe(_print_event)
    summary = BatchRunner(s, ctx.db, ctx.cipher, bus).run(rows, period, cities=wanted, force=a.forcar)
    print("\n" + summary.report.to_text())
    if a.exportar:
        print("Relatório exportado:", export(summary.report, Path(a.exportar)))
    return 0 if summary.report.erros == 0 else 1


def cmd_portal(ctx: AppContext, a: argparse.Namespace) -> int:
    from .portal.portal_registry import PortalRegistry
    registry = PortalRegistry(ctx.db)
    if a.action == "add":
        portal = registry.register([c.strip() for c in a.cidades.split(",")], a.url)
        print(f"Portal salvo: {portal.url} -> {', '.join(portal.cidades)}")
    else:
        for p in registry.list_all():
            print(f"{p.url}  [{', '.join(p.cidades)}]  último reconhecimento: {p.ultimo_reconhecimento or '—'}")
    return 0


def cmd_maint(ctx: AppContext, a: argparse.Namespace) -> int:
    if a.cmd == "clear-credentials":
        n = CompanyRepository(ctx.db, ctx.cipher).clear_credentials()
        print(f"{n} credencial(is) removida(s).")
    else:
        from .memory.portal_memory_manager import PortalMemoryManager
        PortalMemoryManager(ctx.db).clear()
        print("Memória dos portais limpa.")
    return 0


def cmd_report(ctx: AppContext, a: argparse.Namespace) -> int:
    from .database.repositories import ExecutionRepository
    from .reports.export import export
    from .reports.report_generator import ReportGenerator
    period = Period.resolve(a.competencia or ctx.settings.competence)
    rep = ReportGenerator(ExecutionRepository(ctx.db)).build(period.competence.label)
    print(rep.to_text())
    if a.exportar:
        print("Relatório exportado:", export(rep, Path(a.exportar)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nexfiscal", description="NexFiscal — automação fiscal municipal")
    p.add_argument("--version", action="version", version=f"NexFiscal {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="abre a interface gráfica (padrão)")
    learn = sub.add_parser("learn", help="aprendizado guiado de um portal informado antes de abrir o navegador")
    learn.add_argument("--cidade", required=True, help="cidade associada ao portal")
    learn.add_argument("--url", required=True, help="URL inicial do portal (http/https)")

    r = sub.add_parser("run", help="processa em linha de comando (sem --planilha: retoma as armazenadas)")
    r.add_argument("--planilha")
    r.add_argument("--saida", help="pasta raiz de saída")
    r.add_argument("--competencia", help="MM/AAAA (padrão: mês anterior)")
    r.add_argument("--inicio", help="DD/MM/AAAA")
    r.add_argument("--fim", help="DD/MM/AAAA")
    r.add_argument("--cidades", help="lista separada por vírgula")
    r.add_argument("--forcar", action="store_true", help="reprocessa também as já concluídas")
    r.add_argument("--exportar", help="arquivo .xlsx/.csv/.pdf do relatório")
    po = sub.add_parser("portal", help="cadastro de portais")
    po.add_argument("action", choices=["add", "list"])
    po.add_argument("cidades", nargs="?", help='ex.: "Salvador,Lauro de Freitas"')
    po.add_argument("url", nargs="?")
    sub.add_parser("clear-credentials", help="remove as senhas armazenadas")
    sub.add_parser("clear-memory", help="apaga o que foi aprendido dos portais")
    rp = sub.add_parser("report", help="mostra/exporta o relatório de uma competência")
    rp.add_argument("--competencia")
    rp.add_argument("--exportar")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "gui"):
        from .ui.main_window import run_gui
        return run_gui()
    ctx = bootstrap()
    try:
        if args.cmd == "run":
            return cmd_run(ctx, args)
        if args.cmd == "portal":
            if args.action == "add" and not (args.cidades and args.url):
                print('Uso: nexfiscal portal add "Cidade[,Cidade2]" URL')
                return 2
            return cmd_portal(ctx, args)
        if args.cmd == "report":
            return cmd_report(ctx, args)
        return cmd_maint(ctx, args)
    except NexFiscalError as exc:
        logging.getLogger("nexfiscal").error("%s", exc.message)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido. Use 'nexfiscal run' sem --planilha para retomar.")
        return 130
    finally:
        ctx.db.close()
