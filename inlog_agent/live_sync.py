"""Um único sincronizador do turno. O painel só lê o estado já gravado.

Posição da frota: consulta leve, sem recalcular rota.
GPS incremental: só o intervalo depois do último ponto, com folga.
Cobertura: o mesmo cálculo do relatório, apenas nas rotas com ponto novo.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, time as hora, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import janela_datetime
from .live_db import (
    atualizar_turno,
    conectar,
    fechar_turno,
    gravar_rotas,
    gravar_turno,
    inserir_pontos,
    ler_placas,
    ler_pontos_placa,
    ler_view,
    marcar_placa,
    turno_existe,
)

_SYNC = threading.Lock()
_OCUPADO = False
_THREAD: threading.Thread | None = None
FOLGA_S = 90
POSICAO_S = 30
GPS_S = 120
ATRASO_S = 15 * 60
_RECON_ESPERA_S = (0, 180, 480, 900)
_CICLOS = 0
try:
    from zoneinfo import ZoneInfo

    _BRT = ZoneInfo("America/Sao_Paulo")
except Exception:
    _BRT = timezone(timedelta(hours=-3))


def agora_brt() -> datetime:
    """Relógio do turno. No Render o processo está em UTC; a operação é em Brasília."""
    return datetime.now(_BRT).replace(tzinfo=None)


def ocupada() -> bool:
    with _SYNC:
        return _OCUPADO


def sincronizacao_ativa() -> bool:
    return bool(_THREAD and _THREAD.is_alive())


def ciclos_executados() -> int:
    return _CICLOS


def espera_reconstrucao(tentativas: int) -> int:
    indice = max(0, int(tentativas))
    if indice >= len(_RECON_ESPERA_S):
        return _RECON_ESPERA_S[-1]
    return _RECON_ESPERA_S[indice]


def turno_operacional(agora: datetime | None = None) -> tuple[date, str]:
    momento = agora or agora_brt()
    if momento.hour >= 19:
        return momento.date(), "noturno"
    if momento.hour < 6:
        return momento.date() - timedelta(days=1), "noturno"
    return momento.date(), "diurno"


def janela_ainda_aberta(dia: date, turno: str, agora: datetime | None = None) -> bool:
    momento = agora or agora_brt()
    if turno == "noturno":
        fim = datetime.combine(dia + timedelta(days=1), hora(5, 59, 59))
    else:
        fim = datetime.combine(dia, hora(18, 59, 59))
    return momento <= fim


def inicio_da_consulta(
    ultimo: str | None,
    inicio_turno: datetime,
    *,
    folga_s: int = FOLGA_S,
) -> datetime:
    if not (ultimo or "").strip():
        return inicio_turno
    lido = _parse_ts(ultimo)
    if lido is None:
        return inicio_turno
    return max(inicio_turno, lido - timedelta(seconds=folga_s))


def codigos_com_ponto_novo(
    pontos: list[dict[str, Any]],
    limites: list[dict[str, Any]],
) -> list[str]:
    """Rotas cujo retângulo recebeu ponto. Concluída sem ponto relacionado fica de fora."""
    sujos: list[str] = []
    for limite in limites:
        codigo = str(limite.get("codigo") or "")
        if not codigo:
            continue
        caixa = (limite["minx"], limite["miny"], limite["maxx"], limite["maxy"])
        folga = float(limite.get("folga") or 180)
        bateu = False
        for ponto in pontos:
            xy = ponto.get("xy")
            if not xy or len(xy) < 2:
                continue
            x, y = float(xy[0]), float(xy[1])
            if caixa[0] - folga <= x <= caixa[2] + folga and caixa[1] - folga <= y <= caixa[3] + folga:
                bateu = True
                break
        if not bateu:
            continue
        sujos.append(codigo)
    return sujos


def classificar_telemetria(ultimo: str | None, agora: datetime | None = None) -> str:
    momento = agora or agora_brt()
    lido = _parse_ts(ultimo or "")
    if lido is None:
        return "sem_comunicacao"
    idade = (momento - lido).total_seconds()
    if idade <= ATRASO_S:
        return "atualizado"
    return "atrasado"


def ler_painel(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    atualizando: bool = False,
) -> dict[str, Any]:
    from .live_report import _hora_curta, _ler_pasta, _mapa_url, _periodo_padrao, _resumo, montar_estado

    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        raise ValueError("Turno inválido. Use diurno ou noturno.")
    from .config import parse_data

    dia = parse_data(data) or date.today()
    try:
        con = conectar()
    except RuntimeError as exc:
        _avisar_disco(exc)
        return _painel_sem_persistencia(dia, pedido, str(exc))
    try:
        _importar_json_se_vazio(con, output_dir, dia, pedido)
        view = ler_view(con, dia.isoformat(), pedido)
        con.commit()
    finally:
        con.close()
    pasta, _diario, _meta, _falha = _ler_pasta(Path(output_dir), dia, pedido)
    if view is None:
        estado = montar_estado(output_dir, dia.isoformat(), pedido)
        estado["fonte"] = "json"
        estado["telemetria"] = ""
        estado["aberto"] = True
        estado["posicoes"] = []
        if atualizando and not estado.get("falha"):
            estado["atualizando"] = True
            estado["aviso"] = "Atualizando dados..."
        return estado
    cab = view["turno"]
    rotas = view["rotas"]
    atualizado = str(cab["atualizado_em"] or cab["ultima_sincronizacao"] or "")
    falha = str(cab["falha"] or "")
    aviso = ""
    if falha and rotas:
        aviso = "Falha na atualização — exibindo dados de " + (_hora_curta(atualizado) or atualizado)
    elif falha:
        aviso = "Falha na atualização."
    elif not rotas:
        aviso = "Ainda não há estado salvo deste turno."
    elif atualizando:
        aviso = "Atualizando dados..."
    return {
        "ok": not falha,
        "vazio": not rotas,
        "fonte": "sqlite",
        "empresa": "Segtrack",
        "data": dia.isoformat(),
        "turno": pedido,
        "periodo": str(cab["periodo"] or "") or _periodo_padrao(dia, pedido),
        "hora_inicio": str(cab["hora_inicio"] or ""),
        "hora_fim": str(cab["hora_fim"] or ""),
        "data_fim": str(cab["data_fim"] or ""),
        "atualizado_em": atualizado,
        "atualizado_hora": _hora_curta(atualizado),
        "atualizando": atualizando,
        "falha": falha,
        "aviso": aviso,
        "telemetria": str(cab["telemetria"] or ""),
        "aberto": bool(cab["aberto"]),
        "mapa_url": _mapa_url(output_dir, pasta),
        "resumo": _resumo(rotas),
        "rotas": rotas,
        "posicoes": view["posicoes"],
    }


def _painel_sem_persistencia(dia: date, pedido: str, texto: str) -> dict[str, Any]:
    from .live_report import _periodo_padrao, _resumo

    return {
        "ok": False,
        "vazio": True,
        "fonte": "erro",
        "empresa": "Segtrack",
        "data": dia.isoformat(),
        "turno": pedido,
        "periodo": _periodo_padrao(dia, pedido),
        "hora_inicio": "19:00" if pedido == "noturno" else "06:00",
        "hora_fim": "05:59" if pedido == "noturno" else "18:59",
        "data_fim": "",
        "atualizado_em": "",
        "atualizado_hora": "",
        "atualizando": False,
        "falha": texto,
        "aviso": texto,
        "telemetria": "",
        "aberto": False,
        "mapa_url": "",
        "resumo": _resumo([]),
        "rotas": [],
        "posicoes": [],
    }


def pedir(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    job_rodando: bool = False,
    ciclo: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    global _OCUPADO
    with _SYNC:
        if _OCUPADO or job_rodando:
            estado = ler_painel(output_dir, data, turno, atualizando=True)
            estado["aviso"] = "Atualização em andamento..."
            return estado
        _OCUPADO = True
    try:
        executar = ciclo or ciclo_gps
        executar(output_dir, data, turno)
    except Exception as exc:
        _avisar_disco(exc)
        try:
            _registrar_falha(output_dir, data, turno, str(exc))
        except Exception as falha_registro:
            _avisar_disco(falha_registro)
    finally:
        with _SYNC:
            _OCUPADO = False
    return ler_painel(output_dir, data, turno)


def ciclo_gps(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    agora: datetime | None = None,
    buscar_historico: Callable[..., list[dict[str, Any]]] | None = None,
    recalcular: Callable[..., list[dict[str, Any]]] | None = None,
    limites: list[dict[str, Any]] | None = None,
    descobrir: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    from .config import parse_data

    pedido = (turno or "").strip().lower()
    dia = parse_data(data) or date.today()
    momento = agora or agora_brt()
    con = conectar()
    try:
        _importar_json_se_vazio(con, output_dir, dia, pedido)
        if not turno_existe(con, dia.isoformat(), pedido):
            from .live_report import _periodo_padrao

            gravar_turno(
                con,
                {
                    "data": dia.isoformat(),
                    "turno": pedido,
                    "aberto": 1,
                    "periodo": _periodo_padrao(dia, pedido),
                    "hora_inicio": "19:00" if pedido == "noturno" else "06:00",
                    "hora_fim": "05:59" if pedido == "noturno" else "18:59",
                    "falha": "Sem JSON do turno. A primeira sincronização busca o GPS na Fulltrack.",
                    "atualizado_em": momento.isoformat(timespec="seconds"),
                },
            )
            con.commit()
        cab = con.execute(
            "SELECT aberto FROM turno WHERE data = ? AND turno = ?",
            (dia.isoformat(), pedido),
        ).fetchone()
        if cab and not cab["aberto"]:
            return {"novos": 0, "rotas": [], "fechado": True}
        if not janela_ainda_aberta(dia, pedido, momento):
            fechar_turno(con, dia.isoformat(), pedido)
            con.commit()
            return {"novos": 0, "rotas": [], "fechado": True}
        inicio_turno, _fim = janela_datetime(
            dia,
            "19:00" if pedido == "noturno" else "06:00",
            "05:59" if pedido == "noturno" else "18:59",
        )
        placas = ler_placas(con, dia.isoformat(), pedido)
        pesada = _sem_cursor(placas)
        inicio_pesada = 0.0
        if pesada:
            falta = _atraso_reconstrucao(con, dia.isoformat(), pedido, momento)
            if falta is not None:
                prontas = [item for item in placas if str(item["last_gps_timestamp"] or "").strip()]
                if not prontas:
                    print(
                        f"Reconstrução completa adiada por {falta}s após falha. O histórico inteiro não será baixado de novo.",
                        flush=True,
                    )
                    return {"novos": 0, "rotas": [], "adiado": True, "espera_s": falta}
                placas = prontas
            else:
                _marcar_tentativa(con, dia.isoformat(), pedido, momento)
                con.commit()
                inicio_pesada = time.perf_counter()
                print(
                    f"Reconstrução completa do turno {dia.isoformat()} {pedido}: uma vez, porque ainda não há last_gps_timestamp.",
                    flush=True,
                )
                if not placas:
                    try:
                        achadas = (descobrir or _descobrir_frota)()
                    except Exception as exc:
                        atualizar_turno(con, dia.isoformat(), pedido, falha=str(exc))
                        con.commit()
                        print(f"Reconstrução falhou ao obter a frota: {exc}", flush=True)
                        return {"novos": 0, "rotas": [], "falha": str(exc)}
                    for item in achadas:
                        placa = str(item.get("placa") or "").strip()
                        if not placa:
                            continue
                        marcar_placa(
                            con,
                            dia.isoformat(),
                            pedido,
                            placa,
                            veiculo=str(item.get("veiculo") or placa),
                            vehicle_id=str(item.get("vehicle_id") or ""),
                        )
                    placas = ler_placas(con, dia.isoformat(), pedido)
                    if not placas:
                        atualizar_turno(
                            con,
                            dia.isoformat(),
                            pedido,
                            falha="Fulltrack não devolveu placas da frota. O banco permanece sem rotas.",
                        )
                        con.commit()
                        print("Reconstrução falhou: Fulltrack não devolveu placas.", flush=True)
                        return {"novos": 0, "rotas": []}
        novos_total = 0
        pontos_novos: list[dict[str, Any]] = []
        falha = ""
        for placa in placas:
            cursor = str(placa["last_gps_timestamp"] or "")
            começo = inicio_da_consulta(cursor, inicio_turno)
            try:
                eventos = (buscar_historico or _historico_fulltrack)(
                    placa["placa"],
                    str(placa["vehicle_id"] or ""),
                    começo,
                    momento,
                )
            except Exception as exc:
                falha = str(exc)
                continue
            recebidos = [_ponto_local(item, placa["placa"]) for item in eventos]
            recebidos = [item for item in recebidos if item]
            gravados = inserir_pontos(con, dia.isoformat(), pedido, placa["placa"], recebidos)
            novos_total += gravados
            if gravados:
                maior = max(str(item["timestamp"]) for item in recebidos if item.get("timestamp"))
                marcar_placa(
                    con,
                    dia.isoformat(),
                    pedido,
                    placa["placa"],
                    last_gps_timestamp=maior,
                    telemetria=classificar_telemetria(maior, momento),
                )
                for item in recebidos:
                    if str(item.get("timestamp") or "") > cursor:
                        pontos_novos.append(item)
        caixas = limites if limites is not None else _limites(dia, pedido)
        from .cobertura import _xy

        for ponto in pontos_novos:
            ponto["xy"] = _xy(ponto)
        codigos = codigos_com_ponto_novo(pontos_novos, caixas)
        atualizadas: list[dict[str, Any]] = []
        if codigos:
            funcao = recalcular or _recalcular_oficial
            atualizadas = funcao(con, output_dir, dia, pedido, codigos)
            if atualizadas:
                gravar_rotas(con, dia.isoformat(), pedido, atualizadas)
        campos = {
            "falha": falha,
            "telemetria": _telemetria_geral(con, dia.isoformat(), pedido, falha),
        }
        if not falha:
            campos["ultima_sincronizacao"] = momento.isoformat(timespec="seconds")
            campos["atualizado_em"] = campos["ultima_sincronizacao"]
        if pesada and inicio_pesada and not falha and not _sem_cursor(ler_placas(con, dia.isoformat(), pedido)):
            campos["reconstrucao_tentativas"] = 0
            campos["reconstrucao_duracao_s"] = round(time.perf_counter() - inicio_pesada, 1)
            campos["reconstrucao_memoria_mb"] = round(_memoria_mb(), 1)
            campos["reconstrucao_pontos"] = int(
                con.execute(
                    "SELECT COUNT(*) FROM ponto WHERE data = ? AND turno = ?",
                    (dia.isoformat(), pedido),
                ).fetchone()[0]
            )
            print(
                "Reconstrução concluída. "
                f"pontos={campos['reconstrucao_pontos']} rotas={len(atualizadas)} "
                f"tempo={campos['reconstrucao_duracao_s']}s "
                f"memoria={campos['reconstrucao_memoria_mb']}MB. "
                "Próximos ciclos usam só o GPS novo.",
                flush=True,
            )
        elif pesada and falha:
            print(f"Reconstrução falhou e ficou em espera crescente: {falha}", flush=True)
        atualizar_turno(con, dia.isoformat(), pedido, **campos)
        con.commit()
        global _CICLOS
        _CICLOS += 1
        return {"novos": novos_total, "rotas": [item.get("codigo") for item in atualizadas], "falha": falha}
    finally:
        con.close()


def ciclo_posicao(
    output_dir: Path,
    data: str | None,
    turno: str | None,
    *,
    agora: datetime | None = None,
    buscar_eventos: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    from .config import parse_data

    pedido = (turno or "").strip().lower()
    dia = parse_data(data) or date.today()
    momento = agora or agora_brt()
    con = conectar()
    try:
        if not turno_existe(con, dia.isoformat(), pedido):
            return {"ok": False}
        cab = con.execute(
            "SELECT aberto FROM turno WHERE data = ? AND turno = ?",
            (dia.isoformat(), pedido),
        ).fetchone()
        if not cab or not cab["aberto"] or not janela_ainda_aberta(dia, pedido, momento):
            if cab and cab["aberto"] and not janela_ainda_aberta(dia, pedido, momento):
                fechar_turno(con, dia.isoformat(), pedido)
                con.commit()
            return {"ok": True, "fechado": True}
        try:
            eventos = (buscar_eventos or _eventos_fulltrack)()
            falha = ""
        except Exception as exc:
            falha = str(exc)
            atualizar_turno(con, dia.isoformat(), pedido, falha=falha)
            con.commit()
            return {"ok": False, "falha": falha}
        por_placa = {str(row["placa"]): row for row in ler_placas(con, dia.isoformat(), pedido)}
        for evento in eventos:
            placa = _placa_do_evento(evento)
            if not placa or placa not in por_placa:
                continue
            lat = _float(evento.get("ras_eve_latitude"))
            lon = _float(evento.get("ras_eve_longitude"))
            if lat is None or lon is None:
                continue
            marcar_placa(
                con,
                dia.isoformat(),
                pedido,
                placa,
                lat=lat,
                lon=lon,
                telemetria="atualizado",
            )
        marca = momento.isoformat(timespec="seconds")
        atualizar_turno(
            con,
            dia.isoformat(),
            pedido,
            ultima_fulltrack=marca,
            atualizado_em=marca,
            falha="",
            telemetria=_telemetria_geral(con, dia.isoformat(), pedido, ""),
        )
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def iniciar_loop() -> None:
    global _THREAD
    if (os.getenv("SAGTRACK_LIVE_SYNC") or "1").strip().lower() in {"0", "false", "nao", "não"}:
        return
    if _THREAD and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_loop, name="live-sync", daemon=True)
    _THREAD.start()
    print("Sincronizador Fulltrack: 1 thread ativa.", flush=True)


def _loop() -> None:
    ultima_posicao = 0.0
    ultima_gps = 0.0
    while True:
        time.sleep(5)
        if ocupada():
            continue
        agora = time.time()
        dia, pedido = turno_operacional()
        try:
            from .config import load_settings

            settings = load_settings()
        except Exception:
            continue
        if agora - ultima_posicao >= POSICAO_S and _entrar():
            try:
                ciclo_posicao(settings.output_dir, dia.isoformat(), pedido)
            except Exception as exc:
                _avisar_disco(exc)
            finally:
                _sair()
            ultima_posicao = time.time()
        if agora - ultima_gps >= GPS_S and _entrar():
            try:
                ciclo_gps(settings.output_dir, dia.isoformat(), pedido)
            except Exception as exc:
                _avisar_disco(exc)
                try:
                    _registrar_falha(settings.output_dir, dia.isoformat(), pedido, str(exc))
                except Exception as falha_registro:
                    _avisar_disco(falha_registro)
            finally:
                _sair()
            ultima_gps = time.time()


def _entrar() -> bool:
    global _OCUPADO
    with _SYNC:
        if _OCUPADO:
            return False
        _OCUPADO = True
        return True


def _sair() -> None:
    global _OCUPADO
    with _SYNC:
        _OCUPADO = False


_DISCO_AVISADO = False


def _avisar_disco(exc: BaseException) -> None:
    global _DISCO_AVISADO
    texto = str(exc)
    if "não está montado" not in texto and "disco temporário" not in texto:
        return
    if _DISCO_AVISADO:
        return
    _DISCO_AVISADO = True
    print(f"ERRO persistência: {texto}", flush=True)


def _registrar_falha(output_dir: Path, data: str | None, turno: str | None, texto: str) -> None:
    from .config import parse_data

    pedido = (turno or "").strip().lower()
    dia = parse_data(data)
    if pedido not in {"diurno", "noturno"} or dia is None:
        return
    con = conectar()
    try:
        if turno_existe(con, dia.isoformat(), pedido):
            atualizar_turno(con, dia.isoformat(), pedido, falha=texto)
            con.commit()
    finally:
        con.close()


def _importar_json_se_vazio(con, output_dir: Path, dia: date, turno: str) -> None:
    from .live_report import montar_estado

    if turno_existe(con, dia.isoformat(), turno):
        _importar_pontos_json(con, output_dir, dia, turno)
        return
    estado = montar_estado(output_dir, dia.isoformat(), turno)
    if not estado.get("rotas"):
        return
    gravar_turno(
        con,
        {
            "data": dia.isoformat(),
            "turno": turno,
            "aberto": 1 if janela_ainda_aberta(dia, turno) else 0,
            "periodo": estado.get("periodo") or "",
            "hora_inicio": estado.get("hora_inicio") or "",
            "hora_fim": estado.get("hora_fim") or "",
            "data_fim": estado.get("data_fim") or "",
            "ultima_sincronizacao": estado.get("atualizado_em") or "",
            "falha": "",
            "telemetria": "",
            "atualizado_em": estado.get("atualizado_em") or "",
        },
    )
    gravar_rotas(con, dia.isoformat(), turno, estado["rotas"])
    _importar_pontos_json(con, output_dir, dia, turno)
    con.commit()


def _importar_pontos_json(con, output_dir: Path, dia: date, turno: str) -> None:
    ja = con.execute(
        "SELECT 1 FROM ponto WHERE data = ? AND turno = ? LIMIT 1",
        (dia.isoformat(), turno),
    ).fetchone()
    if ja:
        return
    for item in _ler_rotas_gps(output_dir, dia):
        placa = str(item.get("placa") or "").strip()
        pontos = item.get("pontos") or item.get("pontos_dia") or []
        if not placa or not pontos:
            continue
        inserir_pontos(con, dia.isoformat(), turno, placa, pontos)
        maior = max((str(p.get("timestamp") or "") for p in pontos if p.get("timestamp")), default="")
        marcar_placa(
            con,
            dia.isoformat(),
            turno,
            placa,
            veiculo=str(item.get("veiculo") or ""),
            vehicle_id=str(item.get("codigo_veiculo") or ""),
            last_gps_timestamp=maior,
            telemetria=classificar_telemetria(maior),
        )


def _ler_rotas_gps(output_dir: Path, dia: date) -> list[dict[str, Any]]:
    pasta = Path(output_dir) / "segtrack" / dia.isoformat()
    for nome in ("rotas.json", "frota.json"):
        arq = pasta / nome
        if not arq.is_file():
            continue
        try:
            dados = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(dados, dict):
            dados = dados.get("rotas") or dados.get("frota") or []
        if isinstance(dados, list):
            return [item for item in dados if isinstance(item, dict)]
    return []


def _limites(dia: date, turno: str) -> list[dict[str, Any]]:
    from .mapa_oficial import carregar_setores
    from .unificado import _casa_turno, _freq_do_dia

    saida = []
    for setor in carregar_setores(None):
        freq = str(setor.get("frequencia") or "")
        if not _freq_do_dia(freq, dia) or not _casa_turno(freq, turno):
            continue
        geom = setor.get("geom")
        if geom is None:
            continue
        minx, miny, maxx, maxy = geom.bounds
        saida.append(
            {
                "codigo": str(setor.get("codigo") or ""),
                "minx": minx,
                "miny": miny,
                "maxx": maxx,
                "maxy": maxy,
            }
        )
    return saida


def _recalcular_oficial(con, output_dir: Path, dia: date, turno: str, codigos: list[str]) -> list[dict[str, Any]]:
    from .live_report import _rota
    from .unificado import _linha, analisar_codigos

    placas = ler_placas(con, dia.isoformat(), turno)
    caminhoes = []
    for placa in placas:
        pontos = ler_pontos_placa(con, dia.isoformat(), turno, placa["placa"])
        if len(pontos) < 8:
            continue
        caminhoes.append(
            {
                "id": placa["placa"],
                "fonte": "segtrack",
                "fonte_nome": "Segtrack",
                "veiculo": placa["veiculo"] or placa["placa"],
                "placa": placa["placa"],
                "inlog": "",
                "pontos": pontos,
            }
        )
    if not caminhoes:
        return []
    setores = analisar_codigos(dia, caminhoes, set(codigos), turno)
    return [_rota(_linha(setor)) for setor in setores]


def _sem_cursor(placas: list[dict[str, Any]]) -> bool:
    if not placas:
        return True
    return any(not str(item["last_gps_timestamp"] or "").strip() for item in placas)


def _atraso_reconstrucao(con, data: str, turno: str, momento: datetime) -> int | None:
    row = con.execute(
        "SELECT reconstrucao_em, reconstrucao_tentativas FROM turno WHERE data = ? AND turno = ?",
        (data, turno),
    ).fetchone()
    if row is None:
        return None
    espera = espera_reconstrucao(int(row["reconstrucao_tentativas"] or 0))
    ultima = _parse_ts(str(row["reconstrucao_em"] or ""))
    if not espera or ultima is None:
        return None
    passou = (momento - ultima).total_seconds()
    if passou < espera:
        return max(1, int(espera - passou))
    return None


def _marcar_tentativa(con, data: str, turno: str, momento: datetime) -> None:
    row = con.execute(
        "SELECT reconstrucao_tentativas FROM turno WHERE data = ? AND turno = ?",
        (data, turno),
    ).fetchone()
    tentativas = int(row["reconstrucao_tentativas"] or 0) if row else 0
    atualizar_turno(
        con,
        data,
        turno,
        reconstrucao_em=momento.isoformat(timespec="seconds"),
        reconstrucao_tentativas=tentativas + 1,
    )


def _memoria_mb() -> float:
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        status = ""
    for linha in status.splitlines():
        if linha.startswith("VmHWM:"):
            return int(linha.split()[1]) / 1024
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if os.name == "nt":
            return rss / (1024 * 1024)
        return rss / 1024
    except Exception:
        return 0.0


def _descobrir_frota() -> list[dict[str, Any]]:
    from .config import load_settings
    from .fulltrack_api import _escolher_veiculos, _eventos_atuais, _fmt_placa, listar_veiculos, login

    settings = load_settings()
    token = login(settings)
    veiculos = listar_veiculos(settings, token)
    try:
        eventos = _eventos_atuais(settings, token)
    except Exception:
        eventos = []
    escolhidos = _escolher_veiculos(veiculos, eventos, settings.segtrack_veiculos, None)
    saida: list[dict[str, Any]] = []
    for item in escolhidos:
        placa = _fmt_placa(str(item.get("ras_vei_placa") or ""))
        if not placa:
            continue
        saida.append(
            {
                "placa": placa,
                "veiculo": str(item.get("ras_vei_veiculo") or placa),
                "vehicle_id": str(item.get("ras_vei_id") or ""),
            }
        )
    return saida


def _historico_fulltrack(placa: str, vehicle_id: str, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
    from .config import load_settings
    from .fulltrack_api import historico_veiculo, login

    if not vehicle_id:
        return []
    settings = load_settings()
    token = login(settings)
    return historico_veiculo(settings, vehicle_id, inicio, fim, token=token)


def _eventos_fulltrack() -> list[dict[str, Any]]:
    from .config import load_settings
    from .fulltrack_api import _eventos_atuais, login

    settings = load_settings()
    return _eventos_atuais(settings, login(settings))


def _ponto_local(evento: dict[str, Any], placa: str) -> dict[str, Any] | None:
    if evento.get("latitude") is not None and evento.get("timestamp"):
        return {
            "timestamp": str(evento.get("timestamp")),
            "latitude": float(evento["latitude"]),
            "longitude": float(evento["longitude"]),
        }
    from .fulltrack_api import _ponto

    return _ponto(evento, placa)


def _placa_do_evento(evento: dict[str, Any]) -> str:
    from .fulltrack_api import _fmt_placa

    bruto = str(evento.get("placa") or evento.get("ras_vei_placa") or "")
    return _fmt_placa(bruto) if bruto else ""


def _telemetria_geral(con, data: str, turno: str, falha: str) -> str:
    if falha:
        return "falha"
    marcas = [str(row["telemetria"] or "") for row in ler_placas(con, data, turno)]
    if "atualizado" in marcas:
        return "atualizado"
    if "atrasado" in marcas:
        return "atrasado"
    return "sem_comunicacao"


def _parse_ts(texto: str) -> datetime | None:
    bruto = (texto or "").strip()
    if not bruto:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(bruto[:19], fmt)
        except ValueError:
            continue
    return None


def _float(valor: Any) -> float | None:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None
