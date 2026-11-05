"""Rotas que atravessam a virada de turno.

O início carimba data_turno/turno_origem. A conclusão continua no turno de
origem mesmo depois que o painel já acompanha o turno seguinte. O endpoint de
virada devolve o turno atual mais as rotas anteriores ainda abertas ou
concluídas após a virada.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .live_turno import classificar_instante, inicio_operacional, instante_do_ponto


def turno_anterior(dia: date, turno: str) -> tuple[date, str]:
    pedido = (turno or "").strip().lower()
    if pedido == "noturno":
        return dia, "diurno"
    return dia - timedelta(days=1), "noturno"


def origem_do_inicio(inicio: str, data: str, turno: str) -> tuple[str, str]:
    """Data e turno de origem a partir do horário de início da rota."""
    momento = instante_do_ponto(str(inicio or ""))
    if momento is not None:
        classificado = classificar_instante(momento)
        if classificado is not None:
            dia, pedido = classificado
            return dia.isoformat(), pedido
    return str(data or ""), str(turno or "").strip().lower()


def carimbar_origem(rota: dict[str, Any], data: str, turno: str) -> dict[str, Any]:
    """Preenche data_turno/turno_origem na primeira vez que a rota inicia."""
    item = dict(rota)
    data_turno = str(item.get("data_turno") or "").strip()
    turno_origem = str(item.get("turno_origem") or "").strip().lower()
    inicio = str(item.get("inicio") or item.get("entrada_hora") or "").strip()
    if not data_turno or not turno_origem:
        if inicio:
            data_turno, turno_origem = origem_do_inicio(inicio, data, turno)
        else:
            data_turno = data_turno or str(data or "")
            turno_origem = turno_origem or str(turno or "").strip().lower()
        item["data_turno"] = data_turno
        item["turno_origem"] = turno_origem
    return item


def _instante_texto(valor: Any) -> datetime | None:
    return instante_do_ponto(str(valor or ""))


def marcar_conclusao(rota: dict[str, Any], *, anterior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Grava concluido_em quando a rota passa a Concluída."""
    item = dict(rota)
    grupo = str(item.get("grupo") or "").strip().lower()
    if grupo != "concluida":
        return item
    ja = str(item.get("concluido_em") or "").strip()
    if not ja and anterior is not None:
        ja = str(anterior.get("concluido_em") or "").strip()
    if ja:
        item["concluido_em"] = ja
        return item
    for chave in ("saida_hora", "ultimo_gps", "atualizado_em"):
        momento = _instante_texto(item.get(chave))
        if momento is not None:
            item["concluido_em"] = momento.isoformat(timespec="seconds")
            return item
    from .live_sync import agora_brt

    item["concluido_em"] = agora_brt().isoformat(timespec="seconds")
    return item


def _rota_iniciada(rota: dict[str, Any]) -> bool:
    if str(rota.get("inicio") or "").strip():
        return True
    if str(rota.get("entrada_hora") or "").strip():
        return True
    try:
        return float(rota.get("percentual") or 0) > 0
    except (TypeError, ValueError):
        return False


def _ainda_aberta(rota: dict[str, Any]) -> bool:
    grupo = str(rota.get("grupo") or "").strip().lower()
    if grupo in {"concluida"}:
        return False
    if grupo in {"nao", ""} and not _rota_iniciada(rota):
        return False
    return _rota_iniciada(rota) or grupo in {"parcial", "passagem", "apoio", "inicio"}


def _concluida_apos(rota: dict[str, Any], virada: datetime) -> bool:
    if str(rota.get("grupo") or "").strip().lower() != "concluida":
        return False
    for chave in ("concluido_em", "saida_hora", "ultimo_gps"):
        momento = _instante_texto(rota.get(chave))
        if momento is not None and momento >= virada:
            return True
    return False


def filtrar_rotas_virada(rotas: list[dict[str, Any]], virada: datetime) -> list[dict[str, Any]]:
    """Ainda abertas no turno anterior, ou concluídas depois da virada."""
    saida: list[dict[str, Any]] = []
    for rota in rotas:
        if not isinstance(rota, dict):
            continue
        if _ainda_aberta(rota) or _concluida_apos(rota, virada):
            item = dict(rota)
            item["virada"] = True
            saida.append(item)
    return saida


