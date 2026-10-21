"""Geometria do mapa ao vivo.

A única fonte oficial é data/mapa-coleta-aparecida.gpkg, lida pelas mesmas
funções de mapa_oficial. O cache fica no processo: três abas não reabrem o
GPKG. O cursor daqui é só o que o navegador já recebeu; não mexe no cursor
da Fulltrack.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, datetime
from typing import Any

import math

from .live_db import conectar

_LOCK = threading.Lock()
_POLIGONOS: list[dict[str, Any]] | None = None
_AREAS: list[dict[str, Any]] | None = None
_CORPOS: dict[tuple[Any, ...], tuple[bytes, str]] = {}
_GAP_M = 480.0


def _dia_turno(data: str | None, turno: str | None) -> tuple[date, str]:
    from .config import parse_data
    from .live_sync import agora_brt, turno_operacional

    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        raise ValueError("Turno inválido. Use diurno ou noturno.")
    dia = parse_data(data) if data else None
    if data and dia is None:
        raise ValueError("Data inválida.")
    if dia is None:
        dia, _ignorado = turno_operacional(agora_brt())
    return dia, pedido


def _poligonos() -> list[dict[str, Any]]:
    """Contornos oficiais, uma vez por processo. Não carrega as ruas."""
    global _POLIGONOS
    with _LOCK:
        if _POLIGONOS is not None:
            return _POLIGONOS
    from .mapa_oficial import (
        _achar_coluna,
        _colunas,
        _gpkg_geom,
        _poligonos_wgs,
        _quote,
        _sincronizar_gpkg,
        _tabela_setores,
        eh_area_dificil,
    )

    arquivo = _sincronizar_gpkg()
    saida: list[dict[str, Any]] = []
    if arquivo:
        import sqlite3

        con = sqlite3.connect(str(arquivo))
        try:
            tabela = _tabela_setores(con)
            if tabela:
                cols = _colunas(con, tabela)
                geom_col = _achar_coluna(cols, "geom", "geometry") or "geom"
                col_codigo = _achar_coluna(cols, "NOVO_CODIGO", "CODIGO")
                col_nome = _achar_coluna(cols, "NOVO_CODINOME", "CODINOME", "NOME")
                col_freq = _achar_coluna(cols, "FREQUÊNCIA", "FREQUENCIA")
                campos = [geom_col, col_codigo, col_nome, col_freq]
                select = ", ".join(_quote(c) if c else "NULL" for c in campos)
                for blob, codigo, nome, freq in con.execute(f"SELECT {select} FROM {_quote(tabela)}"):
                    codigo = str(codigo or "").strip()
                    if not codigo or not blob or eh_area_dificil(codigo):
                        continue
                    geom = _gpkg_geom(blob)
                    aneis = _poligonos_wgs(geom)
                    if not aneis:
                        continue
                    saida.append(
                        {
                            "codigo": codigo.upper(),
                            "nome": str(nome or "").strip(),
                            "frequencia": str(freq or "").strip(),
                            "aneis": aneis,
                        }
                    )
        finally:
            con.close()
    saida.sort(key=lambda row: row["codigo"])
    with _LOCK:
        if _POLIGONOS is None:
            _POLIGONOS = saida
            print(f"GPKG polígonos: 1 leitura, {len(saida)} setores.", flush=True)
        return _POLIGONOS


def areas_dificil() -> list[dict[str, Any]]:
    """Camada separada. Não entra no GeoJSON das rotas oficiais."""
    global _AREAS
    with _LOCK:
        if _AREAS is not None:
            return _AREAS
    from .mapa_oficial import carregar_areas_dificil

    saida = carregar_areas_dificil()
    with _LOCK:
        if _AREAS is None:
            _AREAS = saida
            print(f"GPKG áreas de difícil acesso: {len(saida)} polígonos.", flush=True)
        return _AREAS


def _codigos_persistidos(dia: date, turno: str) -> list[str] | None:
    try:
        con = conectar()
    except RuntimeError:
        return None
    try:
        rows = con.execute(
            "SELECT codigo FROM rota WHERE data = ? AND turno = ? ORDER BY codigo",
            (dia.isoformat(), turno),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    return [str(row["codigo"] or "").upper() for row in rows if str(row["codigo"] or "").strip()]


def _geometria_de(aneis: list[list[list[float]]]) -> dict[str, Any]:
    polys = []
    for anel in aneis:
        coords = [[float(par[1]), float(par[0])] for par in anel if len(par) >= 2]
        if len(coords) >= 4:
            polys.append([coords])
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def geometria_bytes(data: str | None, turno: str | None) -> tuple[bytes, str]:
    from .unificado import _casa_turno, _freq_do_dia

    dia, pedido = _dia_turno(data, turno)
    codigos = _codigos_persistidos(dia, pedido)
    chave = (dia.isoformat(), pedido, tuple(codigos) if codigos is not None else ("*",))
    with _LOCK:
        pronto = _CORPOS.get(chave)
    if pronto is not None:
        return pronto
    pedidos = set(codigos or [])
    features = []
    for setor in _poligonos():
        if not _freq_do_dia(setor["frequencia"], dia):
            continue
        if not _casa_turno(setor["frequencia"], pedido):
            continue
        if pedidos and setor["codigo"] not in pedidos:
            continue
        features.append(
            {
                "type": "Feature",
                "id": setor["codigo"],
                "properties": {
                    "codigo": setor["codigo"],
                    "nome": setor["nome"],
                    "frequencia": setor["frequencia"],
                },
                "geometry": _geometria_de(setor["aneis"]),
            }
        )
    body = json.dumps(
        {"type": "FeatureCollection", "features": features},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
    with _LOCK:
        _CORPOS[chave] = (body, etag)
    return body, etag


def _gap_segundos(anterior: str, atual: str) -> float:
    def ler(texto: str) -> datetime | None:
        bruto = (texto or "").strip().replace("Z", "")
        if not bruto:
            return None
        try:
            return datetime.fromisoformat(bruto[:19])
        except ValueError:
            return None

    a = ler(anterior)
    b = ler(atual)
    if a is None or b is None:
        return 0.0
    return abs((b - a).total_seconds())


def _simplificar(coords: list[tuple[float, float]]) -> list[list[float]]:
    """Mantém cada pulso persistido. Sem atalho entre o primeiro e o último."""
    pontos: list[list[float]] = []
    for lat, lon in coords:
        par = [round(float(lat), 5), round(float(lon), 5)]
        if not pontos or par != pontos[-1]:
            pontos.append(par)
    return pontos


_SETORES_TURNO: dict[tuple[str, str], list[dict[str, Any]]] = {}
_POLIGONOS_PRONTOS: dict[tuple[tuple[str, int], ...], list[tuple[str, Any, float]]] = {}


def _preparar_setores(setores: list[dict[str, Any]]) -> list[tuple[str, Any, float]]:
    from shapely.geometry import Polygon
    from shapely.prepared import prep

    chave = tuple((str(setor.get("codigo") or ""), len(setor.get("aneis") or [])) for setor in setores or [])
    pronto = _POLIGONOS_PRONTOS.get(chave)
    if pronto is not None:
        return pronto
    itens: list[tuple[str, Any, float]] = []
    for setor in setores or []:
        codigo = str(setor.get("codigo") or "")
        for anel in setor.get("aneis") or []:
            coords = [(float(par[1]), float(par[0])) for par in anel if isinstance(par, (list, tuple)) and len(par) >= 2]
            if len(coords) < 4:
                continue
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            itens.append((codigo, prep(poly), float(poly.area)))
    _POLIGONOS_PRONTOS[chave] = itens
    return itens


def codigo_que_contem(lat: float, lon: float, setores: list[dict[str, Any]]) -> str | None:
    """Setor oficial que cobre o ponto. O menor polígono ganha se houver sobreposição."""
    from shapely.geometry import Point

    ponto = Point(float(lon), float(lat))
    melhor = None
    area = None
    for codigo, pronto, tamanho in _preparar_setores(setores):
        if not pronto.covers(ponto):
            continue
        if area is None or tamanho < area:
            area = tamanho
            melhor = codigo
    return melhor or None


def _setores_do_turno(dia: date, turno: str) -> list[dict[str, Any]]:
    from .mapa_oficial import _freq_do_dia
    from .unificado import _casa_turno

    chave = (dia.isoformat(), turno)
    guardado = _SETORES_TURNO.get(chave)
    if guardado is not None:
        return guardado
    saida = [
        setor
        for setor in _poligonos()
        if _freq_do_dia(str(setor.get("frequencia") or ""), dia) and _casa_turno(str(setor.get("frequencia") or ""), turno)
    ]
    _SETORES_TURNO[chave] = saida
    return saida


def _partir_trilha(
    coords: list[tuple[float, float]],
    setores: list[dict[str, Any]],
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    dentro: list[list[tuple[float, float]]] = []
    fora: list[list[tuple[float, float]]] = []
    if not setores:
        return [coords], []
    atual = ""
    balde: list[tuple[float, float]] = []
    for lat, lon in coords:
        lugar = "dentro" if codigo_que_contem(lat, lon, setores) else "fora"
        if atual and lugar != atual:
            balde.append((lat, lon))
            (dentro if atual == "dentro" else fora).append(balde)
            balde = [(lat, lon)]
        else:
            balde.append((lat, lon))
        atual = lugar
    if len(balde) >= 2:
        (dentro if atual == "dentro" else fora).append(balde)
    return dentro, fora


def _metros(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _linhas_visuais(brutos: list[tuple[str, float, float]]) -> list[list[list[float]]]:
    grupos: list[list[tuple[float, float]]] = []
    atual: list[tuple[float, float]] = []
    anterior = ""
    anterior_xy: tuple[float, float] | None = None
    for ts, lat, lon in brutos:
        quebra = False
        if atual and anterior and _gap_segundos(anterior, ts) > 600:
            quebra = True
        if atual and anterior_xy is not None and _metros(anterior_xy, (lat, lon)) > _GAP_M:
            quebra = True
        if quebra:
            grupos.append(atual)
            atual = []
        atual.append((lat, lon))
        anterior = ts
        anterior_xy = (lat, lon)
    if atual:
        grupos.append(atual)
    return [_simplificar(grupo) for grupo in grupos if len(grupo) >= 2]


def _poligonos_oficiais(areas: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """Anel oficial, com o mesmo reparo de preparar_areas. Sem buffer de aproximação."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.prepared import prep

    saida: list[tuple[Any, Any]] = []
    for area in areas or []:
        polys = []
        for anel in area.get("poligonos") or []:
            coords = []
            for par in anel:
                if not isinstance(par, (list, tuple)) or len(par) < 2:
                    continue
                try:
                    lat = float(par[0])
                    lon = float(par[1])
                except (TypeError, ValueError):
                    continue
                coords.append((lon, lat))
            if len(coords) < 4:
                continue
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                polys.append(poly)
        if not polys:
            continue
        geom = unary_union(polys) if len(polys) > 1 else polys[0]
        if geom.is_empty:
            continue
        saida.append((geom, prep(geom)))
    return saida


