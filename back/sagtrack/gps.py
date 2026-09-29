from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from .config import normalizar_hora

Ponto = tuple[float, float]

LAT_KEYS = {
    "lat",
    "latitude",
    "latitud",
    "y",
    "coordy",
    "coord_y",
    "posicaoy",
    "posicaolatitude",
    "rasevelatitude",
    "rasevelat",
    "rasreflatitude",
    "raslat",
}
LON_KEYS = {
    "lng",
    "lon",
    "long",
    "longitude",
    "longitud",
    "x",
    "coordx",
    "coord_x",
    "posicaox",
    "posicaolongitude",
    "rasevelongitude",
    "rasevelng",
    "rasreflongitude",
    "raslng",
    "raslon",
}
TIME_KEYS = {
    "data",
    "hora",
    "datetime",
    "timestamp",
    "datahora",
    "data_hora",
    "dhposicao",
    "dh_posicao",
    "datacoleta",
    "dataevento",
    "dtposicao",
    "dtgps",
    "dtservidor",
    "dtgpsformatado",
    "rasevedataservidor",
    "rasevedatagps",
    "rasevedataenviado",
    "rasdatahora",
}
SPEED_KEYS = {"velocidade", "speed", "vel", "velocity", "kmh", "km_h"}
ADDR_KEYS = {"endereco", "endereço", "address", "logradouro", "rua", "local"}
VEIC_KEYS = {
    "veiculo",
    "veículo",
    "prefixo",
    "placa",
    "frota",
    "identificador",
    "codigo",
    "codigoveiculo",
    "idveiculo",
    "nome",
    "descricao",
    "rasveiplaca",
    "rasveiveiculo",
}


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 180:
            number = number / 1_000_000
    else:
        texto = str(value).strip().replace(",", ".")
        if not re.fullmatch(r"-?\d+(\.\d+)?", texto):
            return None
        number = float(texto)
        if abs(number) > 180:
            number = number / 1_000_000
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _is_lat(value: float) -> bool:
    return -90.0 <= value <= 90.0 and abs(value) > 0.5


def _is_lon(value: float) -> bool:
    return -180.0 <= value <= 180.0 and abs(value) > 0.5


def _pair_from_list(item: Any) -> tuple[float, float] | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    a = _as_float(item[0])
    b = _as_float(item[1])
    if a is None or b is None:
        return None
    if _is_lat(a) and _is_lon(b):
        return a, b
    if _is_lat(b) and _is_lon(a):
        return b, a
    return None


def _first_key(obj: dict[str, Any], candidates: set[str]) -> Any:
    for key, value in obj.items():
        if _norm(str(key)) in candidates:
            return value
    return None


def extract_points(payload: Any, veiculo: str = "", fonte: str = "") -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []
    _walk(payload, pontos, veiculo=veiculo, fonte=fonte)
    return _dedupe(pontos)


def _walk(node: Any, acc: list[dict[str, Any]], veiculo: str, fonte: str) -> None:
    if isinstance(node, dict):
        herdado = str(_first_key(node, VEIC_KEYS) or veiculo or "").strip()
        point = _point_from_dict(node, herdado, fonte)
        if point:
            acc.append(point)
        for value in node.values():
            _walk(value, acc, herdado, fonte)
        return

    if isinstance(node, list):
        pair = _pair_from_list(node)
        if pair and all(not isinstance(item, (dict, list)) for item in node[:2]):
            acc.append(
                {
                    "veiculo": veiculo,
                    "timestamp": "",
                    "latitude": pair[0],
                    "longitude": pair[1],
                    "velocidade": "",
                    "endereco": "",
                    "fonte": fonte,
                }
            )
            return
        for item in node:
            _walk(item, acc, veiculo, fonte)