def ler_rotas_turno_anterior(con, dia: date, turno: str) -> dict[str, Any]:
    """Rotas do turno imediatamente anterior relevantes para o painel atual."""
    from .live_db import ler_view
    from .live_report import _faixa_percentual

    pedido = (turno or "").strip().lower()
    ant_dia, ant_turno = turno_anterior(dia, pedido)
    virada = inicio_operacional(dia, pedido)
    view = ler_view(con, ant_dia.isoformat(), ant_turno)
    rotas_brutas = list((view or {}).get("rotas") or [])
    rotas = []
    for rota in filtrar_rotas_virada(rotas_brutas, virada):
        item = dict(rota)
        if str(item.get("grupo") or "") != "apoio":
            item["grupo"], item["status"] = _faixa_percentual(item.get("percentual"))
        item["data_turno"] = str(item.get("data_turno") or ant_dia.isoformat())
        item["turno_origem"] = str(item.get("turno_origem") or ant_turno)
        rotas.append(item)
    return {
        "data": ant_dia.isoformat(),
        "turno": ant_turno,
        "virada_em": virada.isoformat(timespec="seconds"),
        "rotas": rotas,
    }


def destino_da_conclusao(rota: dict[str, Any], data: str, turno: str) -> tuple[str, str]:
    """Sempre conclui no turno de origem, mesmo depois da virada."""
    item = carimbar_origem(rota, data, turno)
    data_turno = str(item.get("data_turno") or data or "").strip() or str(data or "")
    turno_origem = str(item.get("turno_origem") or turno or "").strip().lower() or str(turno or "").strip().lower()
    return data_turno, turno_origem