def _iter_linhas(geom) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo == "LineString":
        return [geom] if len(geom.coords) >= 2 else []
    if tipo == "MultiLineString":
        return [parte for parte in geom.geoms if len(parte.coords) >= 2]
    if tipo == "GeometryCollection":
        linhas: list[Any] = []
        for parte in geom.geoms:
            linhas.extend(_iter_linhas(parte))
        return linhas
    return []


def _coord_visual(lat: float, lon: float) -> list[float]:
    return [round(float(lat), 6), round(float(lon), 6)]


def trechos_internos_dificil(
    pontos: list[tuple[str, float, float]],
    areas: list[dict[str, Any]],
) -> list[list[list[float]]]:
    """Trechos só para desenho, dentro dos polígonos já escolhidos.

    A quebra de 10 min ou 480 m é a de _linhas_visuais. Cada área é
    intersectada à parte, e o vértice externo que a divisão por covers
    manteria é cortado na borda. Não grava ponto e não liga áreas distintas.
    """
    from shapely.geometry import LineString

    geoms = _poligonos_oficiais(areas)
    if not geoms:
        return []
    saida: list[list[list[float]]] = []
    for linha in _linhas_visuais(pontos):
        coords = [(float(par[0]), float(par[1])) for par in linha if len(par) >= 2]
        if len(coords) < 2:
            continue
        trilha = LineString([(lon, lat) for lat, lon in coords])
        if trilha.is_empty:
            continue
        for geom, pronto in geoms:
            for parte in _iter_linhas(trilha.intersection(geom)):
                meio = parte.interpolate(0.5, normalized=True)
                if not pronto.covers(meio):
                    continue
                desenho: list[list[float]] = []
                for lon, lat in parte.coords:
                    par = _coord_visual(lat, lon)
                    if not desenho or par != desenho[-1]:
                        desenho.append(par)
                if len(desenho) >= 2:
                    saida.append(desenho)
    return saida


