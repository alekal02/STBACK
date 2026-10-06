"""Leitura do painel. O estado persistente fica no SQLite; o JSON só entra na primeira vez."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import parse_data, rotulo_periodo

_GRUPOS = {
    "Concluída": "concluida",
    "Concluída parcialmente": "parcial",
    "Início de coleta": "inicio",
    "Não iniciada": "nao",
    "Apoio": "apoio",
    "Só passagem": "passagem",
}
_CODIGO = re.compile(r"\b(\d{2}[A-Z]{2,4})\b", re.I)
_HORA = re.compile(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})")


def sincronizacao_ocupada() -> bool:
    from .live_sync import ocupada

    return ocupada()


def ler_estado(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    job_rodando: bool = False,
) -> dict[str, Any]:
    """Leitura para o painel. Não chama a Fulltrack."""
    from .live_sync import ler_painel, ocupada

    estado = ler_painel(output_dir, data, turno, atualizando=bool(job_rodando or ocupada()))
    return _exibir_classificacao(estado)


def pedir_atualizacao(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    job_rodando: bool = False,
) -> dict[str, Any]:
    """Pede uma sincronização. Se já houver uma, devolve o estado atual."""
    from .live_sync import pedir

    return _exibir_classificacao(pedir(output_dir, data, turno, job_rodando=job_rodando))


def montar_estado(
    output_dir: Path,
    data: str | None,
    turno: str | None,
) -> dict[str, Any]:
    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        raise ValueError("Turno inválido. Use diurno ou noturno.")
    dia = parse_data(data) or date.today()
    pasta, diario, meta, falha = _ler_pasta(Path(output_dir), dia, pedido)
    periodo = str((diario or meta or {}).get("periodo") or "") or _periodo_padrao(dia, pedido)
    atualizado = _atualizado(pasta, diario or {}, meta or {})
    rotas = [_rota(linha) for linha in ((diario or {}).get("linhas") or [])]
    resumo = _resumo(rotas)
    aviso = ""
    if falha:
        aviso = "Falha na atualização — exibindo dados salvos." if rotas else "Falha na atualização."
    elif not rotas:
        aviso = "Ainda não há estado salvo deste turno."
    return {
        "ok": not falha,
        "vazio": not rotas,
        "empresa": "Segtrack",
        "data": dia.isoformat(),
        "turno": pedido,
        "periodo": periodo,
        "hora_inicio": str((diario or meta or {}).get("hora_inicio") or ("19:00" if pedido == "noturno" else "06:00")),
        "hora_fim": str((diario or meta or {}).get("hora_fim") or ("05:59" if pedido == "noturno" else "18:59")),
        "data_fim": str((diario or meta or {}).get("data_fim") or ""),
        "atualizado_em": atualizado,
        "atualizado_hora": _hora_curta(atualizado),
        "atualizando": False,
        "falha": falha,
        "aviso": aviso,
        "mapa_url": _mapa_url(output_dir, pasta),
        "resumo": resumo,
        "rotas": rotas,
    }


def _periodo_padrao(dia: date, turno: str) -> str:
    if turno == "noturno":
        return rotulo_periodo(dia, "19:00", "05:59")
    return rotulo_periodo(dia, "06:00", "18:59")


def _ler_pasta(
    output_dir: Path,
    dia: date,
    turno: str,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, str]:
    base = output_dir / "unificado" / dia.isoformat()
    candidatos = [base / "turnos" / turno, base]
    falha = ""
    for pasta in candidatos:
        diario_arq = pasta / "relatorio-diario.json"
        meta_arq = pasta / "relatorio.json"
        if not diario_arq.is_file():
            continue
        try:
            diario = json.loads(diario_arq.read_text(encoding="utf-8"))
            meta = json.loads(meta_arq.read_text(encoding="utf-8")) if meta_arq.is_file() else {}
        except (OSError, json.JSONDecodeError) as exc:
            falha = str(exc)
            continue
        turno_arquivo = str((meta or {}).get("turno") or diario.get("turno") or "").strip().lower()
        if pasta == base and turno_arquivo != turno:
            continue
        if not isinstance(diario, dict):
            continue
        return pasta, diario, meta if isinstance(meta, dict) else {}, ""
    return None, None, None, falha


def _atualizado(pasta: Path | None, diario: dict[str, Any], meta: dict[str, Any]) -> str:
    bruto = str(diario.get("gerado_em") or meta.get("quando") or "").strip()
    if bruto:
        return bruto
    if pasta and (pasta / "relatorio-diario.json").is_file():
        stamp = datetime.fromtimestamp((pasta / "relatorio-diario.json").stat().st_mtime)
        return stamp.isoformat(timespec="seconds")
    return ""


def _hora_curta(iso: str) -> str:
    texto = (iso or "").strip()
    if "T" in texto:
        return texto.split("T", 1)[1][:8]
    if " " in texto and ":" in texto:
        return texto.split(" ", 1)[1][:8]
    return ""


def _mapa_url(output_dir: Path, pasta: Path | None) -> str:
    if pasta is None:
        return ""
    mapa = pasta / "mapa.html"
    if not mapa.is_file():
        return ""
    try:
        return "/arquivos/" + mapa.relative_to(Path(output_dir)).as_posix()
    except ValueError:
        return ""


def _rota(linha: dict[str, Any]) -> dict[str, Any]:
    setor = str(linha.get("Setor") or "")
    achado = _CODIGO.search(setor)
    status = str(linha.get("Status") or "Não iniciada")
    grupo = _GRUPOS.get(status, "outro")
    inicio = str(linha.get("Início") or "")
    fim = str(linha.get("Fim") or "")
    return {
        "codigo": achado.group(1).upper() if achado else "",
        "rota": setor,
        "frequencia": str(linha.get("Rota") or ""),
        "veiculo": str(linha.get("Veículo") or "—"),
        "placa": str(linha.get("Placa") or "—"),
        "status": status,
        "grupo": grupo,
        "percentual": _percentual(linha.get("% Concluída")),
        "inicio": inicio,
        "ultimo_gps": fim,
        "tempo_em_rota": _tempo(inicio, fim),
        "km_coleta": str(linha.get("Km Coleta") or "—"),
        "km_oficial": str(linha.get("Km Total") or "—"),
        "na_grade": str(linha.get("Na grade") or "").strip().lower() == "sim",
    }


def _percentual(valor: Any) -> float:
    texto = str(valor or "").strip().replace("%", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _tempo(inicio: str, fim: str) -> str:
    a = _HORA.search(inicio or "")
    b = _HORA.search(fim or "")
    if not a or not b:
        return ""
    t0 = datetime(2000, int(a.group(2)), int(a.group(1)), int(a.group(3)), int(a.group(4)))
    t1 = datetime(2000, int(b.group(2)), int(b.group(1)), int(b.group(3)), int(b.group(4)))
    if t1 < t0:
        t1 += timedelta(days=1)
    minutos = int((t1 - t0).total_seconds() // 60)
    horas, resto = divmod(minutos, 60)
    if horas:
        return f"{horas} h {resto} min"
    return f"{resto} min"


def _marcar_deslocamento(estado: dict[str, Any], _rotas: list[dict[str, Any]]) -> None:
    """Diz onde o veículo está agora. Não grava e não mexe no percentual da rota."""
    from datetime import date as dia_tipo

    from .config import parse_data
    from .live_mapa import _setores_do_turno, codigo_que_contem

    dia = parse_data(str(estado.get("data") or "")) or dia_tipo.today()
    turno = str(estado.get("turno") or "")
    setores = _setores_do_turno(dia, turno) if turno in {"diurno", "noturno"} else []
    for pos in estado.get("posicoes") or []:
        if not isinstance(pos, dict) or pos.get("lat") is None or pos.get("lon") is None:
            continue
        instante = str(pos.get("posicao_em") or pos.get("ultimo_gps") or "")
        pos["gps_ha"] = _idade_gps(instante)
        pos["gps_desatualizado"] = _gps_desatualizado(str(pos.get("telemetria") or ""), instante)
        if not setores:
            pos["rota_atual"] = ""
            pos["deslocamento"] = False
            pos["rotulo_rota"] = ""
            continue
        codigo = codigo_que_contem(float(pos["lat"]), float(pos["lon"]), setores)
        pos["rota_atual"] = codigo or ""
        pos["deslocamento"] = not codigo
        pos["rotulo_rota"] = codigo or "Em deslocamento"


def _idade_gps(valor: Any) -> str:
    """Tempo desde o último ponto já gravado. Não cria posição nova."""
    from datetime import datetime

    from .live_sync import agora_brt

    texto = str(valor or "").strip().replace("Z", "")
    if not texto:
        return ""
    try:
        momento = datetime.fromisoformat(texto[:19])
    except ValueError:
        return ""
    segundos = int((agora_brt() - momento).total_seconds())
    if segundos < 90:
        return ""
    minutos = max(1, segundos // 60)
    if minutos < 60:
        return f"há {minutos} min"
    horas, resto = divmod(minutos, 60)
    if horas >= 24:
        dias = horas // 24
        return f"há {dias} dia" if dias == 1 else f"há {dias} dias"
    return f"há {horas} h {resto} min"


def _gps_desatualizado(telemetria: str, instante: str) -> bool:
    """Pino antigo não conta como posição atual. Não apaga a coordenada."""
    from .live_sync import classificar_telemetria

    if telemetria in {"atrasado", "sem_dados", "sem_comunicacao"}:
        return True
    if not instante.strip():
        return True
    return classificar_telemetria(instante) != "atualizado"


def _faixa_percentual(valor: Any) -> tuple[str, str]:
    """0% não iniciada, 1% a 99% parcial, 100% concluída. O percentual não muda."""
    try:
        pct = float(valor)
    except (TypeError, ValueError):
        pct = 0.0
    if pct >= 100:
        return "concluida", "Concluída"
    if pct >= 1:
        return "parcial", "Parcial"
    return "nao", "Não iniciada"


def _exibir_classificacao(estado: dict[str, Any]) -> dict[str, Any]:
    """Classifica pela faixa do percentual. Não grava e não mexe na cobertura."""
    if not isinstance(estado, dict):
        return estado
    rotas = []
    for rota in estado.get("rotas") or []:
        if not isinstance(rota, dict):
            rotas.append(rota)
            continue
        item = dict(rota)
        if str(item.get("grupo") or "") != "apoio":
            item["grupo"], item["status"] = _faixa_percentual(item.get("percentual"))
        rotas.append(item)
    _marcar_deslocamento(estado, rotas)
    resumo = dict(estado.get("resumo") or {})
    previstas = [r for r in rotas if isinstance(r, dict) and r.get("na_grade") and r.get("grupo") != "apoio"]
    def n(grupo: str) -> int:
        return sum(1 for r in previstas if r.get("grupo") == grupo)
    resumo["previstas"] = len(previstas)
    resumo["concluidas"] = n("concluida")
    resumo["parciais"] = n("parcial")
    resumo["nao_iniciadas"] = n("nao")
    resumo["inicio"] = 0
    resumo["passagem"] = 0
    resumo["apoios"] = sum(1 for r in rotas if isinstance(r, dict) and r.get("grupo") == "apoio")
    estado["rotas"] = rotas
    estado["resumo"] = resumo
    return estado


def _resumo(rotas: list[dict[str, Any]]) -> dict[str, Any]:
    previstas = [r for r in rotas if r["na_grade"] and r["grupo"] != "apoio"]
    def n(grupo: str, base: list[dict[str, Any]] | None = None) -> int:
        fonte = previstas if base is None else base
        return sum(1 for r in fonte if r["grupo"] == grupo)
    media = round(sum(r["percentual"] for r in previstas) / len(previstas)) if previstas else 0
    return {
        "previstas": len(previstas),
        "concluidas": n("concluida"),
        "parciais": n("parcial"),
        "inicio": n("inicio"),
        "nao_iniciadas": n("nao"),
        "passagem": n("passagem"),
        "apoios": n("apoio", rotas),
        "percentual_geral": media,
    }
