"""Frota ao vivo, paradas e telemetria. Somente leitura da Fulltrack.

O km é a soma haversine. /events/all traz ras_eve_ignicao (0/1) e
ras_eve_hodometro; a ignição entra na parada, o hodômetro não entra no km.
O banco fica no mesmo disco de live_db.caminho_banco, em tabelas próprias.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import load_settings
from .fulltrack_api import (
    FulltrackSemDados,
    _eventos_atuais,
    _fmt_placa,
    _norm_placa,
    historico_veiculo,
    listar_veiculos,
    login,
)
from .gps import haversine_m, utc_para_brt_texto
from .live_turno import classificar_instante, fim_operacional, inicio_operacional

BRT = timezone(timedelta(hours=-3))
VEL_PARADO = 3.0
RAIO_PARADA_M = 60.0
LACUNA_S = 10 * 60
SALTO_M = 2000.0
SALTO_S = 30.0
BLOCO_H = 24
CACHE_S = 30.0
_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_AUSENTE = object()

_IO = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"expira": 0.0, "corpo": None}
_GEO = threading.Lock()
_GEO_ULTIMO = 0.0
_GEO_AVISO = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frota_ponto (
    placa TEXT NOT NULL,
    ts TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    vel REAL,
    ignicao INTEGER,
    PRIMARY KEY (placa, ts)
);
CREATE TABLE IF NOT EXISTS frota_geocache (
    lat TEXT NOT NULL,
    lon TEXT NOT NULL,
    endereco TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (lat, lon)
);
CREATE TABLE IF NOT EXISTS frota_janela (
    placa TEXT NOT NULL,
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL,
    PRIMARY KEY (placa, inicio, fim)
);
"""


def janela_do_turno(dia: date, turno: str) -> tuple[datetime, datetime]:
    """Início e fim do turno, inclusive o horário especial de sábado."""
    return inicio_operacional(dia, turno), fim_operacional(dia, turno)


def resolver_periodo(
    data: str | None = None,
    turno: str | None = None,
    inicio: str | None = None,
    fim: str | None = None,
) -> tuple[datetime, datetime, date, str | None]:
    """data+turno usam a janela operacional. inicio/fim explícitos têm prioridade."""
    data_txt = (data or "").strip()
    pedido = (turno or "").strip().lower()
    inicio_txt = (inicio or "").strip()
    fim_txt = (fim or "").strip()
    if inicio_txt or fim_txt:
        if not inicio_txt or not fim_txt:
            raise ValueError("Informe inicio e fim juntos, no formato ISO.")
        ini = _parse_parametro(inicio_txt)
        end = _parse_parametro(fim_txt)
        if end <= ini:
            raise ValueError("Janela inválida: o fim precisa ser depois do início.")
        classe = classificar_instante(ini)
        if data_txt:
            dia = _data(data_txt)
        elif classe:
            dia = classe[0]
        else:
            dia = ini.date()
        if pedido in {"diurno", "noturno"}:
            turno_poly: str | None = pedido
        elif classe:
            turno_poly = classe[1]
        else:
            turno_poly = None
        return ini, end, dia, turno_poly
    if not data_txt or not pedido:
        raise ValueError("Informe data e turno, ou inicio e fim.")
    dia = _data(data_txt)
    ini, end = janela_do_turno(dia, pedido)
    if end <= ini:
        raise ValueError("Janela inválida: o fim precisa ser depois do início.")
    return ini, end, dia, pedido


def ao_vivo() -> dict[str, Any]:
    """Posição atual da frota. O resultado fica 30 s em memória."""
    agora = time.monotonic()
    with _CACHE_LOCK:
        corpo = _CACHE["corpo"]
        if corpo is not None and agora < float(_CACHE["expira"]):
            return corpo
        fresco = _buscar_ao_vivo()
        _CACHE["corpo"] = fresco
        _CACHE["expira"] = time.monotonic() + CACHE_S
        return fresco