def trilhas(
    data: str | None,
    turno: str | None,
    placa: str | None = None,
    rota: str | None = None,
) -> dict[str, Any]:
    dia, pedido = _dia_turno(data, turno)
    filtro_placa = str(placa or "").strip().upper()
    filtro_rota = str(rota or "").strip().upper()
    con = conectar()
    try:
        rows = con.execute(
            """
            SELECT placa, ts, lat, lon FROM ponto
            WHERE data = ? AND turno = ?
            ORDER BY placa, ts
            """,
            (dia.isoformat(), pedido),
        ).fetchall()
    finally:
        con.close()
    por_placa: dict[str, list[tuple[str, float, float]]] = {}
    cursor = ""
    for row in rows:
        placa = str(row["placa"] or "")
        ts = str(row["ts"] or "")
        por_placa.setdefault(placa, []).append((ts, float(row["lat"]), float(row["lon"])))
        if ts > cursor:
            cursor = ts
    veiculos = []
    visuais = 0
    setores = _setores_do_turno(dia, pedido)
    from .dificil_acesso import eh_basculante

    for placa, brutos in por_placa.items():
        if eh_basculante(placa):
            continue
        linhas = _linhas_visuais(brutos)
        fora = []
        if setores and linhas:
            dentro = []
            for linha in linhas:
                coords = [(float(par[0]), float(par[1])) for par in linha]
                parte_dentro, parte_fora = _partir_trilha(coords, setores)
                dentro.extend(parte_dentro)
                fora.extend(parte_fora)
            linhas = [_simplificar(parte) for parte in dentro if len(parte) >= 2]
            fora = [_simplificar(parte) for parte in fora if len(parte) >= 2]
        visuais += sum(len(linha) for linha in linhas) + sum(len(linha) for linha in fora)
        if linhas or fora:
            veiculos.append({"placa": placa, "linhas": linhas, "deslocamento": fora})
    if filtro_placa:
        veiculos = [item for item in veiculos if str(item.get("placa") or "").upper() == filtro_placa]
    if filtro_rota:
        permitidas = _placas_executoras(dia.isoformat(), pedido, filtro_rota)
        if permitidas is not None:
            veiculos = [item for item in veiculos if str(item.get("placa") or "").upper() in permitidas]
    return {
        "data": dia.isoformat(),
        "turno": pedido,
        "cursor": cursor,
        "pontos_originais": len(rows),
        "pontos_visuais": visuais,
        "veiculos": veiculos,
    }