def _point_from_dict(obj: dict[str, Any], veiculo: str, fonte: str) -> dict[str, Any] | None:
    lat = _as_float(_first_key(obj, LAT_KEYS))
    lon = _as_float(_first_key(obj, LON_KEYS))
    if lat is None or lon is None:
        coords = obj.get("coordinates") or obj.get("coordenadas")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            pair = _pair_from_list(coords)
            if pair:
                lat, lon = pair
    if lat is None or lon is None or not _is_lat(lat) or not _is_lon(lon):
        return None

    veiculo_local = _first_key(obj, VEIC_KEYS)
    return {
        "veiculo": str(veiculo_local or veiculo or "").strip(),
        "timestamp": str(_first_key(obj, TIME_KEYS) or "").strip(),
        "latitude": lat,
        "longitude": lon,
        "velocidade": _first_key(obj, SPEED_KEYS) or "",
        "endereco": str(_first_key(obj, ADDR_KEYS) or "").strip(),
        "fonte": fonte,
    }


def _dedupe(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for ponto in pontos:
        key = (
            ponto.get("veiculo"),
            ponto.get("timestamp"),
            round(float(ponto["latitude"]), 6),
            round(float(ponto["longitude"]), 6),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(ponto)
    return unique


def haversine_m(a: Ponto, b: Ponto) -> float:
    r = 6371000
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _xy(ponto: Ponto, origem: Ponto) -> Ponto:
    lat0 = math.radians(origem[0])
    return ((ponto[1] - origem[1]) * 111_320 * math.cos(lat0), (ponto[0] - origem[0]) * 110_540)


def _dist_ponto_seg(p: Ponto, a: Ponto, b: Ponto) -> float:
    if a == b:
        return haversine_m(p, a)
    ax, ay = 0.0, 0.0
    bx, by = _xy(b, a)
    px, py = _xy(p, a)
    ab2 = bx * bx + by * by
    t = 0.0 if ab2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / ab2))
    qx, qy = ax + t * bx, ay + t * by
    return math.hypot(px - qx, py - qy)


def _rdp(pontos: list[Ponto], tolerancia_m: float) -> list[Ponto]:
    if len(pontos) < 3:
        return pontos
    inicio, fim = pontos[0], pontos[-1]
    idx = 0
    dist_max = -1.0
    for i in range(1, len(pontos) - 1):
        dist = _dist_ponto_seg(pontos[i], inicio, fim)
        if dist > dist_max:
            idx = i
            dist_max = dist
    if dist_max > tolerancia_m:
        esquerda = _rdp(pontos[: idx + 1], tolerancia_m)
        direita = _rdp(pontos[idx:], tolerancia_m)
        return esquerda[:-1] + direita
    return [inicio, fim]


def _proximo_na_via(ponto: Ponto, via: list[list[float]]) -> tuple[float, float, Ponto] | None:
    """Distância em metros, posição ao longo da via e o ponto projetado."""
    if len(via) < 2:
        return None
    melhor: tuple[float, float, Ponto] | None = None
    percorrido = 0.0
    for indice in range(len(via) - 1):
        a = (float(via[indice][0]), float(via[indice][1]))
        b = (float(via[indice + 1][0]), float(via[indice + 1][1]))
        comp = haversine_m(a, b)
        if comp < 0.5:
            continue
        ax, ay = _xy(a, a)
        bx, by = _xy(b, a)
        px, py = _xy(ponto, a)
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        t = 0.0 if denom <= 0 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / denom))
        proj = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        dist = haversine_m(ponto, proj)
        along = percorrido + comp * t
        if melhor is None or dist < melhor[0]:
            melhor = (dist, along, proj)
        percorrido += comp
    return melhor


