"""Frota ao vivo, paradas, telemetria e operações por região.

O km é a soma haversine. /events/all traz ras_eve_ignicao (0/1) e
ras_eve_hodometro; a ignição entra na parada, o hodômetro não entra no km.
O banco fica no mesmo disco de live_db.caminho_banco, em tabelas próprias.
Sem ?operacao=, a consulta usa a operação padrão. operacao=todas devolve a conta inteira.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .conducao import (
    ALERTA_IGNICAO,
    analisar as analisar_conducao,
    normalizar_alerta,
)
from .config import ROOT, load_settings
from .fulltrack_api import (
    APARECIDA_LAT,
    APARECIDA_LON,
    TIMEOUT_OPCIONAL_S,
    FulltrackSemDados,
    _em_aparecida,
    _eventos_atuais,
    _fmt_placa,
    _norm_placa,
    alertas_periodo,
    consolidado_veiculo,
    historico_veiculo,
    listar_veiculos,
    login,
    teto_timeout,
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
CAP_BASE_S = 2 * 60 * 60
RAIO_VIZINHA_M = 40_000.0
_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_BUSCA = "https://nominatim.openstreetmap.org/search"
_AUSENTE = object()
# Cidades da empresa. A placa entra pela cidade da base, não pela caixa do mapa.
OPERACOES_ALVO = (
    ("Belo Horizonte", "MG"),
    ("Aparecida de Goiânia", "GO"),
    ("Londrina", "PR"),
    ("Unaí", "MG"),
    ("Contagem", "MG"),
    ("Ibiporã", "PR"),
    ("Pindamonhangaba", "SP"),
    ("Porto Velho", "RO"),
    ("Uberaba", "MG"),
)
OPERACOES_EXTRAS = (
    ("Ariquemes", "RO"),
    ("Caldas Novas", "GO"),
)
_PLACAS_ARIQUEMES = (
    "CAT226B3",
    "RVE-2H79",
    "RVU-8C66",
    "TTV-9G70",
    "TUF-3C31",
    "UAX-0F65",
    "YTO NX854 0",
)
_PLACAS_CALDAS = ("SCO-6H09", "TBY-3G12")
_PLACA_CONTAGEM = "TRZ-5F28"
_ESTADOS = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}

_IO = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"chave": "", "expira": 0.0, "corpo": None}
_TEL_LOCK = threading.Lock()
_TEL: dict[str, tuple[float, dict[str, Any]]] = {}
_TEL_ABERTO_S = 30.0
_TEL_FECHADO_S = 60.0
_TEL_MAX = 32
_GEO = threading.Lock()
_GEO_ULTIMO = 0.0
_GEO_AVISO = False
_FILA_MAX = 400
_FILA_LOCK = threading.Lock()
_FILA: deque[tuple[str, str]] = deque()
_FILA_SET: set[tuple[str, str]] = set()
_FILA_AVISO = threading.Event()
_FILA_THREAD: threading.Thread | None = None
LIMITE_ESPERA_GEO_S = 12.0
_STATS_LOCK = threading.Lock()
_PULSO_STATS = {"sem_coord": 0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frota_ponto (
    placa TEXT NOT NULL,
    ts TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    vel REAL,
    ignicao INTEGER,
    direcao REAL,
    PRIMARY KEY (placa, ts)
);
CREATE TABLE IF NOT EXISTS frota_geocache (
    lat TEXT NOT NULL,
    lon TEXT NOT NULL,
    endereco TEXT NOT NULL DEFAULT '',
    cidade TEXT,
    uf TEXT,
    PRIMARY KEY (lat, lon)
);
CREATE TABLE IF NOT EXISTS frota_janela (
    placa TEXT NOT NULL,
    inicio TEXT NOT NULL,
    fim TEXT NOT NULL,
    PRIMARY KEY (placa, inicio, fim)
);
CREATE TABLE IF NOT EXISTS frota_operacao (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    lat_min REAL NOT NULL,
    lat_max REAL NOT NULL,
    lon_min REAL NOT NULL,
    lon_max REAL NOT NULL,
    ativa INTEGER NOT NULL DEFAULT 1,
    padrao INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frota_operacao_placa (
    operacao_id INTEGER NOT NULL,
    placa TEXT NOT NULL,
    PRIMARY KEY (operacao_id, placa)
);
CREATE TABLE IF NOT EXISTS frota_import (
    versao TEXT PRIMARY KEY,
    aplicado_em TEXT NOT NULL
);
"""

ARQUIVO_OPERACOES_APROVADO = ROOT / "data" / "frota_operacoes_aprovado.json"


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


def ao_vivo(qs: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Posição atual. O cache de 30 s guarda a conta inteira; o filtro vem depois."""
    op = _operacao_efetiva(qs)
    base = _ao_vivo_completo()
    if op is None:
        veiculos = [_publico_vivo(item) for item in base["veiculos"]]
        return {
            "ok": True,
            "atualizado_em": base["atualizado_em"],
            "qtd": len(veiculos),
            "veiculos": veiculos,
        }
    placas = _com_banco(lambda con: _fixadas_fmt(con, int(op["id"])))
    veiculos = _veiculos_da_operacao(base["veiculos"], op, placas)
    return {
        "ok": True,
        "atualizado_em": base["atualizado_em"],
        "qtd": len(veiculos),
        "veiculos": veiculos,
    }


def _ao_vivo_completo() -> dict[str, Any]:
    from .live_db import caminho_banco

    chave = str(caminho_banco())
    agora = time.monotonic()
    with _CACHE_LOCK:
        corpo = _CACHE["corpo"]
        if corpo is not None and _CACHE.get("chave") == chave and agora < float(_CACHE["expira"]):
            return corpo
        fresco = _buscar_ao_vivo()
        _CACHE["chave"] = chave
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
    incluir_direcao: bool = False,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Pontos já guardados não voltam para a API. Janela acima de 24 h vira blocos.

    incluir_direcao rebusca uma vez a faixa antiga, gravada sem rumo.
    """
    if fim <= inicio:
        raise ValueError("Janela inválida: o fim precisa ser depois do início.")
    placa_fmt = _fmt_placa(placa)
    _preencher(placa_fmt, inicio, fim, settings, token, veiculos, timeout=timeout)
    pontos = _com_banco(lambda con: _ler(con, placa_fmt, inicio, fim))
    if incluir_direcao and pontos and any(item.get("direcao") is None for item in pontos):
        _com_banco(lambda con: _esquecer(con, placa_fmt, inicio, fim))
        _preencher(placa_fmt, inicio, fim, settings, token, veiculos, timeout=timeout)
        pontos = _com_banco(lambda con: _ler(con, placa_fmt, inicio, fim))
    return pontos


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


def telemetria(
    placa: str,
    inicio: datetime,
    fim: datetime,
    *,
    dia: date | None = None,
    geocodificar: bool = True,
) -> dict[str, Any]:
    settings = load_settings()
    token = login(settings)
    mapa = _mapa_veiculos(settings, token)
    placa_fmt = _fmt_placa(placa)
    item = mapa.get(_norm_placa(placa_fmt)) or {}
    vehicle_id = str(item.get("ras_vei_id") or "")
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_pontos = pool.submit(
            historico,
            placa,
            inicio,
            fim,
            settings=settings,
            token=token,
            veiculos=mapa,
            incluir_direcao=True,
            timeout=TIMEOUT_OPCIONAL_S,
        )
        fut_alertas = pool.submit(_alertas_do_turno, settings, token, inicio, fim, [])
        fut_ocioso = pool.submit(_ocioso_api, settings, token, vehicle_id, dia)
        pontos = fut_pontos.result()
        alertas, falha_alertas = fut_alertas.result()
        ocioso, falha_consolidado = fut_ocioso.result()
    resumo = resumir_pontos(pontos)
    conducao = _conducao_de(
        placa_fmt,
        pontos,
        item.get("ras_vei_velocidade_limite"),
        alertas,
        ocioso,
        falha_alertas or falha_consolidado,
    )
    _aplicar_enderecos(conducao["eventos"], enfileirar=geocodificar)
    resumo.update(conducao)
    resumo["ok"] = True
    resumo["placa"] = placa_fmt
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
    """Só o que já está em frota_geocache. O que falta entra na fila e devolve vazio."""
    texto = _endereco_cache(lat, lon)
    if texto is None:
        _enfileirar_pontos([(lat, lon)])
        return ""
    return texto


def consultar_paradas(qs: dict[str, list[str]]) -> dict[str, Any]:
    op = _operacao_efetiva(qs)
    inicio, fim, dia, turno, minimo, maximo, placa = _filtros(qs)
    avisos: list[str] = []
    if placa:
        if op is not None:
            _recusar_fora(placa, op)
        blocos = [historico(placa, inicio, fim)]
        placa_fmt = _fmt_placa(placa)
    else:
        placas = None if op is None else _placas_operacao(op)
        blocos = _historico_frota(inicio, fim, avisos, placas=placas)
        placa_fmt = ""
    paradas: list[dict[str, Any]] = []
    for pontos in blocos:
        paradas.extend(detectar_paradas(pontos, minimo, maximo, dia=dia, turno=turno))
    _aplicar_enderecos(paradas, enfileirar=True)
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
    with teto_timeout(TIMEOUT_OPCIONAL_S):
        op = _operacao_efetiva(qs)
        inicio, fim, dia, turno, _minimo, _maximo, placa = _filtros(qs, exigir_placa=True)
        if op is not None:
            _recusar_fora(placa, op)
        geocodificar = _param(qs, "sem_geo_eventos").lower() not in {"1", "true", "sim", "yes", "on"}
        chave = _tel_chave(placa, dia, turno)
        guardado = _tel_ler(chave)
        if guardado is not None:
            if geocodificar:
                _enfileirar_pontos((item.get("lat"), item.get("lon")) for item in guardado.get("eventos") or [])
            return guardado
        corpo = telemetria(placa, inicio, fim, dia=dia, geocodificar=geocodificar)
        _tel_gravar(chave, corpo, _tel_ttl(fim))
        return corpo


def consultar_enderecos(qs: dict[str, list[str]]) -> dict[str, Any]:
    """Devolve o que já está no cache e enfileira o resto. No máximo 200 pontos."""
    bruto = _param(qs, "pontos")
    if not bruto:
        raise ValueError("Informe os pontos.")
    partes = [parte.strip() for parte in bruto.split(";") if parte.strip()]
    if len(partes) > 200:
        raise ValueError("No máximo 200 pontos.")
    chaves: list[tuple[str, str]] = []
    for parte in partes:
        pedacos = [item.strip() for item in parte.split(",")]
        if len(pedacos) != 2:
            raise ValueError("Ponto inválido.")
        chave = _chave_ponto(pedacos[0], pedacos[1])
        if chave is None:
            raise ValueError("Ponto inválido.")
        chaves.append(chave)
    lidos = _geo_mapa(chaves)
    faltam = [chave for chave in chaves if lidos.get(chave) is _AUSENTE]
    _enfileirar_chaves(faltam)
    enderecos = []
    for la, lo in chaves:
        guardado = lidos.get((la, lo), _AUSENTE)
        enderecos.append(
            {
                "lat": float(la),
                "lon": float(lo),
                "endereco": None if guardado is _AUSENTE else (str(guardado) or None),
            }
        )
    return {"ok": True, "enderecos": enderecos}


def consultar_eventos(qs: dict[str, list[str]]) -> dict[str, Any]:
    """Ranking do turno: mais eventos primeiro e, no empate, mais tempo ocioso."""
    op = _operacao_efetiva(qs)
    inicio, fim, dia, turno, _minimo, _maximo, _placa = _filtros(qs)
    if turno not in {"diurno", "noturno"}:
        raise ValueError("Informe o turno: diurno ou noturno.")
    settings = load_settings()
    token = login(settings)
    mapa = _mapa_veiculos(settings, token)
    avisos: list[str] = []
    if op is None:
        placas = sorted(
            {
                _fmt_placa(str(item.get("ras_vei_placa") or ""))
                for item in mapa.values()
                if _norm_placa(str(item.get("ras_vei_placa") or ""))
            }
        )
        nome = "Todas"
    else:
        placas = sorted(_placas_operacao(op))
        nome = str(op.get("nome") or "")
    alertas, falha_alertas = _alertas_do_turno(settings, token, inicio, fim, avisos)
    pontos = _pontos_das_placas(placas, inicio, fim, settings, token, mapa, avisos)
    linhas: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(placas)))) as pool:
        futuros = {
            pool.submit(
                _linha_eventos,
                placa,
                pontos.get(placa) or [],
                mapa.get(_norm_placa(placa)) or {},
                alertas,
                settings,
                token,
                inicio,
                fim,
                dia,
            ): placa
            for placa in placas
        }
        for fut in as_completed(futuros):
            placa = futuros[fut]
            try:
                linhas.append(fut.result())
            except Exception as exc:
                avisos.append(f"{placa}: {exc}")
    linhas.sort(key=lambda item: (-int(item["eventos"]), -(item["tempo_ocioso_min"] or 0), item["placa"]))
    return {
        "ok": True,
        "operacao": nome,
        "data": dia.isoformat(),
        "turno": turno,
        "inicio": _iso(inicio),
        "fim": _iso(fim),
        "caminhoes": linhas,
        "avisos": avisos,
        "consolidado_indisponivel": bool(falha_alertas or any(item.get("consolidado_indisponivel") for item in linhas)),
    }