def dificil_acesso(data: str | None, turno: str | None) -> dict[str, Any]:
    """Camada dos dois basculantes. Lê o ponto já gravado e não grava rota."""
    from .dificil_acesso import (
        BASCULANTES,
        anotar_programacao,
        codigo_da_frota,
        eventos_nas_areas,
        montar_trajetoria,
        preparar_areas,
        rotulo_operacional,
    )

    dia, pedido = _dia_turno(data, turno)
    inicio = time.perf_counter()
    con = conectar()
    try:
        placas = _placas_basculante(con, dia.isoformat(), pedido)
        bruto: dict[str, list[tuple[str, float, float]]] = {item["codigo"]: [] for item in BASCULANTES}
        if placas:
            marcas = list(placas)
            consulta = ",".join("?" for _ in marcas)
            rows = con.execute(
                f"""
                SELECT placa, ts, lat, lon FROM ponto
                WHERE data = ? AND turno = ? AND placa IN ({consulta})
                ORDER BY placa, ts
                """,
                [dia.isoformat(), pedido, *marcas],
            ).fetchall()
        else:
            rows = []
    finally:
        con.close()
    for row in rows:
        codigo = placas.get(str(row["placa"] or ""))
        if not codigo:
            codigo = codigo_da_frota(str(row["placa"] or ""))
        if not codigo:
            continue
        bruto[codigo].append((str(row["ts"] or ""), float(row["lat"]), float(row["lon"])))
    setores = _setores_do_turno(dia, pedido)

    def contem(lat: float, lon: float) -> bool:
        if not setores:
            return False
        return codigo_que_contem(lat, lon, setores) is not None

    areas = areas_dificil()
    areas_marcadas = anotar_programacao(areas, dia, pedido)
    programadas = [area for area in areas_marcadas if area.get("programada")]
    from .live_sync import agora_brt
    from .live_turno import situacao_pelo_relogio

    momento = agora_brt()
    vivo = situacao_pelo_relogio(dia, pedido, momento) in {"ABERTO", "FINALIZANDO"}
    prontos = preparar_areas(areas)
    veiculos = []
    eventos: list[dict[str, Any]] = []
    pulsos = fora = 0
    trechos_dificil = trechos_desloc = trechos_rota = 0
    for item in BASCULANTES:
        serie = bruto.get(item["codigo"]) or []
        desenho = montar_trajetoria(serie, contem)
        presenca = eventos_nas_areas(
            serie,
            prontos,
            codigo=item["codigo"],
            placa=item["placa"],
            data=dia.isoformat(),
            turno=pedido,
        )
        texto = rotulo_operacional(
            desenho["pulsos"],
            desenho["ultimo_gps"],
            desenho["situacao"],
            presenca["area"],
            vivo=vivo,
            momento=momento,
        )
        pulsos += desenho["pulsos"]
        fora += desenho["pontos_fora"]
        trechos_rota += len(desenho["na_rota"])
        trechos_desloc += len(desenho["deslocamento"])
        trechos_dificil += len(desenho["dificil"])
        eventos.extend(presenca["eventos"])
        veiculos.append(
            {
                "codigo": item["codigo"],
                "placa": item["placa"],
                "situacao": desenho["situacao"],
                "rotulo": texto["rotulo"],
                "sinal": texto["sinal"],
                "aviso": texto["aviso"],
                "ultimo_gps": desenho["ultimo_gps"],
                "lat": desenho["lat"],
                "lon": desenho["lon"],
                "pulsos": desenho["pulsos"],
                "pontos_fora": desenho["pontos_fora"],
                "trajeto": desenho["trajeto"],
                "trechos_internos": trechos_internos_dificil(serie, programadas),
                "na_rota": desenho["na_rota"],
                "deslocamento": desenho["deslocamento"],
                "dificil": desenho["dificil"],
                "isolados": desenho["isolados"],
                "poligonos": desenho["poligonos"],
                "area": presenca["area"],
                "area_id": presenca["area_id"],
                "dentro": bool(presenca["area_id"]),
                "eventos": presenca["eventos"],
                "permanencias": presenca["permanencias"],
            }
        )
    return {
        "data": dia.isoformat(),
        "turno": pedido,
        "fonte": "live_sync",
        "camada": "Áreas de difícil acesso",
        "areas": areas_marcadas,
        "veiculos": veiculos,
        "eventos": eventos,
        "resumo": {
            "pulsos": pulsos,
            "pontos_fora": fora,
            "trechos_na_rota": trechos_rota,
            "trechos_deslocamento": trechos_desloc,
            "trechos_dificil": trechos_dificil,
            "areas": len(areas),
            "eventos": len(eventos),
            "ms": round((time.perf_counter() - inicio) * 1000, 1),
        },
    }


