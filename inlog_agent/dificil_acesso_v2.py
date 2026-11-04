"""Camada aditiva V2 das áreas de difícil acesso.

Calcula acessos fixos (vias ∩ borda), passagens por placa e trechos
dentro/fora do polígono. Não altera tabelas, colunas nem o endpoint V1.
Qualquer falha devolve estruturas vazias em vez de derrubar a API.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from shapely.geometry import Point

from .gps import haversine_m
from .pdf_mapa import cor_da_placa

# Mais de 3 min sem pulso encerra a passagem (pedido V2).
GAP_PASSAGEM_S = 180.0
# Agrupa cruzamentos da mesma via na borda.
CLUSTER_ACESSO_M = 45.0
# Distância máxima para associar entrada/saída a um acesso nomeado.
MATCH_ACESSO_M = 120.0


def _ler_hora(texto: str) -> datetime | None:
    bruto = (texto or "").strip().replace("Z", "")
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(bruto[:19])
    except ValueError:
        return None


def _segundos(anterior: str, atual: str) -> float:
    a = _ler_hora(anterior)
    b = _ler_hora(atual)
    if a is None or b is None:
        return 0.0
    return abs((b - a).total_seconds())


def _gap_passagem(anterior: tuple[str, float, float], atual: tuple[str, float, float]) -> bool:
    return _segundos(anterior[0], atual[0]) > GAP_PASSAGEM_S


def _geom_wgs(aneis: list[list[list[float]]]):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polys = []
    for anel in aneis or []:
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
        return None
    geom = unary_union(polys) if len(polys) > 1 else polys[0]
    return None if geom.is_empty else geom


def _geom_utm(aneis: list[list[list[float]]]):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from .mapa_oficial import TO_UTM

    polys = []
    for anel in aneis or []:
        coords = []
        for par in anel:
            if not isinstance(par, (list, tuple)) or len(par) < 2:
                continue
            try:
                lat = float(par[0])
                lon = float(par[1])
            except (TypeError, ValueError):
                continue
            x, y = TO_UTM.transform(lon, lat)
            coords.append((float(x), float(y)))
        if len(coords) < 4:
            continue
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            polys.append(poly)
    if not polys:
        return None
    geom = unary_union(polys) if len(polys) > 1 else polys[0]
    return None if geom.is_empty else geom


def _utm_para_wgs(x: float, y: float) -> tuple[float, float]:
    from .mapa_oficial import TO_WGS

    lon, lat = TO_WGS.transform(float(x), float(y))
    return float(lat), float(lon)


def _pontos_geom(geom) -> list[tuple[float, float]]:
    """Extrai vértices (x, y) de Point / MultiPoint / GeometryCollection."""
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo == "Point":
        return [(float(geom.x), float(geom.y))]
    if tipo == "MultiPoint":
        return [(float(p.x), float(p.y)) for p in geom.geoms]
    if tipo == "GeometryCollection":
        saida: list[tuple[float, float]] = []
        for parte in geom.geoms:
            saida.extend(_pontos_geom(parte))
        return saida
    if tipo in {"LineString", "MultiLineString"}:
        # Cruzamento tangente: pega extremos da interseção com a borda.
        coords = list(geom.coords) if tipo == "LineString" else [c for g in geom.geoms for c in g.coords]
        if not coords:
            return []
        return [(float(coords[0][0]), float(coords[0][1])), (float(coords[-1][0]), float(coords[-1][1]))]
    return []


def _agrupar_acessos(pontos: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Agrupa cruzamentos próximos; devolve centroides em WGS (lat, lon)."""
    if not pontos:
        return []
    restos = list(pontos)
    grupos: list[list[tuple[float, float]]] = []
    while restos:
        base = restos.pop(0)
        lat0, lon0 = _utm_para_wgs(base[0], base[1])
        cluster = [base]
        i = 0
        while i < len(restos):
            lat1, lon1 = _utm_para_wgs(restos[i][0], restos[i][1])
            if haversine_m((lat0, lon0), (lat1, lon1)) <= CLUSTER_ACESSO_M:
                cluster.append(restos.pop(i))
            else:
                i += 1
        cx = sum(p[0] for p in cluster) / len(cluster)
        cy = sum(p[1] for p in cluster) / len(cluster)
        grupos.append((cx, cy))
    # Ordena pelo ângulo em torno do centroide para nomes estáveis.
    if len(grupos) <= 1:
        return [_utm_para_wgs(g[0], g[1]) for g in grupos]
    mx = sum(g[0] for g in grupos) / len(grupos)
    my = sum(g[1] for g in grupos) / len(grupos)
    grupos.sort(key=lambda g: math.atan2(g[1] - my, g[0] - mx))
    return [_utm_para_wgs(g[0], g[1]) for g in grupos]