def historico(
    placa: str,
    inicio: datetime,
    fim: datetime,
    *,
    settings: Any = None,
    token: str | None = None,
    veiculos: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Pontos já guardados não voltam para a API. Janela acima de 24 h vira blocos."""
    if fim <= inicio:
        raise ValueError("Janela inválida: o fim precisa ser depois do início.")
    placa_fmt = _fmt_placa(placa)
    faltam = _com_banco(lambda con: _faltantes(con, placa_fmt, inicio, fim))
    if faltam:
        settings = settings or load_settings()
        token = token or login(settings)
        mapa = veiculos if veiculos is not None else _mapa_veiculos(settings, token)
        _placa, vid = _exigir_veiculo(placa_fmt, mapa)
        for bloco in _achatar(faltam):
            eventos = _baixar(settings, vid, placa_fmt, bloco, token)
            pontos = [item for ev in eventos if (item := _ponto_evento(ev, placa_fmt))]
            _com_banco(lambda con, bloco=bloco, pontos=pontos: _salvar(con, placa_fmt, bloco, pontos))
    return _com_banco(lambda con: _ler(con, placa_fmt, inicio, fim))


def detectar_paradas(
    pontos: list[dict[str, Any]] | None,
    min_min: float = 5,
    max_min: float = 60,
    *,
    dia: date | None = None,
    turno: str | None = None,
) -> list[dict[str, Any]]:
    """Paradas entre min e max minutos, tolerando 60 m de deriva do GPS."""
    if min_min < 0 or max_min < min_min:
        raise ValueError("Intervalo de parada inválido.")
    cache: dict[str, Any] = {}
    saida: list[dict[str, Any]] = []
    for placa, lista in _por_placa(pontos).items():
        _anotar(lista)
        for grupo in _agrupar(lista):
            dur_s = (grupo[-1]["ts"] - grupo[0]["ts"]).total_seconds()
            if dur_s < min_min * 60 or dur_s > max_min * 60:
                continue
            lat = sum(item["lat"] for item in grupo) / len(grupo)
            lon = sum(item["lon"] for item in grupo) / len(grupo)
            dentro, rota = _classificar_ponto(lat, lon, dia, turno, cache)
            saida.append(
                {
                    "placa": placa,
                    "inicio": _iso(grupo[0]["ts"]),
                    "fim": _iso(grupo[-1]["ts"]),
                    "duracao_min": round(dur_s / 60.0, 1),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "qtd_pulsos": len(grupo),
                    "com_lacuna": _tem_lacuna(grupo),
                    "ignicao_desligada": _ignicao_desligada(grupo),
                    "dentro_da_rota": dentro,
                    "rota": rota,
                }
            )
    saida.sort(key=lambda item: (item["placa"], item["inicio"]))
    return saida


def km_rodados(pontos: list[dict[str, Any]] | None) -> float:
    """Soma haversine. Salto acima de 2 km em menos de 30 s é ruído e sai da conta."""
    total = 0.0
    for lista in _por_placa(pontos).values():
        total += _km_lista(lista)
    return total


def telemetria(placa: str, inicio: datetime, fim: datetime) -> dict[str, Any]:
    pontos = historico(placa, inicio, fim)
    resumo = resumir_pontos(pontos)
    resumo["ok"] = True
    resumo["placa"] = _fmt_placa(placa)
    resumo["inicio"] = _iso(inicio)
    resumo["fim"] = _iso(fim)
    return resumo


def resumir_pontos(pontos: list[dict[str, Any]] | None) -> dict[str, Any]:
    grupos = list(_por_placa(pontos).values())
    lista = grupos[0] if grupos else []
    if lista:
        _anotar(lista)
    velocidades = [item.get("vel_efetiva") for item in lista]
    conhecidas = [float(item) for item in velocidades if item is not None]
    em_movimento = [
        float(item["vel_efetiva"])
        for item in lista
        if item.get("vel_efetiva") is not None and not _eh_parado(item)
    ]
    mov_s, par_s = _tempos(lista)
    return {
        "km": round(_km_lista(lista), 3),
        "vel_media": round(sum(em_movimento) / len(em_movimento), 1) if em_movimento else None,
        "vel_maxima": round(max(conhecidas), 1) if conhecidas else None,
        "tempo_movimento_min": round(mov_s / 60.0, 1),
        "tempo_parado_min": round(par_s / 60.0, 1),
        "paradas": len(detectar_paradas(pontos, 5, 60)),
        "primeiro_pulso": _iso(lista[0]["ts"]) if lista else None,
        "ultimo_pulso": _iso(lista[-1]["ts"]) if lista else None,
        "pulsos_acima_60": sum(1 for item in conhecidas if item > 60),
        "serie": [
            [_iso(item["ts"]), None if item.get("vel_efetiva") is None else round(float(item["vel_efetiva"]), 1)]
            for item in lista
        ],
    }


def endereco(lat: Any, lon: Any) -> str:
    """Geocodificação reversa. Falha de rede devolve vazio e não repete o mesmo ponto."""
    try:
        la = f"{round(float(lat), 4):.4f}"
        lo = f"{round(float(lon), 4):.4f}"
        if abs(float(la)) < 0.5 or abs(float(lo)) < 0.5:
            return ""
    except (TypeError, ValueError):
        return ""
    guardado = _com_banco(lambda con: _geo_ler(con, la, lo))
    if guardado is not _AUSENTE:
        return str(guardado)
    try:
        return _nominatim(la, lo)
    except Exception:
        _avisar_geo()
        return ""


def consultar_paradas(qs: dict[str, list[str]]) -> dict[str, Any]:
    inicio, fim, dia, turno, minimo, maximo, placa = _filtros(qs)
    avisos: list[str] = []
    if placa:
        blocos = [historico(placa, inicio, fim)]
        placa_fmt = _fmt_placa(placa)
    else:
        blocos = _historico_frota(inicio, fim, avisos)
        placa_fmt = ""
    paradas: list[dict[str, Any]] = []
    for pontos in blocos:
        paradas.extend(detectar_paradas(pontos, minimo, maximo, dia=dia, turno=turno))
    for item in paradas:
        item["endereco"] = endereco(item.get("lat"), item.get("lon"))
    return {
        "ok": True,
        "placa": placa_fmt,
        "inicio": _iso(inicio),
        "fim": _iso(fim),
        "min": minimo,
        "max": maximo,
        "paradas": paradas,
        "avisos": avisos,
    }


def paradas_csv(qs: dict[str, list[str]]) -> tuple[bytes, str]:
    corpo = consultar_paradas(qs)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "placa",
            "inicio",
            "fim",
            "duracao_min",
            "lat",
            "lon",
            "qtd_pulsos",
            "com_lacuna",
            "ignicao_desligada",
            "dentro_da_rota",
            "rota",
            "endereco",
        ]
    )
    for item in corpo["paradas"]:
        writer.writerow(
            [
                item.get("placa") or "",
                item.get("inicio") or "",
                item.get("fim") or "",
                item.get("duracao_min") if item.get("duracao_min") is not None else "",
                item.get("lat") if item.get("lat") is not None else "",
                item.get("lon") if item.get("lon") is not None else "",
                item.get("qtd_pulsos") if item.get("qtd_pulsos") is not None else "",
                _celula(item.get("com_lacuna")),
                _celula(item.get("ignicao_desligada")),
                _celula(item.get("dentro_da_rota")),
                item.get("rota") or "",
                item.get("endereco") or "",
            ]
        )
    return buf.getvalue().encode("utf-8-sig"), "paradas.csv"


def consultar_telemetria(qs: dict[str, list[str]]) -> dict[str, Any]:
    inicio, fim, _dia, _turno, _minimo, _maximo, placa = _filtros(qs, exigir_placa=True)
    return telemetria(placa, inicio, fim)


def consultar_trajeto(qs: dict[str, list[str]]) -> dict[str, Any]:
    inicio, fim, _dia, _turno, _minimo, _maximo, placa = _filtros(qs, exigir_placa=True)
    pontos = historico(placa, inicio, fim)
    return {
        "ok": True,
        "placa": _fmt_placa(placa),
        "inicio": _iso(inicio),
        "fim": _iso(fim),
        "qtd": len(pontos),
        "pontos": pontos,
    }


def _filtros(
    qs: dict[str, list[str]],
    *,
    exigir_placa: bool = False,
) -> tuple[datetime, datetime, date, str | None, float, float, str]:
    inicio, fim, dia, turno = resolver_periodo(
        _param(qs, "data"),
        _param(qs, "turno"),
        _param(qs, "inicio"),
        _param(qs, "fim"),
    )
    minimo = _limite(_param(qs, "min"), 5, "min")
    maximo = _limite(_param(qs, "max"), 60, "max")
    if maximo < minimo:
        raise ValueError("O máximo de minutos precisa ser maior ou igual ao mínimo.")
    placa = _param(qs, "placa")
    if exigir_placa and not placa:
        raise ValueError("Informe a placa.")
    return inicio, fim, dia, turno, minimo, maximo, placa


def _param(qs: dict[str, list[str]] | None, nome: str) -> str:
    if not isinstance(qs, dict):
        return ""
    valor = qs.get(nome)
    if isinstance(valor, list):
        valor = valor[0] if valor else ""
    return str(valor or "").strip()


def _limite(texto: str, padrao: float, nome: str) -> float:
    if not texto:
        return float(padrao)
    try:
        valor = float(texto.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Parâmetro {nome} inválido.") from exc
    if valor < 0:
        raise ValueError(f"Parâmetro {nome} inválido.")
    return valor


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError as exc:
        raise ValueError("Data inválida.") from exc


def _parse_parametro(texto: str) -> datetime:
    bruto = (texto or "").strip()
    if not bruto:
        raise ValueError("Data/hora inválida. Use o formato ISO, por exemplo 2026-10-21T06:00:00.")
    normal = bruto.replace("Z", "+00:00")
    try:
        momento = datetime.fromisoformat(normal)
    except ValueError as exc:
        raise ValueError("Data/hora inválida. Use o formato ISO, por exemplo 2026-10-21T06:00:00.") from exc
    if momento.tzinfo is not None:
        momento = momento.astimezone(BRT).replace(tzinfo=None)
    return momento.replace(microsecond=0)


def _buscar_ao_vivo() -> dict[str, Any]:
    settings = load_settings()
    token = login(settings)
    veiculos = listar_veiculos(settings, token)
    eventos = _eventos_atuais(settings, token)
    por_id: dict[str, dict[str, Any]] = {}
    por_placa: dict[str, dict[str, Any]] = {}
    for ev in eventos:
        vid = str(ev.get("ras_vei_id") or "")
        if vid and (vid not in por_id or _chave_tempo(ev) >= _chave_tempo(por_id[vid])):
            por_id[vid] = ev
        chave = _norm_placa(str(ev.get("ras_vei_placa") or ""))
        if chave and (chave not in por_placa or _chave_tempo(ev) >= _chave_tempo(por_placa[chave])):
            por_placa[chave] = ev
    agora = _agora_brt()
    saida: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for item in veiculos:
        placa = _fmt_placa(str(item.get("ras_vei_placa") or ""))
        chave = _norm_placa(placa)
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        ev = por_id.get(str(item.get("ras_vei_id") or "")) or por_placa.get(chave)
        saida.append(_posicao(item, ev, agora, placa))
    saida.sort(key=lambda row: row["placa"])
    return {
        "ok": True,
        "atualizado_em": _iso(agora),
        "qtd": len(saida),
        "veiculos": saida,
    }


def _chave_tempo(evento: dict[str, Any]) -> str:
    return str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")


def _posicao(item: dict[str, Any], ev: dict[str, Any] | None, agora: datetime, placa: str) -> dict[str, Any]:
    lat = lon = vel = ign = None
    data_gps = ""
    minutos = None
    if ev:
        lat, lon = _coord(ev.get("ras_eve_latitude"), ev.get("ras_eve_longitude"))
        vel = _numero_ou_none(ev.get("ras_eve_velocidade"))
        ign = _ignicao(ev.get("ras_eve_ignicao"))
        data_gps = _iso_de_utc(ev.get("ras_eve_data_gps") or ev.get("ras_eve_data_enviado"))
        momento = _parse_ts(data_gps)
        if momento is not None:
            minutos = round(max(0.0, (agora - momento).total_seconds() / 60.0), 1)
    return {
        "placa": placa,
        "descricao": str(item.get("ras_vei_veiculo") or placa).strip() or placa,
        "lat": None if lat is None else round(lat, 6),
        "lon": None if lon is None else round(lon, 6),
        "velocidade": vel,
        "ignicao": ign,
        "data_gps": data_gps,
        "minutos": minutos,
        "status": _status(vel, minutos),
    }


def _status(vel: float | None, minutos: float | None) -> str:
    if minutos is None or minutos >= 15:
        return "sem_sinal"
    if minutos >= 5:
        return "atrasado"
    if vel is not None and vel > VEL_PARADO:
        return "movimento"
    return "parado"


def _historico_frota(inicio: datetime, fim: datetime, avisos: list[str]) -> list[list[dict[str, Any]]]:
    settings = load_settings()
    token = login(settings)
    mapa = _mapa_veiculos(settings, token)
    placas = sorted({_fmt_placa(str(item.get("ras_vei_placa") or "")) for item in mapa.values()})
    placas = [placa for placa in placas if _norm_placa(placa)]
    if not placas:
        return []
    blocos: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(placas))) as pool:
        futuros = {
            pool.submit(historico, placa, inicio, fim, settings=settings, token=token, veiculos=mapa): placa
            for placa in placas
        }
        for fut in as_completed(futuros):
            placa = futuros[fut]
            try:
                blocos[placa] = fut.result()
            except Exception as exc:
                avisos.append(f"{placa}: {exc}")
    return [blocos[placa] for placa in placas if placa in blocos]


def _mapa_veiculos(settings: Any, token: str) -> dict[str, dict[str, Any]]:
    mapa: dict[str, dict[str, Any]] = {}
    for item in listar_veiculos(settings, token):
        chave = _norm_placa(str(item.get("ras_vei_placa") or ""))
        if chave and chave not in mapa:
            mapa[chave] = item
    return mapa


def _exigir_veiculo(placa_fmt: str, mapa: dict[str, dict[str, Any]]) -> tuple[str, str]:
    item = mapa.get(_norm_placa(placa_fmt))
    if not item:
        raise ValueError(f"Placa {placa_fmt} não encontrada na frota.")
    vid = str(item.get("ras_vei_id") or "").strip()
    if not vid:
        raise ValueError(f"Placa {placa_fmt} sem identificador na Fulltrack.")
    return placa_fmt, vid


def _baixar(settings: Any, vid: str, placa: str, bloco: tuple[datetime, datetime], token: str) -> list[dict[str, Any]]:
    inicio, fim = bloco
    if fim <= inicio:
        return []
    print(f"Frota: histórico {placa} {_iso(inicio)} → {_iso(fim)}", flush=True)
    try:
        eventos = historico_veiculo(settings, vid, inicio, fim, token=token)
    except FulltrackSemDados:
        return []
    return eventos or []


def _ponto_evento(evento: dict[str, Any], placa: str) -> dict[str, Any] | None:
    lat, lon = _coord(evento.get("ras_eve_latitude"), evento.get("ras_eve_longitude"))
    if lat is None or lon is None:
        return None
    ts = _iso_de_utc(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado"))
    if not ts:
        return None
    return {
        "ts": ts,
        "lat": lat,
        "lon": lon,
        "vel": _numero_ou_none(evento.get("ras_eve_velocidade")),
        "ignicao": _ignicao(evento.get("ras_eve_ignicao")),
        "placa": placa,
    }


def _iso_de_utc(valor: Any) -> str:
    momento = _parse_ts(utc_para_brt_texto(str(valor or "")))
    return _iso(momento) if momento else ""


def _por_placa(pontos: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    grupos: dict[str, list[dict[str, Any]]] = {}
    for ponto in _preparar(pontos):
        grupos.setdefault(ponto["placa"], []).append(ponto)
    for lista in grupos.values():
        lista.sort(key=lambda item: item["ts"])
    return grupos


def _preparar(pontos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    for bruto in pontos or []:
        if not isinstance(bruto, dict):
            continue
        momento = _parse_ts(bruto.get("ts") if "ts" in bruto else bruto.get("timestamp"))
        if momento is None:
            continue
        lat, lon = _coord(
            bruto.get("lat") if "lat" in bruto else bruto.get("latitude"),
            bruto.get("lon") if "lon" in bruto else bruto.get("longitude"),
        )
        if lat is None or lon is None:
            continue
        saida.append(
            {
                "placa": _fmt_placa(str(bruto.get("placa") or "")),
                "ts": momento,
                "lat": lat,
                "lon": lon,
                "vel": _numero_ou_none(bruto.get("vel") if "vel" in bruto else bruto.get("velocidade")),
                "ignicao": _ignicao(bruto.get("ignicao")),
            }
        )
    return saida


def _anotar(lista: list[dict[str, Any]]) -> None:
    for i, ponto in enumerate(lista):
        if ponto.get("vel") is not None:
            ponto["vel_efetiva"] = ponto["vel"]
            continue
        if i > 0:
            ponto["vel_efetiva"] = _vel_haversine(lista[i - 1], ponto)
        elif i + 1 < len(lista):
            ponto["vel_efetiva"] = _vel_haversine(ponto, lista[i + 1])
        else:
            ponto["vel_efetiva"] = None


def _eh_parado(ponto: dict[str, Any]) -> bool:
    if ponto.get("ignicao") is False:
        return True
    vel = ponto.get("vel_efetiva")
    if vel is None:
        return False
    return float(vel) <= VEL_PARADO


def _vel_haversine(a: dict[str, Any], b: dict[str, Any]) -> float:
    dt = (b["ts"] - a["ts"]).total_seconds()
    if dt <= 0:
        return 0.0
    dist = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
    return dist / dt * 3.6


def _agrupar(lista: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grupos: list[list[dict[str, Any]]] = []
    atual: list[dict[str, Any]] = []
    for ponto in lista:
        if not _eh_parado(ponto):
            if atual:
                grupos.append(atual)
                atual = []
            continue
        if atual:
            centro = (
                sum(item["lat"] for item in atual) / len(atual),
                sum(item["lon"] for item in atual) / len(atual),
            )
            if haversine_m(centro, (ponto["lat"], ponto["lon"])) > RAIO_PARADA_M:
                grupos.append(atual)
                atual = [ponto]
                continue
        atual.append(ponto)
    if atual:
        grupos.append(atual)
    return grupos


def _tem_lacuna(grupo: list[dict[str, Any]]) -> bool:
    for a, b in zip(grupo, grupo[1:]):
        if (b["ts"] - a["ts"]).total_seconds() > LACUNA_S:
            return True
    return False


def _ignicao_desligada(grupo: list[dict[str, Any]]) -> bool | None:
    valores = [item["ignicao"] for item in grupo if item.get("ignicao") is not None]
    if not valores:
        return None
    return all(item is False for item in valores)


def _classificar_ponto(
    lat: float,
    lon: float,
    dia: date | None,
    turno: str | None,
    cache: dict[str, Any],
) -> tuple[bool | None, str | None]:
    if dia is None:
        return None, None
    if "setores" not in cache:
        cache["setores"] = _carregar_setores(dia, turno)
    setores = cache["setores"]
    if not setores:
        return None, None
    try:
        from .live_mapa import codigo_que_contem
    except Exception:
        return None, None
    try:
        codigo = codigo_que_contem(lat, lon, setores)
    except Exception:
        return None, None
    if codigo:
        return True, str(codigo)
    return False, None


def _carregar_setores(dia: date, turno: str | None) -> list[dict[str, Any]]:
    try:
        from .live_mapa import _setores_do_turno
    except Exception:
        return []
    try:
        if turno in {"diurno", "noturno"}:
            return list(_setores_do_turno(dia, turno) or [])
        return list(_setores_do_turno(dia, "diurno") or []) + list(_setores_do_turno(dia, "noturno") or [])
    except Exception:
        return []


def _km_lista(lista: list[dict[str, Any]]) -> float:
    metros = 0.0
    for a, b in zip(lista, lista[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt <= 0:
            continue
        dist = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if dist > SALTO_M and dt < SALTO_S:
            continue
        metros += dist
    return metros / 1000.0


def _tempos(lista: list[dict[str, Any]]) -> tuple[float, float]:
    mov = 0.0
    par = 0.0
    for a, b in zip(lista, lista[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt <= 0 or dt > LACUNA_S:
            continue
        if _eh_parado(b):
            par += dt
        else:
            mov += dt
    return mov, par


def _nominatim(lat: str, lon: str) -> str:
    global _GEO_ULTIMO
    with _GEO:
        guardado = _com_banco(lambda con: _geo_ler(con, lat, lon))
        if guardado is not _AUSENTE:
            return str(guardado)
        espera = 1.0 - (time.monotonic() - _GEO_ULTIMO)
        if _GEO_ULTIMO and espera > 0:
            time.sleep(espera)
        _GEO_ULTIMO = time.monotonic()
        query = urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": "18"})
        pedido = Request(
            _NOMINATIM + "?" + query,
            headers={
                "User-Agent": "SEGTRACK-frota",
                "Accept": "application/json",
                "Accept-Language": "pt-BR",
            },
        )
        try:
            with urlopen(pedido, timeout=12) as resp:
                corpo = json.loads(resp.read().decode("utf-8", "replace"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            raise
        texto = str(corpo.get("display_name") or "").strip() if isinstance(corpo, dict) else ""
        _com_banco(lambda con: _geo_gravar(con, lat, lon, texto))
        return texto


def _avisar_geo() -> None:
    global _GEO_AVISO
    if _GEO_AVISO:
        return
    _GEO_AVISO = True
    print("Frota: geocodificação indisponível; o endereço fica vazio.", flush=True)


def _faltantes(con: sqlite3.Connection, placa: str, inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
    rows = con.execute(
        "SELECT inicio, fim FROM frota_janela WHERE placa = ? AND fim > ? AND inicio < ?",
        (placa, _iso(inicio), _iso(fim)),
    ).fetchall()
    intervalos: list[tuple[datetime, datetime]] = []
    for row in rows:
        a = _parse_ts(row["inicio"])
        b = _parse_ts(row["fim"])
        if a is not None and b is not None and b > a:
            intervalos.append((a, b))
    return _buracos(inicio, fim, _unir(intervalos))


def _salvar(
    con: sqlite3.Connection,
    placa: str,
    bloco: tuple[datetime, datetime],
    pontos: list[dict[str, Any]],
) -> None:
    for ponto in pontos:
        con.execute(
            """
            INSERT OR REPLACE INTO frota_ponto (placa, ts, lat, lon, vel, ignicao)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (placa, ponto["ts"], ponto["lat"], ponto["lon"], ponto["vel"], _ign_int(ponto["ignicao"])),
        )
    ini, fim = bloco
    con.execute(
        "INSERT OR IGNORE INTO frota_janela (placa, inicio, fim) VALUES (?, ?, ?)",
        (placa, _iso(ini), _iso(fim)),
    )