def consultar_trajeto(qs: dict[str, list[str]]) -> dict[str, Any]:
    op = _operacao_efetiva(qs)
    inicio, fim, _dia, _turno, _minimo, _maximo, placa = _filtros(qs, exigir_placa=True)
    if op is not None:
        _recusar_fora(placa, op)
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
        "cidade": _cidade_evento(ev),
    }


def _cidade_evento(ev: dict[str, Any] | None) -> str:
    if not ev:
        return ""
    for chave in (
        "ras_eve_cidade",
        "cidade",
        "ras_eve_municipio",
        "municipio",
        "ras_eve_endereco",
        "endereco",
        "ras_eve_localizacao",
        "localizacao",
    ):
        texto = str(ev.get(chave) or "").strip()
        if texto:
            return texto
    return ""


def _status(vel: float | None, minutos: float | None) -> str:
    if minutos is None or minutos >= 15:
        return "sem_sinal"
    if minutos >= 5:
        return "atrasado"
    if vel is not None and vel > VEL_PARADO:
        return "movimento"
    return "parado"


def _historico_frota(
    inicio: datetime,
    fim: datetime,
    avisos: list[str],
    placas: set[str] | None = None,
) -> list[list[dict[str, Any]]]:
    if placas is not None and not placas:
        return []
    settings = load_settings()
    token = login(settings)
    mapa = _mapa_veiculos(settings, token)
    if placas is None:
        alvos = sorted({_fmt_placa(str(item.get("ras_vei_placa") or "")) for item in mapa.values()})
        alvos = [placa for placa in alvos if _norm_placa(placa)]
    else:
        quer = {_norm_placa(placa) for placa in placas}
        alvos = sorted(
            {
                _fmt_placa(str(item.get("ras_vei_placa") or ""))
                for item in mapa.values()
                if _norm_placa(str(item.get("ras_vei_placa") or "")) in quer
            }
        )
        alvos = [placa for placa in alvos if _norm_placa(placa)]
        achadas = {_norm_placa(placa) for placa in alvos}
        for placa in sorted(placas):
            if _norm_placa(placa) not in achadas:
                avisos.append(f"{placa}: Placa {placa} não encontrada na frota.")
    if not alvos:
        return []
    blocos: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(alvos))) as pool:
        futuros = {
            pool.submit(historico, placa, inicio, fim, settings=settings, token=token, veiculos=mapa): placa
            for placa in alvos
        }
        for fut in as_completed(futuros):
            placa = futuros[fut]
            try:
                blocos[placa] = fut.result()
            except Exception as exc:
                avisos.append(f"{placa}: {exc}")
    return [blocos[placa] for placa in alvos if placa in blocos]


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


def _baixar(
    settings: Any,
    vid: str,
    placa: str,
    bloco: tuple[datetime, datetime],
    token: str,
    *,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    inicio, fim = bloco
    if fim <= inicio:
        return []
    print(f"Frota: historico {placa} {_iso(inicio)} -> {_iso(fim)}", flush=True)
    try:
        eventos = historico_veiculo(
            settings,
            vid,
            inicio,
            fim,
            token=token,
            timeout=TIMEOUT_OPCIONAL_S if timeout is None else timeout,
        )
    except FulltrackSemDados:
        return []
    return eventos or []


def _ponto_evento(evento: dict[str, Any], placa: str) -> dict[str, Any] | None:
    lat, lon = _coord(evento.get("ras_eve_latitude"), evento.get("ras_eve_longitude"))
    if lat is None or lon is None:
        if str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "").strip():
            with _STATS_LOCK:
                _PULSO_STATS["sem_coord"] += 1
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
        "direcao": _numero_ou_none(evento.get("ras_eve_direcao")),
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
                "direcao": _numero_ou_none(
                    bruto.get("direcao") if "direcao" in bruto else bruto.get("ras_eve_direcao")
                ),
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


def _esperar_nominatim() -> None:
    """No máximo uma consulta ao Nominatim por segundo. Chamar com _GEO preso."""
    global _GEO_ULTIMO
    espera = 1.0 - (time.monotonic() - _GEO_ULTIMO)
    if _GEO_ULTIMO and espera > 0:
        time.sleep(espera)
    _GEO_ULTIMO = time.monotonic()


def _nominatim(lat: str, lon: str) -> str:
    with _GEO:
        guardado = _com_banco(lambda con: _geo_ler(con, lat, lon))
        if guardado is not _AUSENTE:
            return str(guardado)
        _esperar_nominatim()
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


def _chave_ponto(lat: Any, lon: Any) -> tuple[str, str] | None:
    try:
        la = f"{round(float(lat), 4):.4f}"
        lo = f"{round(float(lon), 4):.4f}"
    except (TypeError, ValueError):
        return None
    if abs(float(la)) < 0.5 or abs(float(lo)) < 0.5:
        return None
    return la, lo


def _geo_mapa(chaves: list[tuple[str, str]]) -> dict[tuple[str, str], Any]:
    unicas = list(dict.fromkeys(chaves))
    if not unicas:
        return {}

    def ler(con: sqlite3.Connection) -> dict[tuple[str, str], Any]:
        return {chave: _geo_ler(con, chave[0], chave[1]) for chave in unicas}

    return _com_banco(ler)


def _endereco_cache(lat: Any, lon: Any) -> str | None:
    chave = _chave_ponto(lat, lon)
    if chave is None:
        return None
    guardado = _geo_mapa([chave]).get(chave, _AUSENTE)
    if guardado is _AUSENTE:
        return None
    return str(guardado) or None


def _enfileirar_pontos(pontos: Any) -> None:
    chaves = []
    for item in pontos:
        if isinstance(item, tuple) and len(item) == 2:
            chave = _chave_ponto(item[0], item[1])
        else:
            continue
        if chave is not None:
            chaves.append(chave)
    if not chaves:
        return
    presentes = _geo_mapa(chaves)
    _enfileirar_chaves([chave for chave in chaves if presentes.get(chave) is _AUSENTE])


def _enfileirar_chaves(chaves: list[tuple[str, str]]) -> None:
    if not chaves:
        return
    with _FILA_LOCK:
        for chave in chaves:
            if chave in _FILA_SET or len(_FILA) >= _FILA_MAX:
                continue
            _FILA.append(chave)
            _FILA_SET.add(chave)
        pendente = bool(_FILA)
    if pendente:
        _acordar_fila()


def _acordar_fila() -> None:
    global _FILA_THREAD
    _FILA_AVISO.set()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _FILA_LOCK:
        if _FILA_THREAD is not None and _FILA_THREAD.is_alive():
            return
        _FILA_THREAD = threading.Thread(target=_worker_geo, name="frota-geocode", daemon=True)
        _FILA_THREAD.start()