def _fatia_via(via: list[list[float]], inicio_m: float, fim_m: float) -> list[Ponto]:
    if fim_m < inicio_m:
        inicio_m, fim_m = fim_m, inicio_m
        inverter = True
    else:
        inverter = False
    saida: list[Ponto] = []
    percorrido = 0.0
    for indice in range(len(via) - 1):
        a = (float(via[indice][0]), float(via[indice][1]))
        b = (float(via[indice + 1][0]), float(via[indice + 1][1]))
        comp = haversine_m(a, b)
        if comp < 0.5:
            continue
        prox = percorrido + comp
        if prox < inicio_m:
            percorrido = prox
            continue
        if not saida:
            t = 0.0 if comp <= 0 else max(0.0, min(1.0, (inicio_m - percorrido) / comp))
            saida.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        if prox <= fim_m:
            saida.append(b)
        else:
            t = 0.0 if comp <= 0 else max(0.0, min(1.0, (fim_m - percorrido) / comp))
            saida.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
        percorrido = prox
    if inverter:
        saida.reverse()
    return saida


def _no_via(ponto: Ponto) -> tuple[int, int]:
    return (int(round(float(ponto[0]) * 1_000_000)), int(round(float(ponto[1]) * 1_000_000)))


def _alongar(destino: list[Ponto], extra: list[Ponto]) -> None:
    if not extra:
        return
    if destino and haversine_m(destino[-1], extra[0]) < 4:
        destino.extend(extra[1:])
    else:
        destino.extend(extra)


_REDE_CACHE: dict[tuple, list[list[list[float]]]] = {}


def _rede_viaria(ruas: list[list[list[float]]]) -> list[list[list[float]]]:
    """Une as ruas oficiais nos cruzamentos para o trajeto não quebrar no meio da quadra."""
    from shapely.geometry import LineString
    from shapely.ops import linemerge, snap, unary_union

    if not ruas:
        return []
    chave = (
        len(ruas),
        round(float(ruas[0][0][0]), 5),
        round(float(ruas[0][0][1]), 5),
        round(float(ruas[-1][-1][0]), 5),
        round(float(ruas[-1][-1][1]), 5),
    )
    pronta = _REDE_CACHE.get(chave)
    if pronta is not None:
        return pronta
    geoms = []
    for via in ruas:
        coords = [(float(p[1]), float(p[0])) for p in via if len(p) >= 2]
        if len(coords) < 2 or coords[0] == coords[-1]:
            continue
        try:
            linha = LineString(coords)
        except (TypeError, ValueError):
            continue
        if not linha.is_empty and linha.length > 0:
            geoms.append(linha)
    if not geoms:
        return []
    base = unary_union(geoms)
    grudado = unary_union([snap(geom, base, 0.0002) for geom in geoms])
    if grudado.geom_type == "MultiLineString":
        try:
            fundido = linemerge(grudado)
        except ValueError:
            fundido = grudado
    else:
        fundido = grudado
    partes = [fundido] if fundido.geom_type == "LineString" else list(getattr(fundido, "geoms", []) or [])
    saida: list[list[list[float]]] = []
    for geom in partes:
        linhas = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []) or [])
        for linha in linhas:
            if linha.geom_type != "LineString" or len(linha.coords) < 2:
                continue
            saida.append([[lat, lon] for lon, lat in linha.coords])
    _REDE_CACHE[chave] = saida
    return saida