def _mesclar_pontos(*listas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vistos: set[str] = set()
    saida: list[dict[str, Any]] = []
    for lista in listas:
        for ponto in lista or []:
            ts = str(ponto.get("timestamp") or ponto.get("ts") or "").strip()
            if not ts or ts in vistos:
                continue
            vistos.add(ts)
            saida.append(ponto)
    saida.sort(key=lambda item: str(item.get("timestamp") or ""))
    return saida


def atualizar_conclusoes_virada(
    con,
    output_dir: Path,
    dia: date,
    turno: str,
    *,
    agora: datetime | None = None,
) -> list[str]:
    """Recalcula rotas abertas do turno anterior com GPS já gravado nos dois turnos.

    A conclusão é gravada em data_turno/turno_origem. Não reabre o turno encerrado
    e não altera endpoints existentes.
    """
    from .dificil_acesso import eh_basculante
    from .live_db import gravar_rotas, ler_placas, ler_pontos_placa, ler_view
    from .live_report import _rota
    from .live_sync import agora_brt, _anexar_geometria_visual
    from .live_turno import montar_resumo
    from .unificado import _linha, analisar_codigos

    momento = agora or agora_brt()
    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        return []
    ant_dia, ant_turno = turno_anterior(dia, pedido)
    view = ler_view(con, ant_dia.isoformat(), ant_turno)
    if view is None:
        return []
    abertas = [
        rota
        for rota in view.get("rotas") or []
        if _ainda_aberta(rota) and str(rota.get("codigo") or "").strip()
    ]
    if not abertas:
        return []

    placas_uteis: set[str] = set()
    for rota in abertas:
        for chave in ("placa", "placa_principal"):
            placa = str(rota.get(chave) or "").strip()
            if placa and placa != "—":
                placas_uteis.add(placa)
        for placa in rota.get("placas_apoio") or []:
            texto = str(placa or "").strip()
            if texto:
                placas_uteis.add(texto)

    caminhoes = []
    placas_lidas = {
        str(row["placa"]): row
        for row in [
            *ler_placas(con, ant_dia.isoformat(), ant_turno),
            *ler_placas(con, dia.isoformat(), pedido),
        ]
    }
    for placa, row in placas_lidas.items():
        if placas_uteis and placa not in placas_uteis:
            continue
        if eh_basculante(placa, str(row["veiculo"] or "")):
            continue
        pontos = _mesclar_pontos(
            ler_pontos_placa(con, ant_dia.isoformat(), ant_turno, placa),
            ler_pontos_placa(con, dia.isoformat(), pedido, placa),
        )
        if len(pontos) < 8:
            continue
        caminhoes.append(
            {
                "id": placa,
                "fonte": "segtrack",
                "fonte_nome": "SEGTRACK",
                "veiculo": row["veiculo"] or placa,
                "placa": placa,
                "inlog": "",
                "pontos": pontos,
            }
        )
    if not caminhoes:
        return []

    codigos = [str(rota.get("codigo") or "").upper() for rota in abertas]
    setores = analisar_codigos(ant_dia, caminhoes, set(codigos), ant_turno)
    calculadas = []
    for setor in setores:
        rota = _rota(_linha(setor))
        codigo = str(setor.get("codigo") or rota.get("codigo") or "").strip().upper()
        if codigo:
            rota["codigo"] = codigo
        calculadas.append(rota)
    _anexar_geometria_visual(setores, caminhoes, calculadas)

    anteriores = {str(rota.get("codigo") or "").upper(): rota for rota in abertas}
    gravar: list[dict[str, Any]] = []
    for rota in calculadas:
        codigo = str(rota.get("codigo") or "").upper()
        if codigo not in anteriores:
            continue
        item = carimbar_origem(rota, ant_dia.isoformat(), ant_turno)
        item["data_turno"] = ant_dia.isoformat()
        item["turno_origem"] = ant_turno
        item = marcar_conclusao(item, anterior=anteriores.get(codigo))
        destino_data, destino_turno = destino_da_conclusao(item, ant_dia.isoformat(), ant_turno)
        if (destino_data, destino_turno) != (ant_dia.isoformat(), ant_turno):
            # Conclusão fora do par atual: grava no destino e mantém a origem.
            gravar_rotas(con, destino_data, destino_turno, [item])
            continue
        gravar.append(item)
    if gravar:
        gravar_rotas(con, ant_dia.isoformat(), ant_turno, gravar)

    cab = view.get("turno")
    situacao = str(cab["situacao"] or "") if cab is not None and "situacao" in cab.keys() else ""
    if situacao == "ENCERRADO":
        import json

        resumo = montar_resumo(con, ant_dia, ant_turno, str(cab["fechado_em"] or momento.isoformat(timespec="seconds")))
        con.execute(
            """
            UPDATE turno SET resumo_json = ?, atualizado_em = ?
            WHERE data = ? AND turno = ?
            """,
            (
                json.dumps(resumo, ensure_ascii=False, separators=(",", ":")),
                momento.isoformat(timespec="seconds"),
                ant_dia.isoformat(),
                ant_turno,
            ),
        )
    return [str(item.get("codigo") or "") for item in gravar]


def ler_estado_virada(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    job_rodando: bool = False,
) -> dict[str, Any]:
    """Estado do turno atual + rotas do turno anterior ainda relevantes."""
    from .config import parse_data
    from .live_db import conectar
    from .live_report import ler_estado

    estado = ler_estado(output_dir, data, turno, job_rodando=job_rodando)
    pedido = str(estado.get("turno") or turno or "diurno").strip().lower()
    dia = parse_data(str(estado.get("data") or data or "")) or date.today()
    anterior = {"data": "", "turno": "", "virada_em": "", "rotas": []}
    try:
        con = conectar()
    except RuntimeError:
        estado["turno_anterior"] = anterior
        estado["rotas_turno_anterior"] = []
        return estado
    try:
        anterior = ler_rotas_turno_anterior(con, dia, pedido)
    finally:
        con.close()
    estado["turno_anterior"] = {
        "data": anterior.get("data") or "",
        "turno": anterior.get("turno") or "",
        "virada_em": anterior.get("virada_em") or "",
    }
    estado["rotas_turno_anterior"] = list(anterior.get("rotas") or [])
    return estado
