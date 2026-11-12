"""Rotas que atravessam a virada de turno.

O início carimba data_turno/turno_origem. A conclusão continua no turno de
origem mesmo depois que o painel já acompanha o turno seguinte. O endpoint de
virada devolve o turno atual mais as rotas anteriores ainda abertas ou
encerradas após a virada (com % final congelado até o fim do turno atual).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time as hora, timedelta
from pathlib import Path
from typing import Any

from .live_turno import classificar_instante, inicio_operacional, instante_do_ponto

SEM_PULSO = timedelta(minutes=30)
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _hora_env(nome: str, padrao: str) -> hora:
    bruto = (os.getenv(nome) or "").strip() or padrao
    m = _HHMM.match(bruto)
    if not m:
        m = _HHMM.match(padrao)
    assert m is not None
    return hora(int(m.group(1)) % 24, int(m.group(2)) % 60, 0)


def corte_gps_turno_origem(ant_dia: date, ant_turno: str) -> datetime:
    """Limite fixo dos pulsos da rota do turno anterior.

    Noturno: até VIRADA_CORTE_NOTURNO (padrão 12:00) do dia seguinte.
    Diurno: até VIRADA_CORTE_DIURNO (padrão 23:00) do mesmo dia.
    """
    pedido = (ant_turno or "").strip().lower()
    if pedido == "noturno":
        relogio = _hora_env("VIRADA_CORTE_NOTURNO", "12:00")
        return datetime.combine(ant_dia + timedelta(days=1), relogio)
    relogio = _hora_env("VIRADA_CORTE_DIURNO", "23:00")
    return datetime.combine(ant_dia, relogio)


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


def _norm_placa(placa: Any) -> str:
    return "".join(ch for ch in str(placa or "").upper() if ch.isalnum())


def _placa_util(placa: Any) -> str:
    """Normaliza placa real; descarta marcadores tipo 'sem apoio identificado'."""
    texto = str(placa or "").strip()
    if not texto or texto in {"—", "-", "sem apoio"}:
        return ""
    chave = _norm_placa(texto)
    if not chave or chave == "—":
        return ""
    # Texto livre sem dígito (ex.: SEMAPOIOIDENTIFICADO) não é placa.
    if not any(ch.isdigit() for ch in chave):
        return ""
    return chave


def placas_da_rota(rota: dict[str, Any]) -> set[str]:
    """Só as placas da própria rota (principal + apoios). Sem sobreposta."""
    saida: set[str] = set()
    for chave in ("placa", "placa_principal"):
        chave_n = _placa_util(rota.get(chave))
        if chave_n:
            saida.add(chave_n)
    for placa in rota.get("placas_apoio") or []:
        chave_n = _placa_util(placa)
        if chave_n:
            saida.add(chave_n)
    for executor in rota.get("executores") or []:
        if not isinstance(executor, dict):
            continue
        papel = str(executor.get("papel") or "").strip().lower()
        if papel not in {"principal", "apoio"}:
            continue
        chave_n = _placa_util(executor.get("placa"))
        if chave_n:
            saida.add(chave_n)
    apoio = str(rota.get("apoio") or "").strip()
    if apoio and apoio.lower() not in {"—", "-", "sem apoio", "sem apoio identificado"}:
        for pedaco in apoio.replace(";", ",").split(","):
            chave_n = _placa_util(pedaco)
            if chave_n:
                saida.add(chave_n)
    return saida


def _rota_encerrada(rota: dict[str, Any]) -> bool:
    return bool(str(rota.get("concluido_em") or "").strip())


def marcar_conclusao(rota: dict[str, Any], *, anterior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Grava concluido_em quando a rota passa a Concluída ou já foi encerrada."""
    item = dict(rota)
    ja = str(item.get("concluido_em") or "").strip()
    if not ja and anterior is not None:
        ja = str(anterior.get("concluido_em") or "").strip()
    if ja:
        item["concluido_em"] = ja
        return item
    grupo = str(item.get("grupo") or "").strip().lower()
    if grupo != "concluida":
        return item
    for chave in ("saida_hora", "ultimo_gps", "atualizado_em"):
        momento = _instante_texto(item.get(chave))
        if momento is not None:
            item["concluido_em"] = momento.isoformat(timespec="seconds")
            return item
    from .live_sync import agora_brt

    item["concluido_em"] = agora_brt().isoformat(timespec="seconds")
    return item