def _processar_um_geo() -> bool:
    with _FILA_LOCK:
        if not _FILA:
            return False
        chave = _FILA.popleft()
    try:
        guardado = _geo_mapa([chave]).get(chave, _AUSENTE)
        if guardado is _AUSENTE:
            _nominatim(chave[0], chave[1])
    except Exception:
        _avisar_geo()
    finally:
        with _FILA_LOCK:
            _FILA_SET.discard(chave)
    return True


def _worker_geo() -> None:
    while True:
        if _processar_um_geo():
            continue
        _FILA_AVISO.wait(timeout=5)
        _FILA_AVISO.clear()


def _aplicar_enderecos(itens: list[dict[str, Any]], *, enfileirar: bool) -> None:
    chaves = []
    for item in itens:
        if not isinstance(item, dict) or item.get("endereco"):
            continue
        chave = _chave_ponto(item.get("lat"), item.get("lon"))
        if chave is not None:
            chaves.append(chave)
    lidos = _geo_mapa(chaves)
    faltam: list[tuple[str, str]] = []
    for item in itens:
        if not isinstance(item, dict) or item.get("endereco"):
            continue
        chave = _chave_ponto(item.get("lat"), item.get("lon"))
        if chave is None:
            item["endereco"] = None
            continue
        guardado = lidos.get(chave, _AUSENTE)
        if guardado is _AUSENTE:
            item["endereco"] = None
            faltam.append(chave)
        else:
            item["endereco"] = str(guardado) or None
    if enfileirar:
        _enfileirar_chaves(faltam)


def esperar_enderecos(itens: list[dict[str, Any]], limite_s: float = LIMITE_ESPERA_GEO_S) -> None:
    """O PDF espera a fila, até o limite. O que não chegar sai sem endereço."""
    _aplicar_enderecos(itens, enfileirar=True)
    prazo = time.monotonic() + max(0.0, float(limite_s))
    while time.monotonic() < prazo and _faltam_endereco(itens):
        time.sleep(0.2)
        _aplicar_enderecos(itens, enfileirar=False)


def _faltam_endereco(itens: list[dict[str, Any]]) -> bool:
    for item in itens:
        if item.get("endereco"):
            continue
        if _chave_ponto(item.get("lat"), item.get("lon")) is not None:
            return True
    return False


def _tel_chave(placa: str, dia: date, turno: str | None) -> str:
    from .live_db import caminho_banco

    return f"{caminho_banco()}|{_norm_placa(placa)}|{dia.isoformat()}|{turno or ''}"


def _tel_ttl(fim: datetime) -> float:
    if _agora_brt() < fim:
        return _TEL_ABERTO_S
    return _TEL_FECHADO_S


def _tel_ler(chave: str) -> dict[str, Any] | None:
    agora = time.monotonic()
    with _TEL_LOCK:
        item = _TEL.get(chave)
        if item is None or agora >= item[0]:
            _TEL.pop(chave, None)
            return None
        return copy.deepcopy(item[1])


def _tel_gravar(chave: str, corpo: dict[str, Any], ttl: float) -> None:
    agora = time.monotonic()
    with _TEL_LOCK:
        vencidas = [nome for nome, item in _TEL.items() if agora >= item[0]]
        for nome in vencidas:
            _TEL.pop(nome, None)
        while len(_TEL) >= _TEL_MAX:
            _TEL.pop(next(iter(_TEL)))
        _TEL[chave] = (agora + ttl, copy.deepcopy(corpo))


def _preencher(
    placa_fmt: str,
    inicio: datetime,
    fim: datetime,
    settings: Any,
    token: str | None,
    veiculos: dict[str, dict[str, Any]] | None,
    *,
    timeout: int | None = None,
) -> None:
    faltam = _com_banco(lambda con: _faltantes(con, placa_fmt, inicio, fim))
    if not faltam:
        return
    settings = settings or load_settings()
    token = token or login(settings)
    mapa = veiculos if veiculos is not None else _mapa_veiculos(settings, token)
    _placa, vid = _exigir_veiculo(placa_fmt, mapa)
    for bloco in _achatar(faltam):
        eventos = _baixar(settings, vid, placa_fmt, bloco, token, timeout=timeout)
        pontos = [item for ev in eventos if (item := _ponto_evento(ev, placa_fmt))]
        _com_banco(lambda con, bloco=bloco, pontos=pontos: _salvar(con, placa_fmt, bloco, pontos))


def _esquecer(con: sqlite3.Connection, placa: str, inicio: datetime, fim: datetime) -> None:
    con.execute(
        "DELETE FROM frota_ponto WHERE placa = ? AND ts >= ? AND ts <= ?",
        (placa, _iso(inicio), _iso(fim)),
    )
    con.execute(
        "DELETE FROM frota_janela WHERE placa = ? AND fim > ? AND inicio < ?",
        (placa, _iso(inicio), _iso(fim)),
    )


def _conducao_de(
    placa: str,
    pontos: list[dict[str, Any]],
    limite: Any,
    alertas: list[dict[str, Any]],
    ocioso: dict[str, float] | None,
    indisponivel: bool,
) -> dict[str, Any]:
    da_placa = [
        item
        for item in alertas
        if _norm_placa(str(item.get("placa") or "")) == _norm_placa(placa)
    ]
    corpo = analisar_conducao(pontos, limite, da_placa, ocioso)
    corpo["alerta_ignicao"] = ALERTA_IGNICAO if corpo["ignicao_sempre_ligada"] else ""
    corpo["consolidado_indisponivel"] = bool(indisponivel)
    return corpo


def _alertas_do_turno(
    settings: Any,
    token: str,
    inicio: datetime,
    fim: datetime,
    avisos: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    try:
        brutos = alertas_periodo(settings, inicio, fim, token, timeout=TIMEOUT_OPCIONAL_S)
    except Exception as exc:
        avisos.append(f"Alertas: {exc}")
        return [], True
    saida: list[dict[str, Any]] = []
    for bruto in brutos:
        evento = normalizar_alerta(bruto)
        if not evento:
            continue
        momento = _parse_ts(evento.get("data_hora"))
        if momento is None or momento < inicio or momento > fim:
            continue
        saida.append(evento)
    return saida, False


def _ocioso_api(
    settings: Any,
    token: str,
    vehicle_id: str,
    dia: date | None,
) -> tuple[dict[str, float] | None, bool]:
    if not vehicle_id or dia is None:
        return None, False
    try:
        corpo = consolidado_veiculo(settings, vehicle_id, dia, token, timeout=TIMEOUT_OPCIONAL_S)
    except Exception:
        return None, True
    if not corpo:
        return None, False
    ocioso = _minutos_relogio(corpo.get("tempo_total_ocioso"))
    ligado = _minutos_relogio(corpo.get("tempo_total_ligado"))
    if ocioso is None or ligado is None:
        return None, False
    return {"ocioso_min": ocioso, "ligado_min": ligado}, False


def _minutos_relogio(valor: Any) -> float | None:
    texto = str(valor or "").strip()
    partes = texto.split(":")
    if len(partes) != 3:
        return None
    try:
        hora, minuto, segundo = (int(parte) for parte in partes)
    except ValueError:
        return None
    if hora < 0 or minuto < 0 or segundo < 0:
        return None
    return hora * 60 + minuto + segundo / 60.0


def _pontos_das_placas(
    placas: list[str],
    inicio: datetime,
    fim: datetime,
    settings: Any,
    token: str,
    mapa: dict[str, dict[str, Any]],
    avisos: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not placas:
        return {}
    saida: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(placas))) as pool:
        futuros = {
            pool.submit(
                historico,
                placa,
                inicio,
                fim,
                settings=settings,
                token=token,
                veiculos=mapa,
                incluir_direcao=True,
            ): placa
            for placa in placas
        }
        for fut in as_completed(futuros):
            placa = futuros[fut]
            try:
                saida[placa] = fut.result()
            except Exception as exc:
                avisos.append(f"{placa}: {exc}")
    return saida


def _linha_eventos(
    placa: str,
    pontos: list[dict[str, Any]],
    veiculo: dict[str, Any],
    alertas: list[dict[str, Any]],
    settings: Any,
    token: str,
    inicio: datetime,
    fim: datetime,
    dia: date,
) -> dict[str, Any]:
    da_placa = [
        item
        for item in alertas
        if _norm_placa(str(item.get("placa") or "")) == _norm_placa(placa)
    ]
    ocioso, indisponivel = _ocioso_api(settings, token, str(veiculo.get("ras_vei_id") or ""), dia)
    corpo = analisar_conducao(pontos, veiculo.get("ras_vei_velocidade_limite"), da_placa, ocioso)
    _enfileirar_pontos((item.get("lat"), item.get("lon")) for item in corpo["eventos"])
    tempo = corpo["ocioso"].get("tempo_min")
    return {
        "placa": placa,
        "descricao": str(veiculo.get("ras_vei_veiculo") or placa).strip() or placa,
        "eventos": len(corpo["eventos"]),
        "contagem": corpo["contagem"],
        "tempo_ocioso_min": tempo,
        "ocioso_percentual": corpo["ocioso"].get("percentual"),
        "ocioso_origem": corpo["ocioso"].get("origem"),
        "ignicao_sempre_ligada": corpo["ignicao_sempre_ligada"],
        "limite_kmh": corpo["limite_kmh"],
        "consolidado_indisponivel": indisponivel,
    }


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
            INSERT OR REPLACE INTO frota_ponto (placa, ts, lat, lon, vel, ignicao, direcao)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                placa,
                ponto["ts"],
                ponto["lat"],
                ponto["lon"],
                ponto["vel"],
                _ign_int(ponto["ignicao"]),
                ponto.get("direcao"),
            ),
        )
    ini, fim = bloco
    con.execute(
        "INSERT OR IGNORE INTO frota_janela (placa, inicio, fim) VALUES (?, ?, ?)",
        (placa, _iso(ini), _iso(fim)),
    )


def _ler(con: sqlite3.Connection, placa: str, inicio: datetime, fim: datetime) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT ts, lat, lon, vel, ignicao, direcao FROM frota_ponto
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
                "direcao": row["direcao"],
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
        """
        INSERT INTO frota_geocache (lat, lon, endereco) VALUES (?, ?, ?)
        ON CONFLICT(lat, lon) DO UPDATE SET endereco = excluded.endereco
        """,
        (lat, lon, texto),
    )