def _placas_basculante(con, data: str, turno: str) -> dict[str, str]:
    """Grafias já usadas no banco. A consulta de ponto fica na chave, sem varrer o turno."""
    from .dificil_acesso import BASCULANTES, codigo_da_frota

    achadas: dict[str, str] = {}
    for item in BASCULANTES:
        cru = item["placa"]
        achadas[cru] = item["codigo"]
        if len(cru) > 3:
            achadas[f"{cru[:3]}-{cru[3:]}"] = item["codigo"]
    for row in con.execute(
        "SELECT placa, veiculo FROM placa WHERE data = ? AND turno = ?",
        (data, turno),
    ):
        codigo = codigo_da_frota(str(row["placa"] or ""), str(row["veiculo"] or ""))
        if codigo:
            achadas[str(row["placa"])] = codigo
    return achadas


def atualizacoes(data: str | None, turno: str | None, since: str | None) -> dict[str, Any]:
    dia, pedido = _dia_turno(data, turno)
    cursor_cliente = (since or "").strip()
    if not cursor_cliente:
        raise ValueError("Informe since com o cursor que o navegador já recebeu.")
    con = conectar()
    try:
        rows = con.execute(
            """
            SELECT placa, ts, lat, lon FROM ponto
            WHERE data = ? AND turno = ? AND ts > ?
            ORDER BY ts
            LIMIT 2000
            """,
            (dia.isoformat(), pedido, cursor_cliente),
        ).fetchall()
    finally:
        con.close()
    from .dificil_acesso import eh_basculante

    setores = _setores_do_turno(dia, pedido)
    pontos = []
    cursor = cursor_cliente
    for row in rows:
        ts = str(row["ts"] or "")
        if ts > cursor:
            cursor = ts
        placa = str(row["placa"] or "")
        if eh_basculante(placa):
            continue
        lat = float(row["lat"])
        lon = float(row["lon"])
        pontos.append(
            {
                "placa": placa,
                "ts": ts,
                "lat": lat,
                "lon": lon,
                "deslocamento": bool(setores) and codigo_que_contem(lat, lon, setores) is None,
            }
        )
    return {
        "data": dia.isoformat(),
        "turno": pedido,
        "since": cursor_cliente,
        "cursor": cursor,
        "pontos": pontos,
    }