def _ligar_na_rede(
    ruas: list[list[list[float]]],
    comps: list[float],
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], int, bool]]],
    a_idx: int,
    a_along: float,
    b_idx: int,
    b_along: float,
    limite: float,
) -> list[Ponto] | None:
    """Caminho pela rua oficial entre dois pulsos, se a distância cabe no tempo."""
    import heapq

    if limite < 8:
        return None
    if a_idx == b_idx:
        if abs(b_along - a_along) > limite:
            return None
        fatia = _fatia_via(ruas[a_idx], a_along, b_along)
        return fatia if len(fatia) >= 2 else None

    via_a = ruas[a_idx]
    via_b = ruas[b_idx]
    comp_a = comps[a_idx]
    comp_b = comps[b_idx]
    no_a0, no_a1 = _no_via((via_a[0][0], via_a[0][1])), _no_via((via_a[-1][0], via_a[-1][1]))
    no_b0, no_b1 = _no_via((via_b[0][0], via_b[0][1])), _no_via((via_b[-1][0], via_b[-1][1]))
    inicio: dict[tuple[int, int], list[Ponto]] = {}
    pai: dict[tuple[int, int], tuple[tuple[int, int], int, bool]] = {}
    melhor: dict[tuple[int, int], float] = {}
    heap: list[tuple[float, tuple[int, int]]] = []

    def sair(custo: float, no: tuple[int, int], geom: list[Ponto]) -> None:
        if custo > limite or custo >= melhor.get(no, limite + 1):
            return
        melhor[no] = custo
        inicio[no] = geom
        heapq.heappush(heap, (custo, no))

    sair(a_along, no_a0, _fatia_via(via_a, a_along, 0))
    sair(max(0.0, comp_a - a_along), no_a1, _fatia_via(via_a, a_along, comp_a))

    def geometria_ate(no: tuple[int, int]) -> list[Ponto] | None:
        passos: list[tuple[int, bool]] = []
        atual = no
        vistos: set[tuple[int, int]] = set()
        while atual not in inicio and atual in pai and atual not in vistos:
            vistos.add(atual)
            prev, edge, forward = pai[atual]
            passos.append((edge, forward))
            atual = prev
        if atual not in inicio:
            return None
        geom = list(inicio[atual])
        for edge, forward in reversed(passos):
            via = ruas[edge] if forward else list(reversed(ruas[edge]))
            _alongar(geom, [(float(p[0]), float(p[1])) for p in via])
        return geom

    melhor_geom: list[Ponto] | None = None
    melhor_total = limite + 1
    assentados: set[tuple[int, int]] = set()
    while heap:
        custo, no = heapq.heappop(heap)
        if no in assentados or custo > melhor.get(no, custo):
            continue
        assentados.add(no)
        if custo >= melhor_total:
            continue
        geom = geometria_ate(no)
        if geom is None:
            continue
        chegadas = []
        if no == no_b0:
            chegadas.append((b_along, _fatia_via(via_b, 0, b_along)))
        if no == no_b1:
            chegadas.append((max(0.0, comp_b - b_along), _fatia_via(via_b, comp_b, b_along)))
        for extra, fatia in chegadas:
            total = custo + extra
            if total <= limite and total < melhor_total and len(fatia) >= 1:
                copia = list(geom)
                _alongar(copia, fatia)
                if len(copia) >= 2:
                    melhor_total = total
                    melhor_geom = copia
        for outro, edge, forward in adj.get(no, []):
            if edge in {a_idx, b_idx}:
                continue
            custo2 = custo + comps[edge]
            if custo2 > limite or custo2 >= melhor_total or custo2 >= melhor.get(outro, limite + 1):
                continue
            melhor[outro] = custo2
            pai[outro] = (no, edge, forward)
            inicio.pop(outro, None)
            heapq.heappush(heap, (custo2, outro))
    return melhor_geom


