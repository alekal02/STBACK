"""Cache do PDF por turno e por rota.

O relatório do dia continua em output/unificado/DATA. Cada turno guarda
uma cópia estável em turnos/diurno ou turnos/noturno, para um não apagar
o mapa do outro. As fotos ficam em mapas_relatorio, nomeadas pelo hash
do que aparece no desenho.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

ARQUIVOS_TURNO = ("mapa.html", "rotas.json", "relatorio-diario.json", "relatorio.json")
VISUAL = 1


def concorrencia() -> int:
    padrao = "1" if (os.getenv("RENDER") or "").strip().lower() in {"1", "true", "yes", "sim"} else "3"
    try:
        n = int(os.getenv("PDF_MAP_CONCURRENCY") or padrao)
    except ValueError:
        n = int(padrao)
    return max(1, min(n, 4))


def digest(dados: Any) -> str:
    bruto = json.dumps(dados, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def turno_de(pasta: Path) -> str:
    rel = pasta / "relatorio.json"
    if rel.is_file():
        try:
            pedido = str(json.loads(rel.read_text(encoding="utf-8")).get("turno") or "").strip().lower()
            if pedido in {"diurno", "noturno"}:
                return pedido
        except Exception:
            pass
    diario = pasta / "relatorio-diario.json"
    if diario.is_file():
        try:
            pedido = str(json.loads(diario.read_text(encoding="utf-8")).get("turno") or "").strip().lower()
            if pedido in {"diurno", "noturno"}:
                return pedido
        except Exception:
            pass
    return ""


def pasta_turno(pasta_dia: Path, turno: str) -> Path:
    return Path(pasta_dia) / "turnos" / turno


def espelhar_turno(pasta_dia: Path, turno: str | None = None) -> Path | None:
    """Copia o relatório atual para a pasta do turno, sem mexer no outro turno."""
    pasta_dia = Path(pasta_dia)
    pedido = (turno or turno_de(pasta_dia) or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        return None
    if not (pasta_dia / "relatorio-diario.json").is_file():
        return None
    dest = pasta_turno(pasta_dia, pedido)
    dest.mkdir(parents=True, exist_ok=True)
    for nome in ARQUIVOS_TURNO:
        origem = pasta_dia / nome
        if origem.is_file():
            shutil.copy2(origem, dest / nome)
    return dest


def resolver_trabalho(pasta_dia: Path, turno: str | None = None) -> Path:
    """Pasta cujos JSON e mapa alimentam o PDF deste turno."""
    pasta_dia = Path(pasta_dia)
    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        pedido = turno_de(pasta_dia)
    if pedido in {"diurno", "noturno"}:
        vivo = turno_de(pasta_dia)
        if vivo == pedido:
            espelhar_turno(pasta_dia, pedido)
        snap = pasta_turno(pasta_dia, pedido)
        if (snap / "mapa.html").is_file() and (snap / "relatorio-diario.json").is_file():
            return snap
    return pasta_dia


def _carimbo(pasta: Path) -> dict[str, int]:
    saida: dict[str, int] = {}
    for nome in ("mapa.html", "rotas.json", "relatorio-diario.json"):
        arq = pasta / nome
        if arq.is_file():
            st = arq.stat()
            saida[nome] = int(st.st_mtime_ns) ^ int(st.st_size)
    return saida


def cache_path(pasta: Path) -> Path:
    return Path(pasta) / "mapas_relatorio" / "cache.json"


def ler_cache(pasta: Path) -> dict[str, Any]:
    arq = cache_path(pasta)
    if not arq.is_file():
        return {}
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def pdf_inteiro_valido(pasta: Path) -> Path | None:
    """PDF já montado para estes mesmos arquivos de entrada."""
    pasta = Path(pasta)
    info = ler_cache(pasta)
    nome = str(info.get("pdf") or "relatorio-rotas.pdf")
    alvo = pasta / nome
    if not alvo.is_file() or alvo.stat().st_size < 20_000:
        return None
    if info.get("carimbo") != _carimbo(pasta):
        return None
    if str(info.get("visual") or "") != str(VISUAL):
        return None
    return alvo


def gravar_cache(pasta: Path, pdf_hash: str, mapas: dict[str, str]) -> None:
    pasta = Path(pasta)
    dest = cache_path(pasta)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "visual": VISUAL,
                "pdf_hash": pdf_hash,
                "pdf": "relatorio-rotas.pdf",
                "carimbo": _carimbo(pasta),
                "mapas": mapas,
                "quando": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def progresso_path(pasta: Path) -> Path:
    return Path(pasta) / "mapas_relatorio" / "progresso.json"


def gravar_progresso(pasta: Path, pct: int, msg: str) -> None:
    arq = progresso_path(pasta)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(
        json.dumps({"progresso": int(pct), "msg": msg}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[PDF] {msg}", flush=True)


def ler_progresso(pasta: Path) -> dict[str, Any]:
    arq = progresso_path(pasta)
    if not arq.is_file():
        return {}
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def limpar_jpeg_orfaos(pasta: Path, manter: set[str]) -> None:
    diretoria = Path(pasta) / "mapas_relatorio"
    if not diretoria.is_dir():
        return
    for arq in diretoria.glob("*.jpg"):
        if arq.name not in manter:
            try:
                arq.unlink()
            except OSError:
                pass
    parcial = Path(pasta) / "relatorio-rotas.pdf.part"
    if parcial.is_file():
        try:
            parcial.unlink()
        except OSError:
            pass