def _linhas_geojson(bruto: str, nome: str) -> dict[str, Any]:
    try:
        linhas = json.loads(bruto or "[]")
    except json.JSONDecodeError:
        linhas = []
    features = []
    if isinstance(linhas, list):
        for linha in linhas:
            coords = []
            if not isinstance(linha, list):
                continue
            for par in linha:
                if isinstance(par, (list, tuple)) and len(par) >= 2:
                    coords.append([float(par[1]), float(par[0])])
            if len(coords) >= 2:
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"camada": nome},
                        "geometry": {"type": "LineString", "coordinates": coords},
                    }
                )
    return {"type": "FeatureCollection", "features": features}


def _marco(lat: Any, lon: Any, hora: Any) -> dict[str, Any] | None:
    if lat is None or lon is None:
        return None
    try:
        return {"lat": float(lat), "lon": float(lon), "hora": str(hora or "")}
    except (TypeError, ValueError):
        return None


def _placas_executoras(data: str, turno: str, codigo: str) -> set[str] | None:
    """None quando a rota não está gravada. Conjunto vazio não filtra por engano o mapa geral."""
    from .cobertura import ler_executores_json

    con = conectar()
    try:
        row = con.execute(
            "SELECT placa, executores_json FROM rota WHERE data = ? AND turno = ? AND codigo = ?",
            (data, turno, codigo),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return set()
    bruto = row["executores_json"] if "executores_json" in row.keys() else ""
    info = ler_executores_json(bruto)
    placas = {str(item.get("placa") or "").upper() for item in info["executores"] if item.get("placa")}
    if placas:
        return placas
    legado = str(row["placa"] or "").strip().upper()
    return {legado} if legado else set()


def _trajetorias(data: str, turno: str, codigo: str, executores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traçado de exibição. A cobertura já está nos trechos oficiais gravados."""
    placas = [str(item.get("placa") or "").strip() for item in executores if str(item.get("placa") or "").strip()]
    if not placas:
        return []
    con = conectar()
    try:
        marcas = ",".join("?" for _ in placas)
        rows = con.execute(
            f"""
            SELECT placa, ts, lat, lon FROM ponto
            WHERE data = ? AND turno = ? AND placa IN ({marcas})
            ORDER BY placa, ts
            """,
            [data, turno, *placas],
        ).fetchall()
    finally:
        con.close()
    por_placa: dict[str, list[tuple[str, float, float]]] = {}
    for row in rows:
        por_placa.setdefault(str(row["placa"] or ""), []).append((str(row["ts"] or ""), float(row["lat"]), float(row["lon"])))
    aneis = []
    for setor in _poligonos():
        if str(setor.get("codigo") or "").upper() == codigo:
            aneis = setor.get("aneis") or []
            break
    referencia = [{"codigo": codigo, "aneis": aneis}] if aneis else []
    saida = []
    for item in executores:
        placa = str(item.get("placa") or "")
        brutos = por_placa.get(placa) or []
        serie = brutos
        if referencia:
            serie = [ponto for ponto in brutos if codigo_que_contem(ponto[1], ponto[2], referencia)]
        linhas = _linhas_visuais(serie) if serie else []
        if not linhas:
            linhas = _linhas_visuais(brutos)
        copia = dict(item)
        copia["linhas"] = linhas
        saida.append(copia)
    return saida


def detalhe_rota(data: str | None, turno: str | None, rota: str | None) -> dict[str, Any]:
    dia, pedido = _dia_turno(data, turno)
    codigo = str(rota or "").strip().upper()
    if not codigo:
        raise ValueError("Informe a rota.")
    con = conectar()
    try:
        row = con.execute(
            """
            SELECT codigo, placa, percentual, entrada_lat, entrada_lon, entrada_hora,
                   saida_lat, saida_lon, saida_hora, trechos_feitos, trechos_faltando,
                   executores_json
            FROM rota
            WHERE data = ? AND turno = ? AND codigo = ?
            """,
            (dia.isoformat(), pedido, codigo),
        ).fetchone()
    finally:
        con.close()
    from .cobertura import ler_executores_json

    vazio = {
        "ok": False,
        "data": dia.isoformat(),
        "turno": pedido,
        "rota": codigo,
        "percentual": 0,
        "placa_principal": "",
        "apoio": "",
        "cobertura_sobreposta": "",
        "sobrepostos": [],
        "resumo_veiculos": "",
        "executores": [],
        "passagens": [],
        "nao_confirmados": [],
        "veiculos": [],
        "entrada": None,
        "saida": None,
        "trechos_feitos": {"type": "FeatureCollection", "features": []},
        "trechos_faltando": {"type": "FeatureCollection", "features": []},
    }
    if row is None:
        return vazio
    info = ler_executores_json(row["executores_json"] if "executores_json" in row.keys() else "")
    veiculos = _trajetorias(dia.isoformat(), pedido, codigo, info["executores"])
    return {
        "ok": True,
        "data": dia.isoformat(),
        "turno": pedido,
        "rota": codigo,
        "percentual": row["percentual"],
        "placa": info["placa_principal"] or row["placa"],
        "placa_principal": info["placa_principal"],
        "placas_apoio": info["placas_apoio"],
        "apoio": info["apoio"],
        "sobrepostos": info["sobrepostos"],
        "cobertura_sobreposta": info["cobertura_sobreposta"],
        "resumo_veiculos": info["resumo_veiculos"],
        "executores": info["executores"],
        "passagens": info["passagens"],
        "nao_confirmados": info["nao_confirmados"],
        "auditoria": info["auditoria"],
        "veiculos": veiculos,
        "entrada": _marco(row["entrada_lat"], row["entrada_lon"], row["entrada_hora"]),
        "saida": _marco(row["saida_lat"], row["saida_lon"], row["saida_hora"]),
        "trechos_feitos": _linhas_geojson(str(row["trechos_feitos"] or ""), "feitos"),
        "trechos_faltando": _linhas_geojson(str(row["trechos_faltando"] or ""), "faltando"),
    }


def compactar_trechos(linhas: list[Any] | None) -> list[list[list[float]]]:
    """Cópia visual já produzida pela cobertura. Não altera o GPS nem o percentual."""
    saida = []
    for linha in linhas or []:
        pontos = []
        if not isinstance(linha, list):
            continue
        for par in linha:
            if not isinstance(par, (list, tuple)) or len(par) < 2:
                continue
            try:
                pontos.append([round(float(par[0]), 5), round(float(par[1]), 5)])
            except (TypeError, ValueError):
                continue
        if len(pontos) >= 2:
            saida.append(pontos)
    return saida