def trajetoria_uniforme(
    pontos: list[dict[str, Any]] | None,
    vias: list[list[list[float]]] | None = None,
    *,
    dia: date | None = None,
    salto_max_m: float = 120.0,
    cola_m: float = 12.0,
    rua_m: float = 40.0,
) -> list[list[list[float]]]:
    """Liga os pulsos pela hora e, quando o pulso está na via oficial, segue o meio da rua."""
    marcas: list[tuple[datetime, Ponto]] = []
    referencia = dia or date.today()
    for ponto in pontos or []:
        try:
            coord = (float(ponto["latitude"]), float(ponto["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        momento = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), referencia)
        marcas.append((momento or datetime.min, coord))
    marcas.sort(key=lambda item: item[0])
    seguidos: list[tuple[datetime, Ponto]] = []
    for momento, coord in marcas:
        if seguidos and haversine_m(seguidos[-1][1], coord) < cola_m:
            continue
        seguidos.append((momento, coord))
    if len(seguidos) < 2:
        return []

    ruas = [via for via in (vias or []) if via and len(via) >= 2]
    if not ruas:
        return _quebrar_salto([coord for _momento, coord in seguidos], salto_max_m)

    ruas = _rede_viaria(ruas) or ruas
    caixas = []
    comps: list[float] = []
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], int, bool]]] = {}
    for via in ruas:
        lats = [float(p[0]) for p in via]
        lons = [float(p[1]) for p in via]
        caixas.append((min(lats) - 0.0005, max(lats) + 0.0005, min(lons) - 0.0005, max(lons) + 0.0005))
        comp = 0.0
        for i in range(len(via) - 1):
            comp += haversine_m((float(via[i][0]), float(via[i][1])), (float(via[i + 1][0]), float(via[i + 1][1])))
        comps.append(comp)
        if comp < 1:
            continue
        no0 = _no_via((float(via[0][0]), float(via[0][1])))
        no1 = _no_via((float(via[-1][0]), float(via[-1][1])))
        indice = len(comps) - 1
        adj.setdefault(no0, []).append((no1, indice, True))
        adj.setdefault(no1, []).append((no0, indice, False))

    grade: dict[tuple[int, int], list[int]] = {}
    for indice, via in enumerate(ruas):
        vistos = set()
        for ponto in via:
            celula = (int(float(ponto[0]) * 900), int(float(ponto[1]) * 900))
            if celula in vistos:
                continue
            vistos.add(celula)
            grade.setdefault(celula, []).append(indice)

    def projetar_rede(coord: Ponto, preferida: int | None) -> tuple[int, float, Ponto] | None:
        celula = (int(coord[0] * 900), int(coord[1] * 900))
        perto: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                perto.extend(grade.get((celula[0] + dx, celula[1] + dy), ()))
        if preferida is not None:
            perto.append(preferida)
        melhor_local: tuple[float, int, float, Ponto] | None = None
        for indice in dict.fromkeys(perto):
            via = ruas[indice]
            sul, norte, oeste, leste = caixas[indice]
            if not (sul <= coord[0] <= norte and oeste <= coord[1] <= leste):
                continue
            achado = _proximo_na_via(coord, via)
            if achado is None or achado[0] > rua_m:
                continue
            dist, along, proj = achado
            if melhor_local is None or dist < melhor_local[0]:
                melhor_local = (dist, indice, along, proj)
        if melhor_local is None:
            return None
        if preferida is not None and preferida != melhor_local[1]:
            achado = _proximo_na_via(coord, ruas[preferida])
            if achado is not None and achado[0] <= rua_m and achado[0] <= melhor_local[0] + 12:
                return preferida, achado[1], achado[2]
        return melhor_local[1], melhor_local[2], melhor_local[3]

    trechos: list[list[Ponto]] = []
    atual: list[Ponto] = []
    anterior: tuple[int, float, Ponto, datetime] | None = None

    def quebrar() -> None:
        nonlocal atual
        if len(atual) >= 2:
            trechos.append(atual)
        atual = []

    for momento, coord in seguidos:
        proj = projetar_rede(coord, anterior[0] if anterior else None)
        if proj is None:
            quebrar()
            anterior = None
            continue
        indice, along, ponto = proj
        if anterior is None:
            anterior = (indice, along, ponto, momento)
            continue
        reta = haversine_m(anterior[2], ponto)
        dt = abs((momento - anterior[3]).total_seconds())
        pela_forma = max(reta * 2.4, reta + 140.0)
        pela_hora = (42 / 3.6) * dt + 50 if dt > 0 else 80.0
        limite = min(1200.0, pela_forma, pela_hora)
        fatia = _ligar_na_rede(ruas, comps, adj, anterior[0], anterior[1], indice, along, limite)
        if fatia is None and reta <= 40:
            fatia = [anterior[2], ponto]
        if fatia and len(fatia) >= 2:
            if atual:
                _alongar(atual, fatia)
            else:
                atual = list(fatia)
        else:
            quebrar()
        anterior = (indice, along, ponto, momento)
    quebrar()
    if not trechos:
        return _quebrar_salto([coord for _momento, coord in seguidos], salto_max_m)
    saida = []
    for trecho in trechos:
        fino = _rdp(trecho, 6)
        if len(fino) >= 2:
            saida.append([[lat, lon] for lat, lon in fino])
    return saida