def _ultimo_pulso_valido(
    rota: dict[str, Any],
    *,
    ultimo_gps_placas: datetime | None = None,
) -> datetime | None:
    """Último pulso das placas da rota (preferência) ou do que já está gravado."""
    if ultimo_gps_placas is not None:
        return ultimo_gps_placas
    for chave in ("ultimo_gps", "saida_hora", "concluido_em"):
        momento = _instante_texto(rota.get(chave))
        if momento is not None:
            return momento
    return None


def encerrar_rota_virada(
    rota: dict[str, Any],
    *,
    virada: datetime,
    agora: datetime,
    ultimo_pulso: datetime | None = None,
    corte_gps: datetime | None = None,
) -> dict[str, Any] | None:
    """Fecha com 100%, 30 min sem pulso da placa ou ao passar do corte fixo.

    Congela o % atual. concluido_em = último pulso válido até o corte
    (nunca depois do corte; no limite usa esse último pulso, não o horário do corte).
    """
    item = dict(rota)
    if _rota_encerrada(item):
        return None
    try:
        pct = float(item.get("percentual") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    limite = corte_gps or virada
    pulso = _ultimo_pulso_valido(item, ultimo_gps_placas=ultimo_pulso)
    if pulso is not None and pulso > limite:
        pulso = limite
    motivo = ""
    if pct >= 100:
        motivo = "100"
        item["grupo"] = "concluida"
        item["status"] = "Concluída"
        item["percentual"] = max(pct, 100.0)
    elif pulso is not None and agora - pulso >= SEM_PULSO:
        motivo = "sem_pulso"
    elif agora >= limite:
        motivo = "limite_corte"
    else:
        return None
    if motivo != "100":
        from .live_report import _faixa_percentual

        item["grupo"], item["status"] = _faixa_percentual(pct)
        if item["grupo"] != "concluida":
            item["status"] = "Encerrada"
    concluido = pulso or limite
    if concluido > limite:
        concluido = limite
    item["concluido_em"] = concluido.isoformat(timespec="seconds")
    item["virada_encerrada"] = True
    item["motivo_encerramento"] = motivo
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
    if _rota_encerrada(rota):
        return False
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


def _encerrada_na_secao(rota: dict[str, Any], virada: datetime) -> bool:
    """Só entra na seção se concluido_em >= virada.

    Encerrada antes da virada fica só no histórico normal do turno de origem.
    """
    if not _rota_encerrada(rota):
        return False
    momento = _instante_texto(rota.get("concluido_em"))
    return momento is not None and momento >= virada


def _origem_eh_turno_anterior(
    rota: dict[str, Any],
    ant_dia: date,
    ant_turno: str,
) -> bool:
    """Aceita só o par imediatamente anterior; origem mais antiga fica de fora."""
    data_turno = str(rota.get("data_turno") or "").strip()
    turno_origem = str(rota.get("turno_origem") or "").strip().lower()
    if not data_turno and not turno_origem:
        # View já é do turno anterior; carimbo faltando não reabre turnos velhos.
        return True
    if data_turno and data_turno != ant_dia.isoformat():
        return False
    if turno_origem and turno_origem != ant_turno:
        return False
    return True


def filtrar_rotas_virada(
    rotas: list[dict[str, Any]],
    virada: datetime,
    *,
    ant_dia: date | None = None,
    ant_turno: str | None = None,
) -> list[dict[str, Any]]:
    """Ainda abertas, ou com concluido_em >= virada. Antes da virada só no histórico."""
    pedido_ant = str(ant_turno or "").strip().lower() or None
    saida: list[dict[str, Any]] = []
    for rota in rotas:
        if not isinstance(rota, dict):
            continue
        if ant_dia is not None and pedido_ant is not None:
            if not _origem_eh_turno_anterior(rota, ant_dia, pedido_ant):
                continue
        if _ainda_aberta(rota) or _concluida_apos(rota, virada) or _encerrada_na_secao(rota, virada):
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
    for rota in filtrar_rotas_virada(
        rotas_brutas,
        virada,
        ant_dia=ant_dia,
        ant_turno=ant_turno,
    ):
        item = dict(rota)
        if str(item.get("grupo") or "") != "apoio":
            item["grupo"], item["status"] = _faixa_percentual(item.get("percentual"))
        if _rota_encerrada(item) and str(item.get("grupo") or "") != "concluida":
            item["status"] = "Encerrada"
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


def _ultimo_ts_pontos(pontos: list[dict[str, Any]]) -> datetime | None:
    ultimo: datetime | None = None
    for ponto in pontos or []:
        momento = _instante_texto(ponto.get("timestamp") or ponto.get("ts"))
        if momento is not None and (ultimo is None or momento > ultimo):
            ultimo = momento
    return ultimo


def inicio_placa_turno_atual(
    rotas_atuais: list[dict[str, Any]] | None,
    placa: str,
) -> datetime | None:
    """Primeiro início/entrada da placa numa rota do turno atual."""
    alvo = _norm_placa(placa)
    if not alvo or not rotas_atuais:
        return None
    melhor: datetime | None = None
    for rota in rotas_atuais:
        if not isinstance(rota, dict):
            continue
        if alvo not in placas_da_rota(rota):
            continue
        for chave in ("entrada_hora", "inicio"):
            momento = _instante_texto(rota.get(chave))
            if momento is None:
                continue
            if melhor is None or momento < melhor:
                melhor = momento
            break
    return melhor


def filtrar_pontos_virada(
    pontos: list[dict[str, Any]] | None,
    *,
    limite_apos_virada: datetime,
    inicio_rota_nova: datetime | None = None,
) -> list[dict[str, Any]]:
    """Descarta pulsos depois do corte fixo e a partir do 1º pulso na rota nova."""
    saida: list[dict[str, Any]] = []
    for ponto in pontos or []:
        momento = _instante_texto(ponto.get("timestamp") or ponto.get("ts"))
        if momento is None:
            continue
        if momento > limite_apos_virada:
            continue
        if inicio_rota_nova is not None and momento >= inicio_rota_nova:
            continue
        saida.append(ponto)
    return saida


def _caminhao_da_placa(placa: str, row: Any, pontos: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": placa,
        "fonte": "segtrack",
        "fonte_nome": "SEGTRACK",
        "veiculo": (row["veiculo"] if row is not None else None) or placa,
        "placa": placa,
        "inlog": "",
        "pontos": pontos,
    }


def atualizar_conclusoes_virada(
    con,
    output_dir: Path,
    dia: date,
    turno: str,
    *,
    agora: datetime | None = None,
) -> list[str]:
    """Recalcula rotas abertas do turno anterior só com GPS das placas da rota.

    Pulsos depois do corte fixo (12:00/23:00, configurável) não contam. Se a placa
    já está numa rota do turno atual, só entram pulsos até o primeiro na rota nova.
    Encerra com 100%, 30 min sem pulso ou ao passar do corte; concluido_em = último
    pulso válido até o corte.
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
    virada = inicio_operacional(dia, pedido)
    limite_gps = corte_gps_turno_origem(ant_dia, ant_turno)
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

    view_atual = ler_view(con, dia.isoformat(), pedido) or {}
    rotas_atuais = list(view_atual.get("rotas") or [])

    placas_lidas = {
        _norm_placa(row["placa"]): row
        for row in [
            *ler_placas(con, ant_dia.isoformat(), ant_turno),
            *ler_placas(con, dia.isoformat(), pedido),
        ]
        if _norm_placa(row["placa"])
    }
    pontos_brutos: dict[str, list[dict[str, Any]]] = {}
    for chave, row in placas_lidas.items():
        placa_txt = str(row["placa"] or chave)
        if eh_basculante(placa_txt, str(row["veiculo"] or "")):
            continue
        pontos = _mesclar_pontos(
            ler_pontos_placa(con, ant_dia.isoformat(), ant_turno, placa_txt),
            ler_pontos_placa(con, dia.isoformat(), pedido, placa_txt),
        )
        if pontos:
            pontos_brutos[chave] = pontos

    anteriores = {str(rota.get("codigo") or "").upper(): rota for rota in abertas}
    gravar: list[dict[str, Any]] = []
    alteradas: list[str] = []

    for aberta in abertas:
        codigo = str(aberta.get("codigo") or "").strip().upper()
        if not codigo:
            continue
        autorizadas = placas_da_rota(aberta)
        caminhoes: list[dict[str, Any]] = []
        ultimo_pulso_placas: datetime | None = None
        for chave in autorizadas:
            row = placas_lidas.get(chave)
            placa_txt = str(row["placa"] if row is not None else chave)
            inicio_nova = inicio_placa_turno_atual(rotas_atuais, placa_txt)
            pontos = filtrar_pontos_virada(
                pontos_brutos.get(chave) or [],
                limite_apos_virada=limite_gps,
                inicio_rota_nova=inicio_nova,
            )
            if len(pontos) < 8:
                # Ainda conta o pulso para o encerramento, mesmo sem recalcular cobertura.
                pulso = _ultimo_ts_pontos(pontos)
                if pulso is not None and (ultimo_pulso_placas is None or pulso > ultimo_pulso_placas):
                    ultimo_pulso_placas = pulso
                continue
            caminhoes.append(_caminhao_da_placa(placa_txt, row, pontos))
            pulso = _ultimo_ts_pontos(pontos)
            if pulso is not None and (ultimo_pulso_placas is None or pulso > ultimo_pulso_placas):
                ultimo_pulso_placas = pulso
        if ultimo_pulso_placas is None:
            # Sem GPS cortado: respeita o último da rota, mas nunca depois do limite 3h.
            bruto = _ultimo_pulso_valido(aberta)
            if bruto is not None and bruto > limite_gps:
                ultimo_pulso_placas = limite_gps
            else:
                # Se a placa já entrou em rota nova, não usa pulso depois desse início.
                cortes_nova = [
                    inicio_placa_turno_atual(rotas_atuais, placa)
                    for placa in autorizadas
                ]
                cortes_nova = [c for c in cortes_nova if c is not None]
                if bruto is not None and cortes_nova and bruto >= min(cortes_nova):
                    # Último válido seria imediatamente antes da rota nova; sem ponto, congela no corte.
                    ultimo_pulso_placas = min(cortes_nova) - timedelta(seconds=1)
                else:
                    ultimo_pulso_placas = bruto

        item = dict(aberta)
        if caminhoes and autorizadas:
            setores = analisar_codigos(
                ant_dia,
                caminhoes,
                {codigo},
                ant_turno,
                placas_executoras=autorizadas,
            )
            calculadas = []
            for setor in setores:
                rota = _rota(_linha(setor))
                cod = str(setor.get("codigo") or rota.get("codigo") or "").strip().upper()
                if cod:
                    rota["codigo"] = cod
                calculadas.append(rota)
            _anexar_geometria_visual(setores, caminhoes, calculadas)
            calculada = next((r for r in calculadas if str(r.get("codigo") or "").upper() == codigo), None)
            if calculada is not None:
                # Preserva identidade da rota; só atualiza cobertura/GPS das placas dela.
                # Não sobrescreve executores_json/apoio: o recálculo pode omitir apoio
                # sem GPS e apagar o vínculo usado no corte por placa do turno atual.
                for chave in (
                    "percentual",
                    "status",
                    "grupo",
                    "veiculo",
                    "resumo_veiculos",
                    "ultimo_gps",
                    "saida_hora",
                    "saida_lat",
                    "saida_lon",
                    "entrada_hora",
                    "entrada_lat",
                    "entrada_lon",
                    "inicio",
                    "tempo_em_rota",
                    "km_coleta",
                    "km_oficial",
                    "trechos_feitos",
                    "trechos_faltando",
                ):
                    if chave in calculada and calculada.get(chave) not in (None, ""):
                        item[chave] = calculada[chave]
                item["placa"] = aberta.get("placa") or item.get("placa")
                item["placa_principal"] = aberta.get("placa_principal") or item.get("placa_principal")
                if aberta.get("placas_apoio") is not None:
                    item["placas_apoio"] = aberta.get("placas_apoio")
                if aberta.get("apoio") not in (None, ""):
                    item["apoio"] = aberta.get("apoio")
                # Sem _geo: mantém executores_json já gravado (apoio/principal).
                item.pop("_geo", None)
                item.pop("executores_json", None)
                if ultimo_pulso_placas is not None:
                    item["ultimo_gps"] = ultimo_pulso_placas.isoformat(timespec="seconds")

        item = carimbar_origem(item, ant_dia.isoformat(), ant_turno)
        item["data_turno"] = ant_dia.isoformat()
        item["turno_origem"] = ant_turno
        item = marcar_conclusao(item, anterior=anteriores.get(codigo))
        fechada = encerrar_rota_virada(
            item,
            virada=virada,
            agora=momento,
            ultimo_pulso=ultimo_pulso_placas,
            corte_gps=limite_gps,
        )
        if fechada is not None:
            item = fechada
        elif not caminhoes:
            # Sem GPS novo e sem critério de encerramento: não mexe.
            continue

        destino_data, destino_turno = destino_da_conclusao(item, ant_dia.isoformat(), ant_turno)
        if (destino_data, destino_turno) != (ant_dia.isoformat(), ant_turno):
            gravar_rotas(con, destino_data, destino_turno, [item])
            alteradas.append(codigo)
            continue
        gravar.append(item)
        alteradas.append(codigo)

    if gravar:
        gravar_rotas(con, ant_dia.isoformat(), ant_turno, gravar)

    cab = view.get("turno")
    situacao = str(cab["situacao"] or "") if cab is not None and "situacao" in cab.keys() else ""
    if situacao == "ENCERRADO" and alteradas:
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
    return alteradas


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
        con = conectar(leitura=True)
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