def _ler(con: sqlite3.Connection, placa: str, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT ts, lat, lon, vel, ignicao FROM frota_ponto
        WHERE placa = ? AND ts >= ? AND ts <= ?
        ORDER BY ts
        """,
        (placa, _iso(inicio), _iso(fim)),
    ).fetchall()
    saida: list[dict[str, Any]] = []
    for row in rows:
        saida.append(
            {
                "ts": row["ts"],
                "lat": row["lat"],
                "lon": row["lon"],
                "vel": row["vel"],
                "ignicao": _ignicao(row["ignicao"]),
                "placa": placa,
            }
        )
    return saida


def _geo_ler(con: sqlite3.Connection, lat: str, lon: str) -> Any:
    row = con.execute(
        "SELECT endereco FROM frota_geocache WHERE lat = ? AND lon = ?",
        (lat, lon),
    ).fetchone()
    if row is None:
        return _AUSENTE
    return str(row["endereco"] or "")


def _geo_gravar(con: sqlite3.Connection, lat: str, lon: str, texto: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO frota_geocache (lat, lon, endereco) VALUES (?, ?, ?)",
        (lat, lon, texto),
    )


def _achatar(buracos: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    saida: list[tuple[datetime, datetime]] = []
    for inicio, fim in buracos:
        saida.extend(_em_blocos(inicio, fim))
    return saida


def _em_blocos(inicio: datetime, fim: datetime) -> list[tuple[datetime, datetime]]:
    limite = timedelta(hours=BLOCO_H)
    if fim - inicio <= limite:
        return [(inicio, fim)]
    saida: list[tuple[datetime, datetime]] = []
    cursor = inicio
    while cursor < fim:
        prox = min(cursor + limite, fim)
        if prox <= cursor:
            break
        saida.append((cursor, prox))
        cursor = prox
    return saida


def _unir(intervalos: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervalos:
        return []
    itens = sorted(intervalos)
    saida: list[list[datetime]] = [[itens[0][0], itens[0][1]]]
    for inicio, fim in itens[1:]:
        if inicio <= saida[-1][1]:
            if fim > saida[-1][1]:
                saida[-1][1] = fim
        else:
            saida.append([inicio, fim])
    return [(item[0], item[1]) for item in saida]


def _buracos(
    inicio: datetime,
    fim: datetime,
    cobertos: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    cursor = inicio
    faltam: list[tuple[datetime, datetime]] = []
    for a, b in cobertos:
        if b <= cursor:
            continue
        if a > cursor:
            faltam.append((cursor, min(a, fim)))
        cursor = max(cursor, b)
        if cursor >= fim:
            return faltam
    if cursor < fim:
        faltam.append((cursor, fim))
    return faltam


@contextmanager
def _conexao() -> Iterator[sqlite3.Connection]:
    from .live_db import caminho_banco

    con = sqlite3.connect(str(caminho_banco()), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript(_SCHEMA)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _com_banco(fn: Any) -> Any:
    with _IO:
        with _conexao() as con:
            return fn(con)


def _coord(lat: Any, lon: Any) -> tuple[float | None, float | None]:
    try:
        la = float(str(lat).replace(",", "."))
        lo = float(str(lon).replace(",", "."))
    except (TypeError, ValueError):
        return None, None
    if abs(la) < 0.5 or abs(lo) < 0.5 or abs(la) > 90 or abs(lo) > 180:
        return None, None
    return la, lo


def _numero_ou_none(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _ignicao(valor: Any) -> bool | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if valor == 1:
            return True
        if valor == 0:
            return False
        return None
    texto = str(valor).strip().lower()
    if texto in {"1", "true", "on", "ligada", "sim"}:
        return True
    if texto in {"0", "false", "off", "desligada", "nao", "não"}:
        return False
    return None


def _ign_int(valor: Any) -> int | None:
    ign = _ignicao(valor)
    if ign is None:
        return None
    return 1 if ign else 0


def _parse_ts(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        momento = valor.astimezone(BRT).replace(tzinfo=None) if valor.tzinfo else valor
        return momento.replace(microsecond=0)
    texto = str(valor or "").strip()
    if not texto:
        return None
    if texto.endswith("Z") or texto.endswith("z"):
        texto = texto[:-1]
    if texto.endswith("+00:00"):
        texto = texto[: -len("+00:00")].strip()
    texto = texto.replace("T", " ")
    for fmt, tamanho in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(texto[:tamanho], fmt)
        except ValueError:
            continue
    return None


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%dT%H:%M:%S")


def _agora_brt() -> datetime:
    return datetime.now(BRT).replace(tzinfo=None, microsecond=0)


def _celula(valor: Any) -> str:
    if valor is True:
        return "sim"
    if valor is False:
        return "nao"
    return ""