def calcular_acessos(area: dict[str, Any], malha_info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Pontos onde vias oficiais cruzam a borda do polígono."""
    try:
        aneis = area.get("poligonos") or []
        geom_utm = None
        ruas = []
        if malha_info:
            geom_utm = malha_info.get("geom")
            ruas = list(malha_info.get("ruas") or [])
        if geom_utm is None:
            geom_utm = _geom_utm(aneis)
        if geom_utm is None or geom_utm.is_empty:
            return []
        borda = geom_utm.boundary
        if borda is None or borda.is_empty:
            return []
        # Se a malha já veio recortada ao polígono, as vias tocam a borda nos extremos.
        cruzamentos: list[tuple[float, float]] = []
        for rua in ruas:
            try:
                inter = rua.intersection(borda)
            except Exception:
                continue
            cruzamentos.extend(_pontos_geom(inter))
            # Extremos da via recortada que caem na borda (~2 m).
            try:
                for extremo in (Point(*rua.coords[0]), Point(*rua.coords[-1])):
                    if borda.distance(extremo) <= 2.0:
                        cruzamentos.append((float(extremo.x), float(extremo.y)))
            except Exception:
                pass
        if not cruzamentos:
            # Fallback: amostra da borda em intervalos regulares (sem inventar via).
            try:
                comprimento = float(borda.length or 0)
                if comprimento > 0:
                    passos = max(4, min(12, int(comprimento / 80)))
                    for i in range(passos):
                        pt = borda.interpolate(i / passos, normalized=True)
                        cruzamentos.append((float(pt.x), float(pt.y)))
            except Exception:
                return []
        centros = _agrupar_acessos(cruzamentos)
        saida = []
        for i, (lat, lon) in enumerate(centros, start=1):
            saida.append(
                {
                    "id": f"acesso-{i}",
                    "nome": f"Acesso {i}",
                    "n": i,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                }
            )
        return saida
    except Exception as exc:
        print(f"Acessos V2 {area.get('nome') or area.get('id')}: {exc}", flush=True)
        return []


def _acesso_mais_proximo(lat: float, lon: float, acessos: list[dict[str, Any]]) -> dict[str, Any] | None:
    melhor = None
    dist = None
    for acesso in acessos or []:
        try:
            d = haversine_m((lat, lon), (float(acesso["lat"]), float(acesso["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
        if dist is None or d < dist:
            dist = d
            melhor = acesso
    if melhor is None or dist is None or dist > MATCH_ACESSO_M:
        return melhor  # ainda devolve o mais próximo; o front mostra o nome
    return melhor


def _dentro(lat: float, lon: float, pronto) -> bool:
    try:
        return bool(pronto.covers(Point(float(lon), float(lat))))
    except Exception:
        return False


def _coord(lat: float, lon: float) -> list[float]:
    return [round(float(lat), 5), round(float(lon), 5)]


def _status_area(area: dict[str, Any], n_passagens: int) -> dict[str, str]:
    """Mesmo padrão dos cards de rota: Concluída / Parcial / Não iniciada."""
    programada = bool(area.get("programada"))
    pct = area.get("percentual")
    if not programada and n_passagens <= 0 and not area.get("atividade"):
        return {"status": "Não iniciada", "grupo": "nao"}
    try:
        valor = float(pct) if pct is not None and pct != "" else None
    except (TypeError, ValueError):
        valor = None
    if valor is None:
        if n_passagens > 0 or area.get("atividade"):
            return {"status": "Parcial", "grupo": "parcial"}
        return {"status": "Não iniciada", "grupo": "nao"}
    if valor >= 100:
        return {"status": "Concluída", "grupo": "concluida"}
    if valor > 0:
        return {"status": "Parcial", "grupo": "parcial"}
    return {"status": "Não iniciada", "grupo": "nao"}


def passagens_do_veiculo(
    pontos: list[tuple[str, float, float]],
    geom_wgs,
    acessos: list[dict[str, Any]],
    *,
    codigo: str,
    placa: str,
    area_id: str,
    area_nome: str,
) -> list[dict[str, Any]]:
    """Cada sequência contínua dentro do polígono vira uma passagem."""
    if geom_wgs is None or not pontos:
        return []
    try:
        from shapely.prepared import prep

        pronto = prep(geom_wgs)
    except Exception:
        return []

    ordenados = sorted(set(pontos), key=lambda item: (item[0], item[1], item[2]))
    passagens: list[dict[str, Any]] = []
    atual: list[tuple[str, float, float]] = []
    n = 0

    def fechar(aberta: bool) -> None:
        nonlocal n, atual
        if len(atual) < 1:
            atual = []
            return
        n += 1
        ent = atual[0]
        sai = atual[-1]
        acesso_e = _acesso_mais_proximo(ent[1], ent[2], acessos)
        acesso_s = None if aberta else _acesso_mais_proximo(sai[1], sai[2], acessos)
        coords = []
        for _ts, lat, lon in atual:
            par = _coord(lat, lon)
            if not coords or par != coords[-1]:
                coords.append(par)
        passagens.append(
            {
                "id": f"{codigo}-{area_id}-p{n}",
                "n": n,
                "rotulo": f"P{n}",
                "marcador_entrada": f"E{n}",
                "marcador_saida": f"S{n}",
                "veiculo": codigo,
                "placa": placa,
                "cor": cor_da_placa(placa),
                "area_id": area_id,
                "area": area_nome,
                "entrada": {
                    "acesso": (acesso_e or {}).get("nome") or "",
                    "acesso_id": (acesso_e or {}).get("id") or "",
                    "ts": ent[0],
                    "lat": round(ent[1], 5),
                    "lon": round(ent[2], 5),
                },
                "saida": None
                if aberta
                else {
                    "acesso": (acesso_s or {}).get("nome") or "",
                    "acesso_id": (acesso_s or {}).get("id") or "",
                    "ts": sai[0],
                    "lat": round(sai[1], 5),
                    "lon": round(sai[2], 5),
                },
                "segundos": int(round(_segundos(ent[0], sai[0]))),
                "aberta": aberta,
                "coords": coords,
            }
        )
        atual = []

    anterior: tuple[str, float, float] | None = None
    for ponto in ordenados:
        dentro = _dentro(ponto[1], ponto[2], pronto)
        if atual and anterior is not None and _gap_passagem(anterior, ponto):
            fechar(aberta=False)
        if dentro:
            if not atual and anterior is not None and not _dentro(anterior[1], anterior[2], pronto):
                # Inclui o ponto externo imediatamente anterior para a linha tocar a borda.
                atual.append(anterior)
            atual.append(ponto)
        else:
            if atual:
                atual.append(ponto)  # ponto de saída (fora) para fechar na borda
                fechar(aberta=False)
        anterior = ponto
    if atual:
        fechar(aberta=True)
    return passagens


def trechos_do_veiculo(
    pontos: list[tuple[str, float, float]],
    geom_wgs,
    passagens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trajetória completa: dentro = cor da placa; fora = cinza no front."""
    if not pontos:
        return []
    ordenados = sorted(set(pontos), key=lambda item: (item[0], item[1], item[2]))
    pronto = None
    if geom_wgs is not None:
        try:
            from shapely.prepared import prep

            pronto = prep(geom_wgs)
        except Exception:
            pronto = None

    # Mapa ts+coord → passagem n (só pontos internos).
    mapa_n: dict[tuple[str, float, float], int] = {}
    for pas in passagens or []:
        n = int(pas.get("n") or 0)
        for par in pas.get("coords") or []:
            if len(par) < 2:
                continue
            # coords não têm ts; associação por proximidade temporal via sequência abaixo
            pass
        mapa_n[n] = n  # placeholder para tipagem; associação real por intervalo de tempo

    intervalos = []
    for pas in passagens or []:
        ent = ((pas.get("entrada") or {}).get("ts") or "")
        sai = ""
        if pas.get("saida"):
            sai = str((pas["saida"] or {}).get("ts") or "")
        elif pas.get("aberta"):
            sai = "9999-12-31T23:59:59"
        if ent:
            intervalos.append((ent, sai or ent, int(pas.get("n") or 0)))

    def passagem_de(ts: str) -> int | None:
        for a, b, n in intervalos:
            if a <= ts <= b:
                return n
        return None

    trechos: list[dict[str, Any]] = []
    balde: list[list[float]] = []
    estado: bool | None = None
    passagem_atual: int | None = None

    def flush() -> None:
        nonlocal balde, estado, passagem_atual
        if len(balde) >= 2 and estado is not None:
            trechos.append(
                {
                    "dentro": bool(estado),
                    "passagem": passagem_atual,
                    "coords": balde,
                }
            )
        balde = []

    for i, (ts, lat, lon) in enumerate(ordenados):
        dentro = bool(pronto and _dentro(lat, lon, pronto))
        n_pas = passagem_de(ts) if dentro else None
        if estado is None:
            estado = dentro
            passagem_atual = n_pas
            balde = [_coord(lat, lon)]
            continue
        # Quebra por gap de passagem ou mudança dentro/fora
        ant = ordenados[i - 1]
        muda = dentro != estado or (dentro and n_pas != passagem_atual)
        if muda or _gap_passagem(ant, (ts, lat, lon)):
            # ponto de transição compartilha o vértice
            balde.append(_coord(lat, lon))
            flush()
            estado = dentro
            passagem_atual = n_pas
            balde = [_coord(lat, lon)]
        else:
            par = _coord(lat, lon)
            if not balde or par != balde[-1]:
                balde.append(par)
    flush()
    return trechos


def montar_area_v2(
    area: dict[str, Any],
    series: dict[str, list[tuple[str, float, float]]],
    basculantes: tuple[dict[str, str], ...] | list[dict[str, str]],
    malha_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copia a área V1 e acrescenta acessos + passagens. Falha → listas vazias."""
    item = dict(area)
    try:
        acessos = calcular_acessos(area, malha_info)
    except Exception as exc:
        print(f"Acessos V2 falhou {area.get('nome')}: {exc}", flush=True)
        acessos = []
    geom = _geom_wgs(area.get("poligonos") or [])
    todas: list[dict[str, Any]] = []
    for basc in basculantes:
        codigo = basc["codigo"]
        placa = basc["placa"]
        try:
            pas = passagens_do_veiculo(
                series.get(codigo) or [],
                geom,
                acessos,
                codigo=codigo,
                placa=placa,
                area_id=str(area.get("id") or ""),
                area_nome=str(area.get("nome") or ""),
            )
        except Exception as exc:
            print(f"Passagens V2 {placa} / {area.get('nome')}: {exc}", flush=True)
            pas = []
        todas.extend(pas)
    todas.sort(key=lambda p: ((p.get("entrada") or {}).get("ts") or "", p.get("placa") or "", p.get("n") or 0))
    # Renumerar marcadores por área (E1/S1 globais na área, por ordem temporal).
    por_placa: dict[str, int] = {}
    for pas in todas:
        placa = str(pas.get("placa") or "")
        por_placa[placa] = por_placa.get(placa, 0) + 1
        n = por_placa[placa]
        pas["n"] = n
        pas["rotulo"] = f"P{n}"
        pas["marcador_entrada"] = f"E{n}"
        pas["marcador_saida"] = f"S{n}"
        pas["id"] = f"{pas.get('veiculo')}-{area.get('id')}-p{n}"
    st = _status_area(item, len(todas))
    item["acessos"] = acessos
    item["passagens"] = todas
    item["status"] = st["status"]
    item["grupo"] = st["grupo"]
    return item


def montar_veiculo_v2(
    basc: dict[str, str],
    serie: list[tuple[str, float, float]],
    areas_v2: list[dict[str, Any]],
) -> dict[str, Any]:
    """Trechos e marcadores E/S do veículo. Uma trajetória, várias áreas."""
    placa = basc["placa"]
    codigo = basc["codigo"]
    cor = cor_da_placa(placa)
    marcadores: list[dict[str, Any]] = []
    minhas_passagens: list[dict[str, Any]] = []
    geoms = []
    trechos_por_area: dict[str, list[dict[str, Any]]] = {}

    for area in areas_v2:
        pas_area = [p for p in (area.get("passagens") or []) if p.get("placa") == placa]
        if not pas_area and not area.get("atividade"):
            continue
        geom = _geom_wgs(area.get("poligonos") or [])
        if geom is not None:
            geoms.append(geom)
        for pas in pas_area:
            minhas_passagens.append(pas)
            ent = pas.get("entrada") or {}
            if ent.get("lat") is not None and ent.get("lon") is not None:
                marcadores.append(
                    {
                        "tipo": "E",
                        "n": pas.get("n"),
                        "rotulo": pas.get("marcador_entrada") or f"E{pas.get('n')}",
                        "passagem_id": pas.get("id"),
                        "area_id": area.get("id"),
                        "placa": placa,
                        "cor": cor,
                        "ts": ent.get("ts"),
                        "lat": ent.get("lat"),
                        "lon": ent.get("lon"),
                    }
                )
            sai = pas.get("saida")
            if isinstance(sai, dict) and sai.get("lat") is not None:
                marcadores.append(
                    {
                        "tipo": "S",
                        "n": pas.get("n"),
                        "rotulo": pas.get("marcador_saida") or f"S{pas.get('n')}",
                        "passagem_id": pas.get("id"),
                        "area_id": area.get("id"),
                        "placa": placa,
                        "cor": cor,
                        "ts": sai.get("ts"),
                        "lat": sai.get("lat"),
                        "lon": sai.get("lon"),
                    }
                )
        try:
            trechos_por_area[str(area.get("id") or "")] = trechos_do_veiculo(serie, geom, pas_area)
        except Exception as exc:
            print(f"Trechos V2 {placa}: {exc}", flush=True)
            trechos_por_area[str(area.get("id") or "")] = []

    # Trajetória global: união das geometrias relevantes (sem duplicar o GPS).
    trechos: list[dict[str, Any]] = []
    try:
        from shapely.ops import unary_union

        geom_uniao = unary_union(geoms) if geoms else None
        trechos = trechos_do_veiculo(serie, geom_uniao, minhas_passagens)
    except Exception as exc:
        print(f"União trechos V2 {placa}: {exc}", flush=True)
        trechos = []

    if not trechos and serie:
        coords = []
        for _ts, lat, lon in sorted(set(serie), key=lambda i: (i[0], i[1], i[2])):
            par = _coord(lat, lon)
            if not coords or par != coords[-1]:
                coords.append(par)
        if len(coords) >= 2:
            trechos.append({"dentro": False, "passagem": None, "coords": coords})

    ultimo = None
    if serie:
        ultimo = max(serie, key=lambda i: i[0])
    return {
        "codigo": codigo,
        "placa": placa,
        "cor": cor,
        "pulsos": len(serie or []),
        "ultimo_gps": ultimo[0] if ultimo else "",
        "lat": round(ultimo[1], 5) if ultimo else None,
        "lon": round(ultimo[2], 5) if ultimo else None,
        "trechos": trechos,
        "trechos_por_area": trechos_por_area,
        "marcadores": marcadores,
        "passagens": minhas_passagens,
    }


def pacote_vazio(data: str, turno: str) -> dict[str, Any]:
    return {
        "versao": 2,
        "data": data,
        "turno": turno,
        "fonte": "live_sync",
        "camada": "Áreas de difícil acesso v2",
        "areas": [],
        "veiculos": [],
        "placas": [],
        "resumo": {},
    }