def _geo_ler_lugar(con: sqlite3.Connection, lat: str, lon: str) -> dict[str, str] | None:
    row = con.execute(
        "SELECT endereco, cidade, uf FROM frota_geocache WHERE lat = ? AND lon = ?",
        (lat, lon),
    ).fetchone()
    if row is None or row["cidade"] is None:
        return None
    return {
        "endereco": str(row["endereco"] or ""),
        "cidade": str(row["cidade"] or ""),
        "uf": str(row["uf"] or ""),
    }


def _geo_gravar_lugar(con: sqlite3.Connection, lat: str, lon: str, endereco: str, cidade: str, uf: str) -> None:
    con.execute(
        """
        INSERT INTO frota_geocache (lat, lon, endereco, cidade, uf) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(lat, lon) DO UPDATE SET
            endereco = excluded.endereco,
            cidade = excluded.cidade,
            uf = excluded.uf
        """,
        (lat, lon, endereco, cidade, uf),
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


def _sem_acento(texto: str) -> str:
    bruto = unicodedata.normalize("NFKD", str(texto or ""))
    sem = "".join(ch for ch in bruto if not unicodedata.combining(ch))
    return " ".join(sem.lower().split())


def _sigla_uf(texto: str) -> str:
    limpo = _sem_acento(texto)
    if not limpo:
        return ""
    if len(limpo) == 2 and limpo.isalpha():
        return limpo.upper()
    return _ESTADOS.get(limpo, "")


def _chave_op(cidade: str, uf: str) -> str:
    return f"{_sem_acento(cidade)}|{uf.strip().lower()}"


def _rotulo_operacao(cidade: str, uf: str) -> str:
    if _sem_acento(cidade) == "aparecida de goiania":
        return "Aparecida de Goiânia"
    return f"{cidade} - {uf}"


def _operacao_por_cidade(cidade: str, uf: str) -> tuple[str, str] | None:
    alvo = _sem_acento(cidade)
    if not alvo:
        return None
    sigla = _sigla_uf(uf)
    achados = [(nome, estado) for nome, estado in OPERACOES_ALVO if _sem_acento(nome) == alvo and (not sigla or sigla == estado)]
    if len(achados) == 1:
        return achados[0]
    return None


def _cidade_address(address: dict[str, Any]) -> str:
    for chave in ("city", "town", "municipality"):
        texto = str(address.get(chave) or "").strip()
        if texto:
            return texto
    return ""


def _uf_address(address: dict[str, Any]) -> str:
    for chave in ("ISO3166-2-lvl4", "ISO3166-2-lvl3"):
        iso = str(address.get(chave) or "").strip().upper()
        if "-" in iso:
            sigla = iso.split("-")[-1].strip()
            if len(sigla) == 2 and sigla.isalpha():
                return sigla
    return _sigla_uf(str(address.get("state") or ""))


def _nominatim_lugar(lat: str, lon: str) -> dict[str, str]:
    with _GEO:
        guardado = _com_banco(lambda con: _geo_ler_lugar(con, lat, lon))
        if guardado is not None:
            return guardado
        _esperar_nominatim()
        query = urlencode(
            {
                "format": "jsonv2",
                "lat": lat,
                "lon": lon,
                "zoom": "12",
                "addressdetails": "1",
            }
        )
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
        address = corpo.get("address") if isinstance(corpo, dict) else {}
        if not isinstance(address, dict):
            address = {}
        cidade = _cidade_address(address)
        uf = _uf_address(address)
        texto = str(corpo.get("display_name") or "").strip() if isinstance(corpo, dict) else ""
        _com_banco(lambda con: _geo_gravar_lugar(con, lat, lon, texto, cidade, uf))
        return {"endereco": texto, "cidade": cidade, "uf": uf}


def _lugar(lat: float, lon: float) -> dict[str, str]:
    la = f"{float(lat):.3f}"
    lo = f"{float(lon):.3f}"
    guardado = _com_banco(lambda con: _geo_ler_lugar(con, la, lo))
    if guardado is not None:
        return guardado
    try:
        return _nominatim_lugar(la, lo)
    except Exception:
        _avisar_geo()
        return {"endereco": "", "cidade": "", "uf": ""}


def _caixa_da_cidade(cidade: str, uf: str) -> dict[str, float]:
    lat_min, lat_max, lon_min, lon_max = _limites_da_cidade(f"{cidade}, {uf}")
    return {
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
        "centro_lat": (lat_min + lat_max) / 2.0,
        "centro_lon": (lon_min + lon_max) / 2.0,
    }


def _caixas_das_operacoes(avisos: list[str]) -> dict[str, dict[str, float]]:
    caixas: dict[str, dict[str, float]] = {}
    for cidade, uf in OPERACOES_ALVO:
        try:
            caixas[_chave_op(cidade, uf)] = _caixa_da_cidade(cidade, uf)
        except ValueError as exc:
            avisos.append(f"{_rotulo_operacao(cidade, uf)}: {exc}")
    return caixas


def _celula_de(lat: float, lon: float) -> tuple[float, float]:
    return (round(float(lat), 3), round(float(lon), 3))


def _tempo_por_celula(pontos: list[dict[str, Any]]) -> tuple[dict[tuple[float, float], float], dict[tuple[float, float], float]]:
    """Tempo parado+em trânsito e tempo só em movimento, por célula de ~100 m."""
    lista: list[dict[str, Any]] = []
    for bruto in pontos or []:
        if not isinstance(bruto, dict):
            continue
        momento = bruto.get("ts") if isinstance(bruto.get("ts"), datetime) else _parse_ts(bruto.get("ts"))
        lat, lon = _coord(bruto.get("lat"), bruto.get("lon"))
        if momento is None or lat is None or lon is None:
            continue
        lista.append({"ts": momento, "lat": lat, "lon": lon, "vel": _numero_ou_none(bruto.get("vel"))})
    lista.sort(key=lambda item: item["ts"])
    dwell: dict[tuple[float, float], float] = {}
    mov: dict[tuple[float, float], float] = {}
    if len(lista) == 1:
        dwell[_celula_de(lista[0]["lat"], lista[0]["lon"])] = 1.0
        return dwell, mov
    for a, b in zip(lista, lista[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt <= 0 or dt > CAP_BASE_S:
            continue
        celula = _celula_de(a["lat"], a["lon"])
        dwell[celula] = dwell.get(celula, 0.0) + dt
        vel = a.get("vel")
        if vel is None and dt > 0:
            vel = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) / dt * 3.6
        if vel is not None and float(vel) > VEL_PARADO:
            mov[celula] = mov.get(celula, 0.0) + dt
    if lista and not dwell:
        contagem: dict[tuple[float, float], int] = {}
        for item in lista:
            celula = _celula_de(item["lat"], item["lon"])
            contagem[celula] = contagem.get(celula, 0) + 1
        base = max(contagem, key=lambda cel: (contagem[cel], cel[0], cel[1]))
        dwell[base] = 0.0
    return dwell, mov


def _mais_proxima(
    lat: float,
    lon: float,
    caixas: dict[str, dict[str, float]],
) -> tuple[tuple[str, str] | None, float]:
    melhor: tuple[str, str] | None = None
    dist_min: float | None = None
    for cidade, uf in OPERACOES_ALVO:
        caixa = caixas.get(_chave_op(cidade, uf))
        if not caixa:
            continue
        dist = haversine_m((lat, lon), (float(caixa["centro_lat"]), float(caixa["centro_lon"])))
        if dist_min is None or dist < dist_min:
            dist_min = dist
            melhor = (cidade, uf)
    return melhor, dist_min if dist_min is not None else 1e18


def _classificar_base(
    cidade: str,
    uf: str,
    lat: float | None,
    lon: float | None,
    caixas: dict[str, dict[str, float]],
) -> tuple[str, str]:
    achou = _operacao_por_cidade(cidade, uf)
    if achou:
        return _rotulo_operacao(*achou), "associado"
    if lat is None or lon is None:
        return "", "não associado"
    perto, dist = _mais_proxima(lat, lon, caixas)
    if perto is not None and dist <= RAIO_VIZINHA_M:
        return _rotulo_operacao(*perto), "revisar"
    return "", "não associado"


def _cidade_mais_rodou(
    mov: dict[tuple[float, float], float],
    lugares: dict[tuple[float, float], dict[str, str]],
) -> str:
    if not mov:
        return ""
    ordenadas = sorted(mov.items(), key=lambda item: (-item[1], item[0]))
    por_cidade: dict[str, float] = {}
    nomes: dict[str, str] = {}
    restante = sum(mov.values())
    consultas = 0
    for celula, tempo in ordenadas:
        restante -= tempo
        nova = celula not in lugares
        lugar = lugares.get(celula) or _lugar(celula[0], celula[1])
        lugares[celula] = lugar
        if nova:
            consultas += 1
        chave = _sem_acento(lugar.get("cidade") or "")
        if chave:
            por_cidade[chave] = por_cidade.get(chave, 0.0) + tempo
            nomes[chave] = str(lugar.get("cidade") or "")
        if por_cidade and max(por_cidade.values()) > restante:
            break
        if consultas >= 6:
            break
    if not por_cidade:
        return ""
    return nomes[max(por_cidade, key=lambda chave: por_cidade[chave])]


def _coord_evento(ev: dict[str, Any] | None) -> tuple[float, float] | None:
    if not ev:
        return None
    lat, lon = _coord(ev.get("ras_eve_latitude"), ev.get("ras_eve_longitude"))
    if lat is None or lon is None:
        return None
    return lat, lon


def _historicos_da_conta(
    placas: list[str],
    inicio: datetime,
    fim: datetime,
    settings: Any,
    token: str,
    mapa: dict[str, dict[str, Any]],
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    saida: dict[str, tuple[list[dict[str, Any]], str]] = {}

    def um(placa: str) -> tuple[list[dict[str, Any]], str]:
        try:
            return historico(placa, inicio, fim, settings=settings, token=token, veiculos=mapa), ""
        except Exception as exc:
            try:
                novo = login(settings)
                return historico(placa, inicio, fim, settings=settings, token=novo, veiculos=mapa), ""
            except Exception:
                return [], str(exc)

    if not placas:
        return saida
    with ThreadPoolExecutor(max_workers=min(4, len(placas))) as pool:
        futuros = {pool.submit(um, placa): placa for placa in placas}
        for fut in as_completed(futuros):
            placa = futuros[fut]
            saida[placa] = fut.result()
            print(f"Frota: histórico {placa} ({len(saida)}/{len(placas)})", flush=True)
    return saida


def _linha_base(
    placa: str,
    descricao: str,
    *,
    lat: float | None,
    lon: float | None,
    horas: float | None,
    cidade: str,
    uf: str,
    rodou: str,
    operacao: str,
    situacao: str,
    origem: str,
    pulsos: int,
    posicao_valida: bool,
) -> dict[str, Any]:
    return {
        "placa": placa,
        "descricao": descricao,
        "base_lat": None if lat is None else round(float(lat), 3),
        "base_lon": None if lon is None else round(float(lon), 3),
        "cidade_da_base": cidade,
        "uf_da_base": uf,
        "horas_na_base": None if horas is None else round(float(horas), 1),
        "cidade_onde_mais_rodou": rodou,
        "operacao_sugerida": operacao,
        "operacao_chave": _chave_op(*_operacao_por_cidade(cidade, uf)) if situacao == "associado" and _operacao_por_cidade(cidade, uf) else _chave_sugerida(operacao),
        "situacao": situacao,
        "origem": origem,
        "qtd_pulsos": pulsos,
        "posicao_valida": posicao_valida,
    }


def _chave_sugerida(rotulo: str) -> str:
    texto = _sem_acento(rotulo)
    for cidade, uf in OPERACOES_ALVO + OPERACOES_EXTRAS:
        if texto == _sem_acento(_rotulo_operacao(cidade, uf)) or texto == _sem_acento(cidade):
            return _chave_op(cidade, uf)
    return ""


def _montar_linha(
    placa: str,
    descricao: str,
    pontos: list[dict[str, Any]],
    erro: str,
    evento: dict[str, Any] | None,
    inicio: datetime,
    caixas: dict[str, dict[str, float]],
    lugares: dict[tuple[float, float], dict[str, str]],
) -> dict[str, Any]:
    coords_evento = _coord_evento(evento)
    dwell, mov = _tempo_por_celula(pontos) if pontos else ({}, {})
    if dwell:
        celula = max(dwell, key=lambda item: (dwell[item], item[0], item[1]))
        lugar = lugares.get(celula) or _lugar(celula[0], celula[1])
        lugares[celula] = lugar
        operacao, situacao = _classificar_base(lugar.get("cidade") or "", lugar.get("uf") or "", celula[0], celula[1], caixas)
        return _linha_base(
            placa,
            descricao,
            lat=celula[0],
            lon=celula[1],
            horas=dwell.get(celula, 0.0) / 3600.0,
            cidade=str(lugar.get("cidade") or ""),
            uf=str(lugar.get("uf") or ""),
            rodou=_cidade_mais_rodou(mov, lugares),
            operacao=operacao,
            situacao=situacao,
            origem="historico",
            pulsos=len(pontos),
            posicao_valida=True,
        )
    if erro and coords_evento is not None:
        momento = _parse_ts(_iso_de_utc((evento or {}).get("ras_eve_data_gps") or (evento or {}).get("ras_eve_data_enviado")))
        if momento is None or momento >= inicio:
            celula = _celula_de(*coords_evento)
            lugar = lugares.get(celula) or _lugar(celula[0], celula[1])
            lugares[celula] = lugar
            operacao, situacao = _classificar_base(lugar.get("cidade") or "", lugar.get("uf") or "", celula[0], celula[1], caixas)
            return _linha_base(
                placa,
                descricao,
                lat=celula[0],
                lon=celula[1],
                horas=None,
                cidade=str(lugar.get("cidade") or ""),
                uf=str(lugar.get("uf") or ""),
                rodou="",
                operacao=operacao,
                situacao=situacao,
                origem="base pela posição atual",
                pulsos=0,
                posicao_valida=True,
            )
    return _linha_base(
        placa,
        descricao,
        lat=None,
        lon=None,
        horas=None,
        cidade="",
        uf="",
        rodou="",
        operacao="",
        situacao="sem dados",
        origem="",
        pulsos=0,
        posicao_valida=coords_evento is not None,
    )


def mapear_operacoes(*, dias: int = 7) -> dict[str, Any]:
    """Lê os pulsos e sugere a operação de cada placa. Não grava operação nem vínculo."""
    if dias < 1:
        raise ValueError("A janela do mapeamento precisa ter ao menos 1 dia.")
    settings = load_settings()
    token = login(settings)
    brutos = listar_veiculos(settings, token)
    mapa: dict[str, dict[str, Any]] = {}
    for item in brutos:
        chave = _norm_placa(str(item.get("ras_vei_placa") or ""))
        if chave and chave not in mapa:
            mapa[chave] = item
    eventos = _eventos_atuais(settings, token)
    por_id: dict[str, dict[str, Any]] = {}
    por_placa: dict[str, dict[str, Any]] = {}
    eventos_sem_latlon = 0
    for ev in eventos:
        if _coord_evento(ev) is None:
            eventos_sem_latlon += 1
        vid = str(ev.get("ras_vei_id") or "")
        if vid and (vid not in por_id or _chave_tempo(ev) >= _chave_tempo(por_id[vid])):
            por_id[vid] = ev
        chave = _norm_placa(str(ev.get("ras_vei_placa") or ""))
        if chave and (chave not in por_placa or _chave_tempo(ev) >= _chave_tempo(por_placa[chave])):
            por_placa[chave] = ev
    fim = _agora_brt() - timedelta(minutes=6)
    inicio = fim - timedelta(days=int(dias))
    placas = sorted(
        (_fmt_placa(str(item.get("ras_vei_placa") or "")) for item in mapa.values()),
        key=lambda placa: placa,
    )
    placas = [placa for placa in placas if _norm_placa(placa)]
    with _STATS_LOCK:
        _PULSO_STATS["sem_coord"] = 0
    historicos = _historicos_da_conta(placas, inicio, fim, settings, token, mapa)
    with _STATS_LOCK:
        historico_sem_latlon = int(_PULSO_STATS["sem_coord"])
    avisos: list[str] = []
    caixas = _caixas_das_operacoes(avisos)
    lugares: dict[tuple[float, float], dict[str, str]] = {}
    veiculos: list[dict[str, Any]] = []
    for indice, placa in enumerate(placas, start=1):
        if indice == 1 or indice % 25 == 0 or indice == len(placas):
            print(f"Frota: cidade da base {indice}/{len(placas)}", flush=True)
        item = mapa.get(_norm_placa(placa)) or {}
        pontos, erro = historicos.get(placa, ([], ""))
        if erro:
            avisos.append(f"{placa}: {erro}")
        evento = por_id.get(str(item.get("ras_vei_id") or "")) or por_placa.get(_norm_placa(placa))
        veiculos.append(
            _montar_linha(
                placa,
                str(item.get("ras_vei_veiculo") or placa).strip() or placa,
                pontos,
                erro,
                evento,
                inicio,
                caixas,
                lugares,
            )
        )
    return _pacote_mapeamento(veiculos, caixas, avisos, eventos_sem_latlon, historico_sem_latlon, inicio, fim)


def _pacote_mapeamento(
    veiculos: list[dict[str, Any]],
    caixas: dict[str, dict[str, float]],
    avisos: list[str],
    eventos_sem_latlon: int,
    historico_sem_latlon: int,
    inicio: datetime,
    fim: datetime,
) -> dict[str, Any]:
    com_pulso = sum(1 for item in veiculos if item.get("origem"))
    sem_posicao = sum(1 for item in veiculos if not item.get("posicao_valida"))
    resumo = []
    for cidade, uf in OPERACOES_ALVO:
        rotulo = _rotulo_operacao(cidade, uf)
        resumo.append(
            {
                "operacao": rotulo,
                "qtd": sum(1 for item in veiculos if item.get("situacao") == "associado" and item.get("operacao_sugerida") == rotulo),
            }
        )
    return {
        "ok": True,
        "inicio": _iso(inicio),
        "fim": _iso(fim),
        "gerado_em": _iso(_agora_brt()),
        "verificacao": {
            "total_veiculos": len(veiculos),
            "com_pulso": com_pulso,
            "sem_pulso": len(veiculos) - com_pulso,
            "sem_posicao_valida": sem_posicao,
            "eventos_sem_latlon": int(eventos_sem_latlon),
            "historico_sem_latlon": int(historico_sem_latlon),
            "latlon_em_todos_os_pulsos": int(eventos_sem_latlon) == 0 and int(historico_sem_latlon) == 0,
        },
        "resumo": resumo,
        "revisar": [item for item in veiculos if item.get("situacao") == "revisar"],
        "nao_associado": [item for item in veiculos if item.get("situacao") == "não associado"],
        "sem_dados": [item for item in veiculos if item.get("situacao") == "sem dados"],
        "veiculos": veiculos,
        "caixas": caixas,
        "avisos": avisos,
    }


def mapeamento_csv_bytes(resultado: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "placa",
            "descricao",
            "base_lat",
            "base_lon",
            "cidade_da_base",
            "horas_na_base",
            "cidade_onde_mais_rodou",
            "operacao_sugerida",
            "situacao",
            "origem",
        ]
    )
    for item in resultado.get("veiculos") or []:
        writer.writerow(
            [
                item.get("placa") or "",
                item.get("descricao") or "",
                "" if item.get("base_lat") is None else item.get("base_lat"),
                "" if item.get("base_lon") is None else item.get("base_lon"),
                item.get("cidade_da_base") or "",
                "" if item.get("horas_na_base") is None else item.get("horas_na_base"),
                item.get("cidade_onde_mais_rodou") or "",
                item.get("operacao_sugerida") or "",
                item.get("situacao") or "",
                item.get("origem") or "",
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def salvar_mapeamento_csv(resultado: dict[str, Any], destino: Any = None) -> str:
    from pathlib import Path

    if destino is None:
        pasta = load_settings().output_dir / "frota"
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / "mapeamento-operacoes.csv"
    caminho = Path(destino)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(mapeamento_csv_bytes(resultado))
    return str(caminho)


def _buscar_operacao_nome(ops: list[dict[str, Any]], cidade: str, uf: str) -> dict[str, Any] | None:
    alvo = _sem_acento(cidade)
    rotulo = _sem_acento(_rotulo_operacao(cidade, uf))
    for op in ops:
        nome = _sem_acento(str(op.get("nome") or ""))
        if nome in {alvo, rotulo}:
            return op
    return None


def _ajustar_mapeamento(resultado: dict[str, Any]) -> dict[str, Any]:
    """Ajustes aprovados: Ariquemes, Caldas Novas e a TRZ-5F28 em Contagem.

    Placas sem dados continuam sem vínculo. Revisar que não está na lista não entra.
    """
    saida = dict(resultado)
    ariquemes = {_norm_placa(placa) for placa in _PLACAS_ARIQUEMES}
    caldas = {_norm_placa(placa) for placa in _PLACAS_CALDAS}
    contagem = _norm_placa(_PLACA_CONTAGEM)
    veiculos: list[dict[str, Any]] = []
    for item in resultado.get("veiculos") or []:
        if not isinstance(item, dict):
            continue
        linha = dict(item)
        norm = _norm_placa(str(linha.get("placa") or ""))
        if norm in ariquemes:
            linha["situacao"] = "associado"
            linha["operacao_sugerida"] = _rotulo_operacao("Ariquemes", "RO")
            linha["operacao_chave"] = _chave_op("Ariquemes", "RO")
        elif norm in caldas:
            linha["situacao"] = "associado"
            linha["operacao_sugerida"] = _rotulo_operacao("Caldas Novas", "GO")
            linha["operacao_chave"] = _chave_op("Caldas Novas", "GO")
        elif norm == contagem:
            linha["situacao"] = "associado"
            linha["operacao_sugerida"] = _rotulo_operacao("Contagem", "MG")
            linha["operacao_chave"] = _chave_op("Contagem", "MG")
        veiculos.append(linha)
    saida["veiculos"] = veiculos
    return saida


def aplicar_mapeamento(resultado: dict[str, Any] | None = None, *, ajustes: bool = False) -> dict[str, Any]:
    """Cria as operações que faltam e fixa só as placas associadas.

    Não apaga vínculo já existente e não grava situação revisar.
    Com ajustes, inclui Ariquemes, Caldas Novas e a TRZ-5F28 em Contagem.
    """
    if resultado is None:
        resultado = mapear_operacoes()
    if not isinstance(resultado, dict):
        raise ValueError("Mapeamento inválido.")
    if ajustes:
        resultado = _ajustar_mapeamento(resultado)
    cidades = list(OPERACOES_ALVO)
    if ajustes:
        cidades.extend(OPERACOES_EXTRAS)
    caixas = dict(resultado.get("caixas") or {})
    avisos = list(resultado.get("avisos") or [])
    for cidade, uf in cidades:
        chave = _chave_op(cidade, uf)
        if chave in caixas or _sem_acento(cidade) == "aparecida de goiania":
            continue
        try:
            caixas[chave] = _caixa_da_cidade(cidade, uf)
        except ValueError as exc:
            avisos.append(f"{_rotulo_operacao(cidade, uf)}: {exc}")

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        ops = _listar_rows(con)
        ids: dict[str, int] = {}
        criadas: list[str] = []
        for cidade, uf in cidades:
            chave = _chave_op(cidade, uf)
            rotulo = _rotulo_operacao(cidade, uf)
            achada = _buscar_operacao_nome(ops, cidade, uf)
            if achada:
                ids[chave] = int(achada["id"])
                continue
            caixa = caixas.get(chave)
            if not caixa:
                avisos.append(f"{rotulo}: sem limites para criar a operação.")
                continue
            cur = con.execute(
                """
                INSERT INTO frota_operacao (
                    nome, lat_min, lat_max, lon_min, lon_max, ativa, padrao, criado_em
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?)
                """,
                (
                    rotulo,
                    float(caixa["lat_min"]),
                    float(caixa["lat_max"]),
                    float(caixa["lon_min"]),
                    float(caixa["lon_max"]),
                    _iso(_agora_brt()),
                ),
            )
            novo = {
                "id": int(cur.lastrowid),
                "nome": rotulo,
            }
            ops.append(novo)
            ids[chave] = int(cur.lastrowid)
            criadas.append(rotulo)
        vinculos: dict[str, set[int]] = {}
        for row in con.execute("SELECT operacao_id, placa FROM frota_operacao_placa"):
            norm = _norm_placa(str(row["placa"]))
            if norm:
                vinculos.setdefault(norm, set()).add(int(row["operacao_id"]))
        fixadas: list[str] = []
        preservadas: list[str] = []
        ja_estavam: list[str] = []
        for item in resultado.get("veiculos") or []:
            if not isinstance(item, dict) or item.get("situacao") != "associado":
                continue
            chave = str(item.get("operacao_chave") or "") or _chave_sugerida(str(item.get("operacao_sugerida") or ""))
            oid = ids.get(chave)
            placa = _fmt_placa(str(item.get("placa") or ""))
            norm = _norm_placa(placa)
            if oid is None or not norm:
                continue
            atuais = vinculos.setdefault(norm, set())
            if oid in atuais:
                ja_estavam.append(placa)
                continue
            if atuais:
                preservadas.append(placa)
                continue
            con.execute(
                "INSERT OR IGNORE INTO frota_operacao_placa (operacao_id, placa) VALUES (?, ?)",
                (int(oid), placa),
            )
            atuais.add(int(oid))
            fixadas.append(placa)
        return {
            "ok": True,
            "criadas": criadas,
            "fixadas": fixadas,
            "preservadas": preservadas,
            "ja_fixadas": ja_estavam,
            "avisos": avisos,
        }

    return _com_banco(gravar)


def _caminho_snapshot(arquivo: str | Path | None = None) -> Path:
    if arquivo:
        return Path(arquivo)
    return ARQUIVO_OPERACOES_APROVADO


def _versao_snapshot(operacoes: list[dict[str, Any]], placas: dict[str, str]) -> str:
    """SHA-256 do JSON canônico de operações e placas, sem o campo versao."""
    ops: list[dict[str, Any]] = []
    for op in operacoes:
        ops.append(
            {
                "ativa": bool(op["ativa"]),
                "lat_max": float(op["lat_max"]),
                "lat_min": float(op["lat_min"]),
                "lon_max": float(op["lon_max"]),
                "lon_min": float(op["lon_min"]),
                "nome": str(op["nome"]),
                "padrao": bool(op["padrao"]),
            }
        )
    ops.sort(key=lambda item: (_sem_acento(item["nome"]), item["nome"]))
    mapa = {str(placa): str(placas[placa]) for placa in sorted(placas, key=str)}
    corpo = json.dumps(
        {"operacoes": ops, "placas": mapa},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(corpo.encode("utf-8")).hexdigest()


def _ler_snapshot(caminho: Path) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Arquivo inválido: {exc}") from exc
    if not isinstance(bruto, dict):
        raise ValueError("Arquivo inválido.")
    ops_brutas = bruto.get("operacoes")
    placas_brutas = bruto.get("placas")
    if not isinstance(ops_brutas, list) or not isinstance(placas_brutas, dict):
        raise ValueError("Arquivo inválido.")
    operacoes: list[dict[str, Any]] = []
    chaves: set[str] = set()
    for item in ops_brutas:
        if not isinstance(item, dict):
            raise ValueError("Operação inválida no snapshot.")
        nome = item.get("nome")
        if not isinstance(nome, str) or not nome.strip():
            raise ValueError("Operação sem nome no snapshot.")
        chave = _sem_acento(nome)
        if chave in chaves:
            raise ValueError(f"Operação repetida: {nome}")
        chaves.add(chave)
        limites: list[float] = []
        for campo in ("lat_min", "lat_max", "lon_min", "lon_max"):
            if campo not in item:
                raise ValueError(f"Operação {nome} sem {campo}.")
            try:
                limites.append(float(item[campo]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Limite {campo} inválido.") from exc
        _validar_limites(*limites)
        for campo in ("ativa", "padrao"):
            if not isinstance(item.get(campo), bool):
                raise ValueError(f"Campo {campo} inválido.")
        operacoes.append(
            {
                "nome": nome,
                "lat_min": limites[0],
                "lat_max": limites[1],
                "lon_min": limites[2],
                "lon_max": limites[3],
                "ativa": item["ativa"],
                "padrao": item["padrao"],
            }
        )
    placas: dict[str, str] = {}
    normas: dict[str, str] = {}
    for placa, nome_op in placas_brutas.items():
        if not isinstance(placa, str) or not isinstance(nome_op, str):
            raise ValueError("Placa inválida no snapshot.")
        norm = _norm_placa(placa)
        if not norm:
            raise ValueError("Placa inválida no snapshot.")
        chave_op = _sem_acento(nome_op)
        if chave_op not in chaves:
            raise ValueError(f"Operação da placa {placa.strip() or norm} não está no snapshot.")
        anterior = normas.get(norm)
        if anterior is not None and anterior != chave_op:
            raise ValueError(f"Placa repetida no snapshot: {placa}")
        normas[norm] = chave_op
        placas[placa] = nome_op
    versao = str(bruto.get("versao") or "").strip().lower()
    calculada = _versao_snapshot(operacoes, placas)
    if versao != calculada:
        raise ValueError("Versão do arquivo não confere com o conteúdo.")
    return operacoes, placas, versao


def _aplicar_snapshot(
    con: sqlite3.Connection,
    operacoes: list[dict[str, Any]],
    placas: dict[str, str],
) -> tuple[int, int]:
    por_nome: dict[str, int] = {}
    for row in con.execute("SELECT id, nome FROM frota_operacao ORDER BY id"):
        por_nome.setdefault(_sem_acento(str(row["nome"])), int(row["id"]))
    criadas = 0
    for op in operacoes:
        chave = _sem_acento(str(op["nome"]))
        if chave in por_nome:
            continue
        cur = con.execute(
            """
            INSERT INTO frota_operacao (
                nome, lat_min, lat_max, lon_min, lon_max, ativa, padrao, criado_em
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                str(op["nome"]),
                float(op["lat_min"]),
                float(op["lat_max"]),
                float(op["lon_min"]),
                float(op["lon_max"]),
                1 if op["ativa"] else 0,
                _iso(_agora_brt()),
            ),
        )
        por_nome[chave] = int(cur.lastrowid)
        criadas += 1
    oid_ap = por_nome.get(_sem_acento("Aparecida de Goiânia"))
    if oid_ap is not None:
        con.execute("UPDATE frota_operacao SET padrao = 0 WHERE id != ?", (oid_ap,))
        con.execute("UPDATE frota_operacao SET padrao = 1 WHERE id = ?", (oid_ap,))
    vinculos: dict[str, set[int]] = {}
    for row in con.execute("SELECT operacao_id, placa FROM frota_operacao_placa"):
        norm = _norm_placa(str(row["placa"]))
        if norm:
            vinculos.setdefault(norm, set()).add(int(row["operacao_id"]))
    fixadas = 0
    for placa, nome_op in placas.items():
        norm = _norm_placa(placa)
        oid = por_nome.get(_sem_acento(nome_op))
        if oid is None or not norm:
            raise ValueError(f"Operação da placa {placa} não encontrada.")
        atuais = vinculos.setdefault(norm, set())
        if atuais:
            continue
        con.execute(
            "INSERT OR IGNORE INTO frota_operacao_placa (operacao_id, placa) VALUES (?, ?)",
            (int(oid), placa),
        )
        atuais.add(int(oid))
        fixadas += 1
    return criadas, fixadas


def importar_operacoes_aprovadas(
    arquivo: str | Path | None = None,
    *,
    exigir_arquivo: bool = False,
) -> dict[str, Any]:
    """Cria operações e placas que faltam. Não altera limites nem remove vínculos."""
    caminho = _caminho_snapshot(arquivo)
    if not caminho.is_file():
        if exigir_arquivo or arquivo:
            raise ValueError(f"Arquivo não encontrado: {caminho}")
        return {"aplicado": False, "versao": "", "criadas": 0, "fixadas": 0}
    operacoes, placas, versao = _ler_snapshot(caminho)

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        ja = con.execute("SELECT versao FROM frota_import WHERE versao = ?", (versao,)).fetchone()
        if ja is not None:
            return {"aplicado": False, "versao": versao, "criadas": 0, "fixadas": 0}
        criadas, fixadas = _aplicar_snapshot(con, operacoes, placas)
        con.execute(
            "INSERT INTO frota_import (versao, aplicado_em) VALUES (?, ?)",
            (versao, _iso(_agora_brt())),
        )
        return {"aplicado": True, "versao": versao, "criadas": criadas, "fixadas": fixadas}

    return _com_banco(gravar)


def linha_import(resultado: dict[str, Any]) -> str:
    return (
        f"Frota import: {resultado['criadas']} operações criadas, "
        f"{resultado['fixadas']} placas fixadas (versão {resultado['versao']})"
    )


def importar_na_subida(arquivo: str | Path | None = None) -> None:
    """Cria as tabelas da frota e aplica o snapshot. Erro não impede o servidor de subir."""
    try:
        _com_banco(lambda con: None)
        resultado = importar_operacoes_aprovadas(arquivo)
    except Exception as exc:
        print(f"Frota import: {exc}", flush=True)
        return
    if not resultado.get("aplicado"):
        return
    print(linha_import(resultado), flush=True)


def listar_operacoes() -> dict[str, Any]:
    ops = _com_banco(_listar_rows)
    vivo = _ao_vivo_completo()
    for op in ops:
        op["qtd"] = len(_veiculos_da_operacao(vivo["veiculos"], op, list(op["placas"])))
    return {"ok": True, "operacoes": ops}


def criar_operacao(corpo: dict[str, Any]) -> dict[str, Any]:
    nome, limites = _dados_novos(corpo)
    lat_min, lat_max, lon_min, lon_max = limites

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        cur = con.execute(
            """
            INSERT INTO frota_operacao (
                nome, lat_min, lat_max, lon_min, lon_max, ativa, padrao, criado_em
            ) VALUES (?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (nome, lat_min, lat_max, lon_min, lon_max, _iso(_agora_brt())),
        )
        op = _op_de(con, int(cur.lastrowid))
        if op is None:
            raise ValueError("Não foi possível gravar a operação.")
        return op

    return {"ok": True, "operacao": _com_banco(gravar)}


def atualizar_operacao(operacao_id: int, corpo: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(corpo, dict):
        raise ValueError("Dados inválidos.")

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        row = _row_op(con, operacao_id)
        if row is None:
            raise ValueError("Operação não encontrada.")
        nome = str(row["nome"])
        lat_min, lat_max = float(row["lat_min"]), float(row["lat_max"])
        lon_min, lon_max = float(row["lon_min"]), float(row["lon_max"])
        ativa = int(row["ativa"])
        if "nome" in corpo:
            nome = str(corpo.get("nome") or "").strip()
            if not nome:
                raise ValueError("Informe o nome da operação.")
        if _tem_limites(corpo):
            lat_min, lat_max, lon_min, lon_max = _limites_do_corpo(corpo)
        if "ativa" in corpo:
            ativa = 1 if _flag(corpo.get("ativa"), "ativa") else 0
        quer_padrao = _flag(corpo.get("padrao"), "padrao") if "padrao" in corpo else None
        if quer_padrao is False and int(row["padrao"]):
            raise ValueError("A operação padrão não pode deixar de ser padrão.")
        con.execute(
            """
            UPDATE frota_operacao
            SET nome = ?, lat_min = ?, lat_max = ?, lon_min = ?, lon_max = ?, ativa = ?
            WHERE id = ?
            """,
            (nome, lat_min, lat_max, lon_min, lon_max, ativa, int(operacao_id)),
        )
        if quer_padrao:
            con.execute("UPDATE frota_operacao SET padrao = 0 WHERE id != ?", (int(operacao_id),))
            con.execute("UPDATE frota_operacao SET padrao = 1 WHERE id = ?", (int(operacao_id),))
        op = _op_de(con, int(operacao_id))
        if op is None:
            raise ValueError("Operação não encontrada.")
        return op

    return {"ok": True, "operacao": _com_banco(gravar)}


def apagar_operacao(operacao_id: int) -> dict[str, Any]:
    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        row = _row_op(con, operacao_id)
        if row is None:
            raise ValueError("Operação não encontrada.")
        if int(row["padrao"]):
            raise ValueError("Não é possível apagar a operação padrão.")
        con.execute("DELETE FROM frota_operacao_placa WHERE operacao_id = ?", (int(operacao_id),))
        con.execute("DELETE FROM frota_operacao WHERE id = ?", (int(operacao_id),))
        return {"ok": True}

    return _com_banco(gravar)


def fixar_placa(operacao_id: int, placa: str) -> dict[str, Any]:
    fmt = _fmt_placa(placa)
    if not _norm_placa(fmt):
        raise ValueError("Informe a placa.")

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        if _row_op(con, operacao_id) is None:
            raise ValueError("Operação não encontrada.")
        con.execute(
            "INSERT OR IGNORE INTO frota_operacao_placa (operacao_id, placa) VALUES (?, ?)",
            (int(operacao_id), fmt),
        )
        return {"ok": True, "operacao_id": int(operacao_id), "placa": fmt}

    return _com_banco(gravar)


def desafixar_placa(operacao_id: int, placa: str) -> dict[str, Any]:
    fmt = _fmt_placa(placa)
    if not _norm_placa(fmt):
        raise ValueError("Informe a placa.")

    def gravar(con: sqlite3.Connection) -> dict[str, Any]:
        if _row_op(con, operacao_id) is None:
            raise ValueError("Operação não encontrada.")
        alvo = _norm_placa(fmt)
        for row in con.execute(
            "SELECT placa FROM frota_operacao_placa WHERE operacao_id = ?",
            (int(operacao_id),),
        ):
            if _norm_placa(str(row["placa"])) == alvo:
                con.execute(
                    "DELETE FROM frota_operacao_placa WHERE operacao_id = ? AND placa = ?",
                    (int(operacao_id), row["placa"]),
                )
        return {"ok": True, "operacao_id": int(operacao_id), "placa": fmt}

    return _com_banco(gravar)


def catalogo_veiculos(qs: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Todos os veículos da conta, para escolher qual fixar na operação."""
    busca = _param(qs, "busca")
    termo = busca.lower()
    norm = _norm_placa(busca)
    itens: list[dict[str, Any]] = []
    for bruto in _ao_vivo_completo()["veiculos"]:
        placa = str(bruto.get("placa") or "")
        desc = str(bruto.get("descricao") or "")
        if termo and termo not in placa.lower() and termo not in desc.lower() and not (norm and norm in _norm_placa(placa)):
            continue
        lat, lon = bruto.get("lat"), bruto.get("lon")
        itens.append(
            {
                "placa": placa,
                "descricao": desc,
                "cidade": str(bruto.get("cidade") or ""),
                "lat": lat,
                "lon": lon,
                "posicao": None if lat is None or lon is None else {"lat": lat, "lon": lon},
            }
        )
    return {"ok": True, "qtd": len(itens), "veiculos": itens}


def _operacao_efetiva(qs: dict[str, list[str]] | None) -> dict[str, Any] | None:
    bruto = _param(qs, "operacao")
    if bruto.lower() == "todas":
        return None
    if not bruto:
        op = _com_banco(_ler_padrao)
        if op is None:
            raise ValueError("Nenhuma operação padrão.")
        return op
    try:
        oid = int(bruto)
    except ValueError as exc:
        raise ValueError("Operação inválida.") from exc
    op = _com_banco(lambda con: _op_de(con, oid))
    if op is None:
        raise ValueError("Operação não encontrada.")
    return op


def _recusar_fora(placa: str, op: dict[str, Any]) -> None:
    alvo = _norm_placa(placa)
    if not alvo or alvo not in {_norm_placa(item) for item in _placas_operacao(op)}:
        raise ValueError("Veículo fora desta operação.")


def _placas_operacao(op: dict[str, Any]) -> set[str]:
    placas = _com_banco(lambda con: _fixadas_fmt(con, int(op["id"])))
    vivo = _ao_vivo_completo()
    return {str(item["placa"]) for item in _veiculos_da_operacao(vivo["veiculos"], op, placas)}


def _veiculos_da_operacao(
    veiculos: list[dict[str, Any]],
    op: dict[str, Any],
    placas_fmt: list[str],
) -> list[dict[str, Any]]:
    fixados = {_norm_placa(placa) for placa in placas_fmt}
    fmt_por_norm = {_norm_placa(placa): _fmt_placa(placa) for placa in placas_fmt if _norm_placa(placa)}
    saida: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for bruto in veiculos:
        placa = str(bruto.get("placa") or "")
        norm = _norm_placa(placa)
        if not norm or norm in vistos:
            continue
        vistos.add(norm)
        fixado = norm in fixados
        dentro = _dentro_da_area(bruto.get("lat"), bruto.get("lon"), op)
        if not fixado and not dentro:
            continue
        saida.append(_publico_vivo(bruto, {"fixado": fixado, "fora_da_area": bool(fixado and not dentro)}))
    for norm, placa in fmt_por_norm.items():
        if norm in vistos:
            continue
        saida.append(
            _publico_vivo(
                {
                    "placa": placa,
                    "descricao": placa,
                    "lat": None,
                    "lon": None,
                    "velocidade": None,
                    "ignicao": None,
                    "data_gps": "",
                    "minutos": None,
                    "status": "sem_sinal",
                },
                {"fixado": True, "fora_da_area": True},
            )
        )
    saida.sort(key=lambda row: row["placa"])
    return saida


def _publico_vivo(bruto: dict[str, Any], marca: dict[str, bool] | None = None) -> dict[str, Any]:
    item = {
        "placa": bruto.get("placa") or "",
        "descricao": bruto.get("descricao") or bruto.get("placa") or "",
        "lat": bruto.get("lat"),
        "lon": bruto.get("lon"),
        "velocidade": bruto.get("velocidade"),
        "ignicao": bruto.get("ignicao"),
        "data_gps": bruto.get("data_gps") or "",
        "minutos": bruto.get("minutos"),
        "status": bruto.get("status") or "sem_sinal",
    }
    if marca is not None:
        item["fixado"] = bool(marca["fixado"])
        item["fora_da_area"] = bool(marca["fora_da_area"])
    return item


def _dentro_da_area(lat: Any, lon: Any, op: dict[str, Any]) -> bool:
    if lat is None or lon is None:
        return False
    if _caixa_aparecida(op):
        return _em_aparecida(lat, lon)
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return float(op["lat_min"]) <= la <= float(op["lat_max"]) and float(op["lon_min"]) <= lo <= float(op["lon_max"])


def _caixa_aparecida(op: dict[str, Any]) -> bool:
    try:
        return (
            abs(float(op["lat_min"]) - APARECIDA_LAT[0]) < 1e-9
            and abs(float(op["lat_max"]) - APARECIDA_LAT[1]) < 1e-9
            and abs(float(op["lon_min"]) - APARECIDA_LON[0]) < 1e-9
            and abs(float(op["lon_max"]) - APARECIDA_LON[1]) < 1e-9
        )
    except (TypeError, ValueError, KeyError):
        return False


def _dados_novos(corpo: Any) -> tuple[str, tuple[float, float, float, float]]:
    if not isinstance(corpo, dict):
        raise ValueError("Dados inválidos.")
    nome = str(corpo.get("nome") or "").strip()
    if not nome:
        raise ValueError("Informe o nome da operação.")
    cidade = str(corpo.get("cidade") or "").strip()
    if cidade:
        return nome, _limites_da_cidade(cidade)
    return nome, _limites_do_corpo(corpo)


def _tem_limites(corpo: dict[str, Any]) -> bool:
    if isinstance(corpo.get("limites"), dict):
        return True
    return any(chave in corpo for chave in ("lat_min", "lat_max", "lon_min", "lon_max"))


def _limites_do_corpo(corpo: dict[str, Any]) -> tuple[float, float, float, float]:
    fonte = corpo.get("limites") if isinstance(corpo.get("limites"), dict) else corpo
    if not isinstance(fonte, dict):
        raise ValueError("Informe a cidade ou lat_min, lat_max, lon_min e lon_max.")
    faltando = [
        nome
        for nome in ("lat_min", "lat_max", "lon_min", "lon_max")
        if nome not in fonte or fonte.get(nome) in ("", None)
    ]
    if faltando:
        raise ValueError("Informe a cidade ou lat_min, lat_max, lon_min e lon_max.")
    valores: list[float] = []
    for nome in ("lat_min", "lat_max", "lon_min", "lon_max"):
        try:
            valores.append(float(str(fonte[nome]).replace(",", ".")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Limite {nome} inválido.") from exc
    limites = (valores[0], valores[1], valores[2], valores[3])
    _validar_limites(*limites)
    return limites


def _validar_limites(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> None:
    if not (-34 <= lat_min <= 6 and -34 <= lat_max <= 6):
        raise ValueError("Latitude fora do Brasil: use valores entre -34 e 6.")
    if not (-74 <= lon_min <= -34 and -74 <= lon_max <= -34):
        raise ValueError("Longitude fora do Brasil: use valores entre -74 e -34.")
    if not (lat_min < lat_max and lon_min < lon_max):
        raise ValueError("Limites inválidos: o mínimo precisa ser menor que o máximo.")


def _limites_da_cidade(cidade: str) -> tuple[float, float, float, float]:
    with _GEO:
        _esperar_nominatim()
        query = urlencode({"q": cidade, "format": "json", "countrycodes": "br", "limit": "1"})
        pedido = Request(
            _NOMINATIM_BUSCA + "?" + query,
            headers={
                "User-Agent": "SEGTRACK-frota",
                "Accept": "application/json",
                "Accept-Language": "pt-BR",
            },
        )
        try:
            with urlopen(pedido, timeout=12) as resp:
                corpo = json.loads(resp.read().decode("utf-8", "replace"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise ValueError("Não foi possível obter os limites da cidade.") from exc
    if not isinstance(corpo, list) or not corpo or not isinstance(corpo[0], dict):
        raise ValueError("Cidade não encontrada.")
    box = corpo[0].get("boundingbox")
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        raise ValueError("Cidade sem limites no mapa.")
    try:
        limites = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Cidade sem limites no mapa.") from exc
    _validar_limites(*limites)
    return limites


def _flag(valor: Any, campo: str) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return valor != 0
    texto = str(valor or "").strip().lower()
    if texto in {"1", "true", "sim", "yes", "on"}:
        return True
    if texto in {"0", "false", "nao", "não", "no", "off"}:
        return False
    raise ValueError(f"Campo {campo} inválido.")


def _garantir_colunas(con: sqlite3.Connection) -> None:
    colunas = {str(row[1]) for row in con.execute("PRAGMA table_info(frota_geocache)")}
    if "cidade" not in colunas:
        con.execute("ALTER TABLE frota_geocache ADD COLUMN cidade TEXT")
    if "uf" not in colunas:
        con.execute("ALTER TABLE frota_geocache ADD COLUMN uf TEXT")
    pontos = {str(row[1]) for row in con.execute("PRAGMA table_info(frota_ponto)")}
    if pontos and "direcao" not in pontos:
        con.execute("ALTER TABLE frota_ponto ADD COLUMN direcao REAL")


def _semear_operacao(con: sqlite3.Connection) -> None:
    qtd = int(con.execute("SELECT COUNT(*) AS n FROM frota_operacao").fetchone()["n"])
    if qtd:
        return
    con.execute(
        """
        INSERT INTO frota_operacao (
            nome, lat_min, lat_max, lon_min, lon_max, ativa, padrao, criado_em
        ) VALUES (?, ?, ?, ?, ?, 1, 1, ?)
        """,
        (
            "Aparecida de Goiânia",
            APARECIDA_LAT[0],
            APARECIDA_LAT[1],
            APARECIDA_LON[0],
            APARECIDA_LON[1],
            _iso(_agora_brt()),
        ),
    )


def _listar_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute("SELECT * FROM frota_operacao ORDER BY nome COLLATE NOCASE, id").fetchall()
    return [_op_dict(row, _fixadas_fmt(con, int(row["id"]))) for row in rows]


def _ler_padrao(con: sqlite3.Connection) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM frota_operacao WHERE padrao = 1 ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None
    return _op_dict(row, _fixadas_fmt(con, int(row["id"])))


def _op_de(con: sqlite3.Connection, operacao_id: int) -> dict[str, Any] | None:
    row = _row_op(con, operacao_id)
    if row is None:
        return None
    return _op_dict(row, _fixadas_fmt(con, int(row["id"])))


def _row_op(con: sqlite3.Connection, operacao_id: int) -> sqlite3.Row | None:
    try:
        oid = int(operacao_id)
    except (TypeError, ValueError):
        return None
    return con.execute("SELECT * FROM frota_operacao WHERE id = ?", (oid,)).fetchone()


def _fixadas_fmt(con: sqlite3.Connection, operacao_id: int) -> list[str]:
    rows = con.execute(
        "SELECT placa FROM frota_operacao_placa WHERE operacao_id = ? ORDER BY placa",
        (int(operacao_id),),
    ).fetchall()
    return [str(row["placa"]) for row in rows]


def _op_dict(row: sqlite3.Row, placas: list[str]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "nome": str(row["nome"]),
        "lat_min": float(row["lat_min"]),
        "lat_max": float(row["lat_max"]),
        "lon_min": float(row["lon_min"]),
        "lon_max": float(row["lon_max"]),
        "ativa": bool(row["ativa"]),
        "padrao": bool(row["padrao"]),
        "criado_em": str(row["criado_em"] or ""),
        "placas": list(placas),
    }


@contextmanager
def _conexao() -> Iterator[sqlite3.Connection]:
    from .live_db import caminho_banco

    con = sqlite3.connect(str(caminho_banco()), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=5000")
        con.executescript(_SCHEMA)
        _garantir_colunas(con)
        _semear_operacao(con)
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