def _quebrar_salto(pontos: list[Ponto], salto_max_m: float) -> list[list[list[float]]]:
    trechos: list[list[Ponto]] = []
    atual = [pontos[0]]
    for coord in pontos[1:]:
        if haversine_m(atual[-1], coord) > salto_max_m:
            if len(atual) >= 2:
                trechos.append(atual)
            atual = [coord]
            continue
        atual.append(coord)
    if len(atual) >= 2:
        trechos.append(atual)
    return [[[lat, lon] for lat, lon in _rdp(trecho, 8)] for trecho in trechos if len(trecho) >= 2]


def ordem_do_pulso(ponto: dict[str, Any]) -> tuple[int, datetime | str]:
    """Ordena pelo instante do pulso. O texto DD/MM/AAAA inverte na virada do mês."""
    texto = str(ponto.get("timestamp") or "")
    marca = _datetime_do_timestamp(texto, date(2000, 1, 1))
    if marca is None:
        return (1, texto)
    return (0, marca)


def tracos_na_ordem(pontos: list[dict[str, Any]], salto_max_m: float = 160) -> list[list[list[float]]]:
    """Liga os pulsos na ordem do horário. Um salto acima de 160 m abre outro trecho."""
    ordenados = sorted(pontos or [], key=ordem_do_pulso)
    pares: list[Ponto] = []
    for ponto in ordenados:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        pares.append((float(lat), float(lon)))
    if len(pares) < 2:
        return []
    return _quebrar_salto(pares, salto_max_m)


def tracos_realistas(
    pontos: list[dict[str, Any]],
    salto_max_m: float = 380,
    tolerancia_m: float = 11,
    jitter_m: float = 4,
) -> list[list[list[float]]]:
    pares: list[Ponto] = []
    for ponto in pontos or []:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        pares.append((float(lat), float(lon)))
    if not pares:
        return []

    brutos: list[list[Ponto]] = []
    atual = [pares[0]]
    for pt in pares[1:]:
        dist = haversine_m(atual[-1], pt)
        if dist > salto_max_m:
            if len(atual) >= 2:
                brutos.append(atual)
            atual = [pt]
            continue
        if dist >= jitter_m:
            atual.append(pt)
    if len(atual) >= 2:
        brutos.append(atual)

    trechos = []
    for trecho in brutos:
        limpo = _rdp(trecho, tolerancia_m)
        if len(limpo) >= 2:
            trechos.append([[lat, lon] for lat, lon in limpo])
    return trechos


