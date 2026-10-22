"""Validação e extração de metadados de XML de NFS-e (padrão ABRASF e variantes).

O conteúdo original nunca é modificado: apenas lido.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from ..storage.file_manager import XML_PARSER_KW, detect_kind, sha256_bytes


@dataclass
class XmlInfo:
    valid: bool
    reason: str = ""
    numero: str | None = None
    data_emissao: str | None = None
    cnpj_prestador: str | None = None
    prestador: str | None = None
    tomador: str | None = None
    quantidade_notas: int = 0
    sha256: str = ""


def _first(root: etree._Element, *paths: str) -> str | None:
    for p in paths:
        res = root.xpath(p)
        if res:
            node = res[0]
            text = node.text if hasattr(node, "text") else str(node)
            if text and text.strip():
                return text.strip()
    return None


def _ln(name: str) -> str:
    return f"*[local-name()='{name}']"


class XmlValidator:
    def validate_bytes(self, data: bytes) -> XmlInfo:
        info = XmlInfo(valid=False, sha256=sha256_bytes(data))
        if not data or not data.strip():
            info.reason = "Arquivo vazio."
            return info
        kind = detect_kind(data[:1024])
        if kind == "html":
            info.reason = "Conteúdo é HTML, não XML (provável sessão expirada ou erro do portal)."
            return info
        try:
            root = etree.fromstring(data, etree.XMLParser(**XML_PARSER_KW))
        except etree.XMLSyntaxError as exc:
            info.reason = f"XML malformado ou incompleto: {exc}"
            return info

        # Alguns portais entregam a resposta da consulta como arquivo .xml,
        # mesmo quando não há notas. Isso não é uma NFS-e e nunca deve ser salvo
        # nem contado como XML fiscal baixado.
        root_name = etree.QName(root).localname.casefold()
        if root_name in {"consultarnfseservicoprestadoresposta",
                         "consultarnfseservicotomadoresposta",
                         "consultarnfseservicoprestadoresposta",
                         "consultarnfseservicotomadoresposta"}:
            messages = " ".join(t.strip() for t in root.itertext() if t and t.strip()).casefold()
            if any(term in messages for term in (
                "notas fiscais não localizadas", "notas fiscais nao localizadas",
                "nenhuma nota", "nenhuma nfse", "nenhuma nfs-e",
            )):
                info.reason = "O portal retornou a resposta da consulta: NOTAS FISCAIS NÃO LOCALIZADAS. Não é um XML fiscal de nota."
            else:
                info.reason = f"O portal retornou uma resposta de consulta ({root_name}), não um XML fiscal de nota."
            return info

        inf = f".//{_ln('InfNfse')}"
        info.quantidade_notas = len(root.xpath(inf)) or len(root.xpath(f"//{_ln('Nfse')}")) or 1
        info.numero = _first(root, f"{inf}/{_ln('Numero')}", f".//{_ln('Numero')}",
                             f".//{_ln('NumeroNfse')}", f".//{_ln('nNFSe')}")
        info.data_emissao = _first(root, f"{inf}/{_ln('DataEmissao')}", f".//{_ln('DataEmissao')}",
                                   f".//{_ln('dhEmi')}")
        info.cnpj_prestador = _first(
            root,
            f"{inf}/{_ln('PrestadorServico')}//{_ln('Cnpj')}",
            f".//{_ln('IdentificacaoPrestador')}/{_ln('Cnpj')}",
            f".//{_ln('Prestador')}//{_ln('Cnpj')}", f".//{_ln('emit')}//{_ln('CNPJ')}")
        info.prestador = _first(
            root, f"{inf}/{_ln('PrestadorServico')}/{_ln('RazaoSocial')}",
            f".//{_ln('Prestador')}//{_ln('RazaoSocial')}", f".//{_ln('emit')}/{_ln('xNome')}")
        info.tomador = _first(
            root, f"{inf}/{_ln('TomadorServico')}//{_ln('RazaoSocial')}",
            f".//{_ln('Tomador')}//{_ln('RazaoSocial')}", f".//{_ln('toma')}/{_ln('xNome')}")
        info.valid = True
        return info

    def validate_file(self, path: Path) -> XmlInfo:
        try:
            return self.validate_bytes(path.read_bytes())
        except OSError as exc:
            return XmlInfo(valid=False, reason=f"Não foi possível ler o arquivo: {exc.strerror}")