def group_by_vehicle(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agrupado: dict[str, list[dict[str, Any]]] = {}
    for ponto in pontos:
        nome = ponto.get("veiculo") or "sem-identificacao"
        agrupado.setdefault(nome, []).append(ponto)
    rotas = []
    for veiculo, items in agrupado.items():
        rotas.append(
            {
                "veiculo": veiculo,
                "total_pontos": len(items),
                "inicio": items[0].get("timestamp"),
                "fim": items[-1].get("timestamp"),
                "pontos": items,
            }
        )
    return rotas


def para_horario_brasilia(texto: str) -> str:
    """A Segtrack manda o pulso em UTC. O painel mostra o horário de Brasília."""
    bruto = (texto or "").strip()
    if not bruto:
        return bruto
    dt = _datetime_do_timestamp(bruto, date.today())
    if dt is None:
        return bruto
    local = dt - timedelta(hours=3)
    com_segundo = bool(re.search(r"\d{1,2}:\d{2}:\d{2}", bruto))
    if re.search(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}", bruto):
        if "/" in bruto:
            return local.strftime("%d/%m/%Y %H:%M:%S" if com_segundo else "%d/%m/%Y %H:%M")
        return local.strftime("%Y-%m-%d %H:%M:%S" if com_segundo else "%Y-%m-%d %H:%M")
    return local.strftime("%H:%M:%S" if com_segundo else "%H:%M")


def _datetime_do_timestamp(ts: str, dia: date) -> datetime | None:
    texto = (ts or "").strip()
    if not texto:
        return None
    bruto = texto.replace("T", " ")
    for fmt, tamanho in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%d/%m/%Y %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%d/%m/%Y %H:%M", 16),
    ):
        fatia = bruto[:tamanho]
        try:
            return datetime.strptime(fatia, fmt)
        except ValueError:
            continue
    achado = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", texto)
    if not achado:
        return None
    hora, minuto, segundo = int(achado.group(1)), int(achado.group(2)), int(achado.group(3) or 0)
    if hora > 23 or minuto > 59:
        return None
    return datetime.combine(dia, time(hora, minuto, min(segundo, 59)))


def filtrar_pontos_periodo(
    pontos: list[dict[str, Any]] | None,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    dia_referencia: date | None = None,
) -> list[dict[str, Any]]:
    """Mantém GPS no intervalo real (ex.: 21/09 06:00 → 22/09 05:59). Sem timestamp permanece."""
    from .config import janela_datetime

    inicio_txt = normalizar_hora(hora_inicio, "00:00")
    fim_txt = normalizar_hora(hora_fim, "23:59")
    inicio, fim = janela_datetime(dia, inicio_txt, fim_txt, data_fim)
    if inicio.date() == fim.date() and inicio_txt == "00:00" and fim_txt == "23:59":
        return list(pontos or [])
    ref = dia_referencia or dia
    saida: list[dict[str, Any]] = []
    for ponto in pontos or []:
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), ref)
        if dt is None:
            saida.append(ponto)
            continue
        if inicio <= dt <= fim:
            saida.append(ponto)
    return saida


def _fmt_pulso(dt: datetime, outro: datetime | None = None) -> str:
    if outro is None or dt.date() == outro.date():
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%d/%m %H:%M")


def extremos_por_hora(
    pontos: list[dict[str, Any]] | None,
    dia: date,
) -> dict[str, Any]:
    """Primeiro e último pulso do rastreador, ordenados pela hora (não pela linha do mapa)."""
    pares: list[tuple[datetime, float, float]] = []
    for ponto in pontos or []:
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        if dt is None:
            continue
        try:
            lat = float(ponto["latitude"])
            lon = float(ponto["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        pares.append((dt, lat, lon))
    if not pares:
        return {}
    pares.sort(key=lambda item: item[0])
    ini, fim = pares[0], pares[-1]
    if ini[0].date() == fim[0].date():
        texto = f"{ini[0].strftime('%d/%m %H:%M')}–{fim[0].strftime('%H:%M')}"
    else:
        texto = f"{ini[0].strftime('%d/%m %H:%M')}–{fim[0].strftime('%d/%m %H:%M')}"
    return {
        "inicio": [ini[1], ini[2]],
        "fim": [fim[1], fim[2]],
        "hora_inicio": ini[0].strftime("%d/%m %H:%M"),
        "hora_fim": fim[0].strftime("%d/%m %H:%M") if ini[0].date() != fim[0].date() else fim[0].strftime("%H:%M"),
        "horario": texto,
        "inicio_dt": ini[0],
        "fim_dt": fim[0],
    }


def periodo_gps(
    pontos: list[dict[str, Any]] | None,
    dia: date,
) -> tuple[datetime | None, datetime | None, str]:
    ext = extremos_por_hora(pontos, dia)
    if not ext:
        return None, None, ""
    return ext["inicio_dt"], ext["fim_dt"], str(ext["horario"])

