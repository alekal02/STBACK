"""Três relatórios da Segtrack: frota, rota e placa.

A conta é a mesma em todos: ruas oficiais do GeoPackage e os pulsos do dia.
Parada de coleta = o caminhão ficou num raio de 35 m por pelo menos 50 s, a até 6 km/h.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import Point, Polygon

from .frota import norm_placa, prefixo_da_placa
from .gps import _datetime_do_timestamp, haversine_m
from .mapa import TO_UTM

RAIO_PARADA_M = 35.0
PARADA_MIN_S = 50.0
PARADA_COLETA_MAX_S = 15 * 60
VEL_PARADA = 6.0
FEITA_MIN = 90.0
PARCIAL_MIN = 45.0
# Apoio é outro caminhão dentro da rota, com pulsos suficientes.
# Cruzar o polígono em poucos minutos não entra.
APOIO_PULSOS_MIN = 30
APOIO_MINUTOS_MIN = 6.0
APOIO_PULSOS_SEM_TEMPO = 80
# O mapa marca diurno e noturno. A coleta do dia acaba às 16:00; o noturno segue até 05:59.
MINUTO_DIURNO_INI = 6 * 60
MINUTO_DIURNO_FIM = 16 * 60


def _thin(pts: list, max_pts: int = 90) -> list:
    if not pts or len(pts) <= max_pts:
        return pts
    passo = max(1, len(pts) // max_pts)
    saida = list(pts[::passo])
    if saida[-1] != pts[-1]:
        saida.append(pts[-1])
    return saida


def _vel(valor: Any) -> float | None:
    if valor in (None, ""):
        return None
    try:
        return float(str(valor).replace(",", ".").replace("km/h", "").strip())
    except (TypeError, ValueError):
        return None


def _sequencia(pontos: list[dict[str, Any]], dia: date) -> list[tuple[datetime, float, float, float | None]]:
    seq = []
    for ponto in pontos or []:
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        try:
            lat = float(ponto["latitude"])
            lon = float(ponto["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if dt is None:
            continue
        seq.append((dt, lat, lon, _vel(ponto.get("velocidade"))))
    seq.sort(key=lambda item: item[0])
    return seq


def listar_paradas(pontos: list[dict[str, Any]], dia: date) -> list[dict[str, Any]]:
    seq = _sequencia(pontos, dia)
    paradas: list[dict[str, Any]] = []
    i = 0
    while i < len(seq):
        j = i
        while j + 1 < len(seq):
            dist_ancora = haversine_m((seq[i][1], seq[i][2]), (seq[j + 1][1], seq[j + 1][2]))
            vel = seq[j + 1][3]
            if dist_ancora > RAIO_PARADA_M or (vel is not None and vel > VEL_PARADA):
                break
            j += 1
        duracao = (seq[j][0] - seq[i][0]).total_seconds()
        if duracao >= PARADA_MIN_S and j > i:
            fatia = seq[i : j + 1]
            paradas.append(
                {
                    "latitude": round(sum(p[1] for p in fatia) / len(fatia), 6),
                    "longitude": round(sum(p[2] for p in fatia) / len(fatia), 6),
                    "segundos": int(duracao),
                    "inicio": fatia[0][0].strftime("%H:%M"),
                    "fim": fatia[-1][0].strftime("%H:%M"),
                    "tipo": "coleta" if duracao <= PARADA_COLETA_MAX_S else "permanencia",
                }
            )
            i = j + 1
        else:
            i += 1
    return paradas


def _dentro(geom, lat: float, lon: float) -> bool:
    """O ponto conta só se estiver dentro do polígono oficial, sem folga."""
    if geom is None or geom.is_empty:
        return False
    x, y = TO_UTM.transform(lon, lat)
    return bool(geom.covers(Point(x, y)))


_PATIOS: list | None = None
FOLGA_PATIO_M = 50.0


def _patios() -> list:
    """Base e aterro, com uma folga ao lado do polígono, não contam como rota feita."""
    global _PATIOS
    if _PATIOS is not None:
        return _PATIOS
    from .config import ROOT

    zonas = []
    arquivo = ROOT / "data" / "locais.json"
    if arquivo.is_file():
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        for local in dados.get("locais") or []:
            if str(local.get("id") or "") not in {"base", "aterro"}:
                continue
            for anel in local.get("aneis") or []:
                coords = []
                for ponto in anel:
                    if len(ponto) < 2:
                        continue
                    x, y = TO_UTM.transform(float(ponto[1]), float(ponto[0]))
                    coords.append((x, y))
                if len(coords) < 4:
                    continue
                zona = Polygon(coords)
                if not zona.is_valid:
                    zona = zona.buffer(0)
                zonas.append(zona.buffer(FOLGA_PATIO_M))
    _PATIOS = zonas
    return zonas


def _no_patio(lat: float, lon: float) -> bool:
    x, y = TO_UTM.transform(lon, lat)
    ponto = Point(x, y)
    return any(zona.covers(ponto) for zona in _patios())


def _arvore_do_setor(setor: dict[str, Any]):
    from shapely.strtree import STRtree

    guardada = setor.get("_arvore")
    if guardada is not None:
        return guardada or None
    pecas = setor.get("_pecas") or []
    linhas = [peca[0] for peca in pecas] if pecas else [
        rua for rua in (setor.get("ruas") or []) if rua is not None and not getattr(rua, "is_empty", True)
    ]
    arvore = STRtree(linhas) if linhas else False
    setor["_arvore"] = arvore
    return arvore or None


def _setor_da_parada(lat: float, lon: float, setores: list[dict[str, Any]]) -> dict[str, Any] | None:
    from .cobertura import RAIO_M

    x, y = TO_UTM.transform(lon, lat)
    ponto = Point(x, y)
    donos = []
    for setor in setores:
        geom = setor.get("geom")
        if geom is None or geom.is_empty:
            continue
        if geom.covers(ponto):
            donos.append(setor)
    if donos:
        return min(donos, key=lambda setor: setor["geom"].area)
    melhor = None
    melhor_m = RAIO_M + 1
    for setor in setores:
        arvore = _arvore_do_setor(setor)
        if arvore is None:
            continue
        achados = arvore.query(ponto, predicate="dwithin", distance=RAIO_M)
        if len(achados) == 0:
            continue
        distancia = min(float(arvore.geometries[int(i)].distance(ponto)) for i in achados)
        if distancia <= RAIO_M and distancia < melhor_m:
            melhor = setor
            melhor_m = distancia
    return melhor


def _saida(pontos: list[dict[str, Any]], dia: date) -> dict[str, Any] | None:
    seq = _sequencia(pontos, dia)
    if not seq:
        return None
    dt, lat, lon, _vel_ponto = seq[0]
    return {
        "latitude": lat,
        "longitude": lon,
        "timestamp": dt.strftime("%d/%m/%Y %H:%M"),
    }


def medir_presenca(rotas: list[dict[str, Any]], setores: list[dict[str, Any]], dia: date) -> None:
    for item in rotas:
        pontos = item.get("pontos") or []
        item["saida"] = _saida(pontos, dia)
        paradas = listar_paradas(pontos, dia)
        item["paradas_dia"] = len(paradas)
        por_parada: dict[str, list[dict[str, Any]]] = {}
        for parada in paradas:
            if _no_patio(parada["latitude"], parada["longitude"]):
                continue
            setor = _setor_da_parada(parada["latitude"], parada["longitude"], setores)
            if setor is None:
                continue
            if not _parada_no_turno(str(parada.get("inicio") or ""), str(setor.get("turno") or "")):
                continue
            por_parada.setdefault(str(setor.get("codigo") or ""), []).append(parada)
        por_codigo = {str(setor.get("codigo") or ""): setor for setor in setores}
        for cob in item.get("coberturas_inlog") or []:
            codigo = str(cob.get("codigo_rota") or "")
            oficial = por_codigo.get(codigo) or {}
            cob["turno"] = oficial.get("turno") or cob.get("turno") or ""
            cob["dias"] = oficial.get("dias") or cob.get("dias") or []
            if oficial.get("frequencia"):
                cob["rota"] = oficial.get("frequencia")
            dentro = por_parada.get(codigo, [])
            coleta = [p for p in dentro if p.get("tipo") == "coleta"]
            permanencia = [p for p in dentro if p.get("tipo") == "permanencia"]
            cob["paradas"] = len(coleta)
            cob["minutos_parado"] = round(sum(p["segundos"] for p in coleta) / 60.0, 1)
            cob["permanencias"] = len(permanencia)
            cob["minutos_permanencia"] = round(sum(p["segundos"] for p in permanencia) / 60.0, 1)
            cob["paradas_pontos"] = coleta[:80]
            pulsos, minutos = _pulsos_dentro(pontos, oficial.get("geom"), dia, oficial.get("ruas") or [])
            cob["pulsos"] = pulsos
            cob["minutos_dentro"] = minutos
        _definir_papeis(item)
    _aplicar_apoio(rotas)


def _parada_no_turno(inicio: str, turno: str) -> bool:
    """Parada de dia não executa rota noturna, e parada de madrugada não executa rota diurna."""
    if turno not in {"DIURNO", "NOTURNO"}:
        return True
    try:
        hora, minuto = (inicio or "00:00").split(":")[:2]
        marca = int(hora) * 60 + int(minuto)
    except ValueError:
        return True
    diurno = MINUTO_DIURNO_INI <= marca < MINUTO_DIURNO_FIM
    return diurno if turno == "DIURNO" else not diurno


def _e_coleta(cob: dict[str, Any]) -> bool:
    return int(cob.get("paradas") or 0) >= 3


def _pulsos_dentro(pontos: list[dict[str, Any]], geom, dia: date, ruas: list | None = None) -> tuple[int, float]:
    """Pulsos a até 45 m da rua oficial, ou dentro do polígono quando a rua não está no mapa."""
    from shapely.strtree import STRtree

    from .cobertura import RAIO_M

    if not pontos:
        return 0, 0.0
    linhas = [rua for rua in (ruas or []) if rua is not None and not getattr(rua, "is_empty", True)]
    arvore = STRtree(linhas) if linhas else None
    if arvore is None and (geom is None or geom.is_empty):
        return 0, 0.0
    if arvore is not None:
        minx, miny, maxx, maxy = arvore.geometries[0].bounds
        for linha in arvore.geometries[1:]:
            a, b, c, d = linha.bounds
            minx, miny, maxx, maxy = min(minx, a), min(miny, b), max(maxx, c), max(maxy, d)
        minx -= RAIO_M
        miny -= RAIO_M
        maxx += RAIO_M
        maxy += RAIO_M
    else:
        minx, miny, maxx, maxy = geom.bounds
    marcas: list[datetime] = []
    total = 0
    for ponto in pontos:
        try:
            lon = float(ponto["longitude"])
            lat = float(ponto["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        x, y = TO_UTM.transform(lon, lat)
        if x < minx or x > maxx or y < miny or y > maxy:
            continue
        marca_ponto = Point(x, y)
        if arvore is not None:
            if len(arvore.query(marca_ponto, predicate="dwithin", distance=RAIO_M)) == 0:
                continue
        elif not geom.covers(marca_ponto):
            continue
        total += 1
        marca = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        if marca is not None:
            marcas.append(marca)
    if len(marcas) < 2:
        return total, 0.0
    minutos = (max(marcas) - min(marcas)).total_seconds() / 60.0
    return total, round(minutos, 1)


def _pulsos_registrados(cob: dict[str, Any]) -> int:
    if cob.get("pulsos") is not None:
        return int(cob.get("pulsos") or 0)
    return int(cob.get("pontos_no_setor") or 0)


def _conta_como_apoio(cob: dict[str, Any]) -> bool:
    """Entrou na rota e deixou pulsos bastante. Passagem rápida fica de fora."""
    pulsos = _pulsos_registrados(cob)
    if cob.get("pulsos") is not None and cob.get("minutos_dentro") is not None:
        return pulsos >= APOIO_PULSOS_MIN and float(cob.get("minutos_dentro") or 0) >= APOIO_MINUTOS_MIN
    return pulsos >= APOIO_PULSOS_SEM_TEMPO


def _aplicar_apoio(frota: list[dict[str, Any]]) -> None:
    """Em cada rota, um caminhão atuou. Os outros com pulsos suficientes são apoio."""
    por_rota: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for item in frota:
        for cob in item.get("coberturas_inlog") or []:
            codigo = str(cob.get("codigo_rota") or "")
            if codigo:
                por_rota.setdefault(codigo, []).append((item, cob))
    for pares in por_rota.values():
        fortes = [
            (item, cob)
            for item, cob in pares
            if _conta_como_apoio(cob) or int(cob.get("paradas") or 0) >= 3
        ]
        if not fortes:
            continue
        fortes.sort(
            key=lambda par: (
                int(par[1].get("paradas") or 0),
                _pulsos_registrados(par[1]),
                float(par[1].get("percentual") or 0),
            ),
            reverse=True,
        )
        principal = fortes[0][1]
        for _item, cob in pares:
            if cob is principal:
                cob["papel"] = "ATUANDO"
            elif _conta_como_apoio(cob):
                cob["papel"] = "APOIO"
            elif str(cob.get("papel") or "") in {"ATUANDO", "APOIO"}:
                cob["papel"] = "PASSAGEM"
    for item in frota:
        cobs = list(item.get("coberturas_inlog") or [])
        atuando = [cob for cob in cobs if cob.get("papel") == "ATUANDO"]
        apoios = [cob for cob in cobs if cob.get("papel") == "APOIO"]
        if atuando:
            melhor = max(atuando, key=lambda cob: (int(cob.get("paradas") or 0), _pulsos_registrados(cob)))
            item["papel"] = "ATUANDO"
            item["codigo_rota"] = melhor.get("codigo_rota")
            item["setor"] = melhor.get("setor") or item.get("setor")
            item["percentual_concluido"] = melhor.get("percentual") or 0
        elif apoios:
            melhor = max(apoios, key=_pulsos_registrados)
            item["papel"] = "APOIO"
            item["codigo_rota"] = melhor.get("codigo_rota")
            item["setor"] = melhor.get("setor") or item.get("setor")
            item["percentual_concluido"] = melhor.get("percentual") or 0


def _definir_papeis(item: dict[str, Any]) -> None:
    cobs = list(item.get("coberturas_inlog") or [])
    for cob in cobs:
        cob["papel"] = "PASSAGEM"
    por_turno: dict[str, list[dict[str, Any]]] = {}
    for cob in cobs:
        if _e_coleta(cob):
            por_turno.setdefault(str(cob.get("turno") or ""), []).append(cob)
        elif float(cob.get("minutos_permanencia") or 0) >= 20:
            cob["papel"] = "PERMANENCIA"
    candidatos = [cob for grupo in por_turno.values() for cob in grupo]
    if not candidatos:
        item["papel"] = "PASSAGEM" if cobs else (item.get("veredicto") or "SEM COLETA")
        item["paradas"] = 0
        item["minutos_parado"] = 0
        return
    for grupo in por_turno.values():
        grupo.sort(
            key=lambda cob: (int(cob.get("paradas") or 0), float(cob.get("minutos_parado") or 0)),
            reverse=True,
        )
        for indice, cob in enumerate(grupo):
            if indice == 0:
                cob["papel"] = "ATUANDO"
            elif _conta_como_apoio(cob):
                cob["papel"] = "APOIO"
            else:
                cob["papel"] = "PASSAGEM"
    candidatos.sort(
        key=lambda cob: (int(cob.get("paradas") or 0), float(cob.get("minutos_parado") or 0)),
        reverse=True,
    )
    atuando = candidatos[0]
    item["papel"] = "ATUANDO"
    item["setor"] = atuando.get("setor") or item.get("setor")
    item["rota"] = atuando.get("rota") or item.get("rota")
    item["codigo_rota"] = atuando.get("codigo_rota")
    item["percentual_concluido"] = atuando.get("percentual") or 0
    item["veredicto"] = atuando.get("execucao") or atuando.get("veredicto")
    item["km_coleta"] = atuando.get("km_coleta")
    item["km_total"] = atuando.get("km_total")
    item["km_faltando"] = atuando.get("km_faltando")
    item["confianca"] = atuando.get("confianca")
    item["trechos_feitos"] = atuando.get("trechos_feitos") or []
    item["trechos_faltando"] = atuando.get("trechos_faltando") or []
    item["poligonos"] = atuando.get("poligonos") or []
    item["paradas"] = atuando.get("paradas") or 0
    item["minutos_parado"] = atuando.get("minutos_parado") or 0
    item["paradas_pontos"] = atuando.get("paradas_pontos") or []


def setores_leves(setores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leve = []
    for setor in setores:
        aneis = []
        for anel in (setor.get("poligonos") or [])[:2]:
            if anel and len(anel) >= 4:
                aneis.append(_thin(anel, 80))
        leve.append(
            {
                "codigo": setor.get("codigo") or "",
                "nome": setor.get("nome") or "",
                "frequencia": setor.get("frequencia") or "",
                "turno": setor.get("turno") or "",
                "dias": setor.get("dias") or [],
                "diario": bool(setor.get("diario")),
                "km_oficial": setor.get("km_oficial"),
                "poligonos": aneis,
            }
        )
    return leve


def pontos_leves(rotas: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    saida = {}
    for item in rotas:
        placa = str(item.get("placa") or "")
        pontos = []
        for ponto in item.get("pontos") or []:
            if ponto.get("latitude") is None:
                continue
            pontos.append(
                {
                    "timestamp": ponto.get("timestamp") or "",
                    "latitude": ponto.get("latitude"),
                    "longitude": ponto.get("longitude"),
                    "velocidade": ponto.get("velocidade") or "",
                }
            )
        saida[placa] = pontos
    return saida


def _ler(pasta: Path, nome: str) -> Any:
    arquivo = pasta / nome
    if not arquivo.is_file():
        return None
    return json.loads(arquivo.read_text(encoding="utf-8"))


def _pasta(output_dir: Path, dia: date) -> Path:
    return output_dir / dia.isoformat()


def _fmt_pct(valor: Any) -> str:
    try:
        return f"{float(valor):.1f}".replace(".", ",") + "%"
    except (TypeError, ValueError):
        return "0%"


def _nome_conclusao(conclusao: str) -> str:
    return {"FEITA": "Feita", "PARCIAL": "Parcial", "NÃO FEITA": "Não feita"}.get(conclusao, "Não feita")


def _conclusao(pct: float, teve_coleta: bool) -> str:
    if not teve_coleta or pct < PARCIAL_MIN:
        return "NÃO FEITA"
    if pct >= FEITA_MIN:
        return "FEITA"
    return "PARCIAL"


def _nome(item: dict[str, Any]) -> str:
    prefixo = str(item.get("prefixo") or "")
    placa = str(item.get("placa") or "")
    if prefixo and placa:
        return f"{prefixo} · {placa}"
    return placa or prefixo or "caminhão"


def _oficiais(setores: list[dict[str, Any]]) -> dict[str, list]:
    return {str(setor.get("codigo") or ""): list(setor.get("poligonos") or []) for setor in setores}


def _tocados(item: dict[str, Any], *, oficiais: dict[str, list] | None = None) -> list[dict[str, Any]]:
    linhas = []
    for cob in item.get("coberturas_inlog") or []:
        codigo = str(cob.get("codigo_rota") or "")
        aneis = [anel for anel in (oficiais or {}).get(codigo) or [] if anel and len(anel) >= 4]
        if not aneis:
            for pol in cob.get("poligonos") or []:
                if pol and len(pol) >= 4:
                    aneis = [_thin(pol, 80)]
                    break
        linhas.append(
            {
                "codigo": codigo,
                "setor": cob.get("setor") or "",
                "frequencia": cob.get("rota") or "",
                "turno": cob.get("turno") or "",
                "papel": cob.get("papel") or "PASSAGEM",
                "percentual": cob.get("percentual") or 0,
                "paradas": cob.get("paradas") or 0,
                "pulsos": cob.get("pulsos") if cob.get("pulsos") is not None else cob.get("pontos_no_setor") or 0,
                "minutos_dentro": cob.get("minutos_dentro") or 0,
                "minutos_parado": cob.get("minutos_parado") or 0,
                "minutos_permanencia": cob.get("minutos_permanencia") or 0,
                "km_coleta": cob.get("km_coleta"),
                "km_total": cob.get("km_total"),
                "confianca": cob.get("confianca") or "",
                "poligono": aneis[0] if aneis else [],
                "poligonos": aneis,
                "paradas_pontos": cob.get("paradas_pontos") or [],
            }
        )
    ordem = {"ATUANDO": 0, "APOIO": 1, "PERMANENCIA": 2, "PASSAGEM": 3}
    linhas.sort(key=lambda row: (ordem.get(str(row["papel"]), 9), -float(row["percentual"] or 0)))
    return linhas


def balanco_frota(setores: list[dict[str, Any]], frota: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uma linha por rota programada no dia: feita, parcial ou não feita."""
    por_codigo: dict[str, dict[str, Any]] = {}
    for item in frota:
        nome = _nome(item)
        for cob in item.get("coberturas_inlog") or []:
            codigo = str(cob.get("codigo_rota") or "")
            if not codigo or str(cob.get("papel") or "") not in {"ATUANDO", "APOIO"}:
                continue
            if str(cob.get("papel") or "") == "APOIO" and not _conta_como_apoio(cob):
                continue
            paradas = int(cob.get("paradas") or 0)
            if paradas <= 0 and not _conta_como_apoio(cob):
                continue
            linha = por_codigo.setdefault(
                codigo,
                {"percentual": 0.0, "paradas": 0, "caminhoes": [], "pontos": [], "placas": []},
            )
            pct = float(cob.get("percentual") or 0)
            papel = "atuou" if cob.get("papel") == "ATUANDO" else "apoio"
            linha["caminhoes"].append(f"{nome} {papel}")
            linha["paradas"] += paradas
            linha["percentual"] = max(linha["percentual"], pct)
            linha["pontos"].extend(cob.get("paradas_pontos") or [])
            placa = str(item.get("placa") or "")
            if placa:
                linha["placas"].append(placa)
    linhas = []
    for setor in setores:
        codigo = str(setor.get("codigo") or "")
        if not codigo:
            continue
        info = por_codigo.get(codigo)
        pct = float(info["percentual"]) if info else 0.0
        paradas = int(info["paradas"]) if info else 0
        if info and pct >= FEITA_MIN:
            situacao, detalhe = "Feita", "ruas oficiais cobertas"
        elif info and pct >= PARCIAL_MIN:
            situacao, detalhe = "Parcial", "parte das ruas ficou em aberto"
        elif info:
            situacao, detalhe = "Não feita", "parou na rota, mas cobriu pouco"
        else:
            situacao, detalhe = "Não feita", "nenhum caminhão coletou"
        linhas.append(
            {
                "codigo": codigo,
                "nome": setor.get("nome") or "",
                "turno": setor.get("turno") or "",
                "diario": bool(setor.get("diario")),
                "situacao": situacao,
                "detalhe": detalhe,
                "percentual": round(pct, 1),
                "paradas": paradas,
                "caminhoes": info["caminhoes"] if info else [],
                "pontos": info["pontos"] if info else [],
                "placas": info["placas"] if info else [],
                "poligonos": setor.get("poligonos") or [],
            }
        )
    ordem = {"Feita": 0, "Parcial": 1, "Não feita": 2}
    linhas.sort(
        key=lambda row: (
            2 if row["diario"] else 0 if row["turno"] == "DIURNO" else 1,
            ordem.get(row["situacao"], 9),
            row["codigo"],
        )
    )
    return linhas


def _faixa_agenda(row: dict[str, Any]) -> str:
    if row.get("diario"):
        return "Noturno diário"
    if row.get("turno") == "DIURNO":
        return "Diurno"
    if row.get("turno") == "NOTURNO":
        return "Noturno"
    return "Sem turno"


def _conta(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


def _texto_decisao(dia: date, balanco: list[dict[str, Any]]) -> list[str]:
    from .mapa import NOMES_DIA

    feitas = [r for r in balanco if r["situacao"] == "Feita"]
    parciais = [r for r in balanco if r["situacao"] == "Parcial"]
    nao = [r for r in balanco if r["situacao"] == "Não feita"]
    linhas = [
        f"Decisão da frota · {NOMES_DIA[dia.weekday()]} {dia.strftime('%d/%m/%Y')}",
        (
            f"{len(balanco)} rotas programadas neste dia. "
            f"{_conta(len(feitas), 'feita', 'feitas')}, "
            f"{_conta(len(parciais), 'parcial', 'parciais')}, "
            f"{_conta(len(nao), 'não feita', 'não feitas')}."
        ),
        "Feita é pelo menos 90% do eixo das ruas oficiais percorrido pelo trajeto casado na malha. Passar perto, sem entrar na via, não conta.",
    ]
    for faixa in ("Diurno", "Noturno", "Noturno diário", "Sem turno"):
        grupo = [r for r in balanco if _faixa_agenda(r) == faixa]
        if not grupo:
            continue
        n_feita = sum(1 for r in grupo if r["situacao"] == "Feita")
        n_parcial = sum(1 for r in grupo if r["situacao"] == "Parcial")
        n_nao = sum(1 for r in grupo if r["situacao"] == "Não feita")
        linhas.append(
            f"{faixa}: {_conta(n_feita, 'feita', 'feitas')}, "
            f"{_conta(n_parcial, 'parcial', 'parciais')}, "
            f"{_conta(n_nao, 'não feita', 'não feitas')}."
        )
        pronto = [r for r in grupo if r["situacao"] in {"Feita", "Parcial"}]
        if pronto:
            linhas.append(
                "Feito: "
                + "; ".join(
                    f"{r['codigo']} {r['nome']} {_fmt_pct(r['percentual'])} · {r['paradas']} paradas · {', '.join(r['caminhoes'])}"
                    for r in pronto
                )
            )
        fraco = [r for r in grupo if r["detalhe"] == "parou na rota, mas cobriu pouco"]
        if fraco:
            linhas.append(
                "Parou e não concluiu: "
                + "; ".join(f"{r['codigo']} {_fmt_pct(r['percentual'])} ({r['paradas']} paradas)" for r in fraco)
            )
        vazio = [r for r in grupo if r["detalhe"] == "nenhum caminhão coletou"]
        if vazio:
            linhas.append("Sem coleta: " + ", ".join(r["codigo"] for r in vazio) + ".")
    return linhas


def relatorio_frota(output_dir: Path, dia: date) -> dict[str, Any]:
    pasta = _pasta(output_dir, dia)
    resumo = _ler(pasta, "resumo.json") or {}
    setores = _ler(pasta, "setores.json") or []
    frota = _ler(pasta, "frota.json") or []
    _aplicar_apoio(frota)
    por_placa = {str(item.get("placa") or ""): item for item in frota}
    oficiais = _oficiais(setores)
    caminhoes = []
    for caminhao in resumo.get("caminhoes") or []:
        bruto = por_placa.get(str(caminhao.get("placa") or ""), {})
        caminhoes.append(
            {
                **caminhao,
                "papel": bruto.get("papel") or caminhao.get("papel") or "",
                "paradas": bruto.get("paradas") or caminhao.get("paradas") or 0,
                "minutos_parado": bruto.get("minutos_parado") or 0,
                "saida": bruto.get("saida"),
                "tocados": _tocados(bruto, oficiais=oficiais) if bruto else [],
                "feitos": [],
                "faltando": [],
            }
        )
    balanco = balanco_frota(setores, frota)
    texto = _texto_decisao(dia, balanco)
    texto.append(
        f"{len(caminhoes)} caminhão(ões) com pulso na cidade. "
        "Apoio é outro caminhão que entrou na rota e deixou pulsos suficientes. A rua só conta quando o trajeto casado passa por ela."
    )
    return {
        "tipo": "frota",
        "data": dia.isoformat(),
        "titulo": "Frota do dia",
        "texto": texto,
        "setores": setores,
        "caminhoes": caminhoes,
        "balanco": [
            {k: v for k, v in row.items() if k not in {"pontos", "poligonos", "placas"}}
            for row in balanco
        ],
        "frota": len(caminhoes),
        "media": resumo.get("media"),
    }


def _linhas_leves(trechos: list, max_pts: int = 24) -> list:
    saida = []
    for trecho in trechos or []:
        if trecho and len(trecho) >= 2:
            saida.append(_thin(trecho, max_pts))
    return saida


def _cobertura_da_rota(item: dict[str, Any], codigo: str) -> dict[str, Any] | None:
    for cob in item.get("coberturas_inlog") or []:
        if str(cob.get("codigo_rota") or "") == codigo:
            return cob
    return None


def _pontuar_setor(setor: dict[str, Any], consulta: str) -> int:
    bruto = (consulta or "").strip()
    if not bruto:
        return 0
    chave = norm_placa(bruto)
    texto = bruto.upper()
    codigo = norm_placa(str(setor.get("codigo") or ""))
    nome = str(setor.get("nome") or "").upper()
    if chave and codigo == chave:
        return 100
    if nome == texto:
        return 90
    if chave and len(chave) >= 2 and codigo.startswith(chave):
        return max(60, 80 - (len(codigo) - len(chave)) * 5)
    if len(texto) >= 3 and texto in nome:
        return max(40, 70 - min(len(nome) - len(texto), 20))
    return 0


def _escolher_setor(
    setores: list[dict[str, Any]], consulta: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    pontuados = [( _pontuar_setor(setor, consulta), setor) for setor in setores]
    pontuados = [(pontos, setor) for pontos, setor in pontuados if pontos > 0]
    pontuados.sort(key=lambda par: (-par[0], str(par[1].get("codigo") or "")))
    if not pontuados:
        return None, []
    if pontuados[0][0] >= 90:
        return pontuados[0][1], []
    melhores = [setor for pontos, setor in pontuados if pontos >= pontuados[0][0] - 8]
    if len(melhores) == 1:
        return melhores[0], []
    return None, melhores[:12]


def _achar_setor(setores: list[dict[str, Any]], consulta: str) -> dict[str, Any] | None:
    setor, _candidatos = _escolher_setor(setores, consulta)
    return setor


def _achar_caminhao(frota: list[dict[str, Any]], consulta: str) -> dict[str, Any] | None:
    chave = norm_placa(consulta)
    if not chave:
        return None
    for item in frota:
        if norm_placa(str(item.get("placa") or "")) == chave:
            return item
        if norm_placa(str(item.get("prefixo") or "")) == chave:
            return item
    return None


def _gps_na_rota(
    dia: date,
    codigo: str,
    aneis: list,
    placa: str,
    pontos_por_placa: dict[str, list],
) -> dict[str, Any]:
    from .gps import casar_tracos
    from .pdfmapa import _dentro_da_rota

    del codigo
    brutos = _dentro_da_rota(pontos_por_placa.get(placa) or [], aneis)
    return casar_tracos(brutos, dia=dia)


def relatorio_rota(output_dir: Path, dia: date, consulta: str, *, com_gps: bool = True) -> dict[str, Any]:
    pasta = _pasta(output_dir, dia)
    setores = _ler(pasta, "setores.json") or []
    frota = _ler(pasta, "frota.json") or []
    _aplicar_apoio(frota)
    setor, candidatos = _escolher_setor(setores, consulta)
    if candidatos:
        return {
            "tipo": "rota",
            "ambiguo": True,
            "titulo": "Rota",
            "texto": [f"Várias rotas combinam com «{consulta}». Escolha o código da rota."],
            "candidatos": [
                {
                    "codigo": item.get("codigo") or "",
                    "nome": item.get("nome") or "",
                    "frequencia": item.get("frequencia") or "",
                }
                for item in candidatos
            ],
            "caminhoes": [],
            "setores": [],
        }
    if setor is None:
        return {
            "tipo": "rota",
            "titulo": "Rota",
            "texto": [f"Nenhuma rota do dia combina com «{consulta}». Use o código, como 60DN, ou parte do nome."],
            "caminhoes": [],
            "setores": [],
        }
    codigo = str(setor.get("codigo") or "")
    pontos_salvos = _ler(pasta, "pontos.json") or {}
    presentes = []
    feitos_rota: list = []
    faltando_rota: list = []
    incertos_rota: list = []
    melhor_faltando = -1.0
    for item in frota:
        cob = next((c for c in _tocados(item, oficiais=_oficiais(setores)) if str(c.get("codigo") or "") == codigo), None)
        bruto = _cobertura_da_rota(item, codigo)
        if not cob or bruto is None:
            continue
        feitos = _linhas_leves(bruto.get("trechos_feitos"))
        faltando = _linhas_leves(bruto.get("trechos_faltando"))
        incertos = _linhas_leves(bruto.get("trechos_incertos"))
        papel = cob.get("papel") or "PASSAGEM"
        if papel in {"ATUANDO", "APOIO"}:
            feitos_rota.extend(feitos)
            incertos_rota.extend(incertos)
            pct_cob = float(cob.get("percentual") or 0)
            if pct_cob > melhor_faltando:
                melhor_faltando = pct_cob
                faltando_rota = faltando
        casado = (
            _gps_na_rota(dia, codigo, setor.get("poligonos") or [], str(item.get("placa") or ""), pontos_salvos)
            if com_gps and papel in {"ATUANDO", "APOIO"}
            else {}
        )
        gps = list(casado.get("confirmado") or [])
        gps_estimado = list(casado.get("estimado") or [])
        if not gps and not gps_estimado:
            gps = _linhas_leves(bruto.get("trechos_gps"), 800)
        presentes.append(
            {
                "placa": item.get("placa") or "",
                "prefixo": item.get("prefixo") or prefixo_da_placa(str(item.get("placa") or "")),
                "veiculo": item.get("veiculo") or "",
                "papel": papel,
                "percentual": cob.get("percentual") or 0,
                "paradas": cob.get("paradas") or 0,
                "pulsos": cob.get("pulsos") if cob.get("pulsos") is not None else cob.get("pontos_no_setor") or 0,
                "minutos_dentro": cob.get("minutos_dentro") or 0,
                "minutos_parado": cob.get("minutos_parado") or 0,
                "km_coleta": cob.get("km_coleta"),
                "km_total": cob.get("km_total"),
                "confianca": cob.get("confianca") or "",
                "feitos": feitos,
                "faltando": faltando,
                "incertos": incertos,
                "paradas_pontos": cob.get("paradas_pontos") or [],
                "gps": gps,
                "gps_estimado": gps_estimado,
                "gps_original": casado.get("original") or [],
                "gps_deslocamento": casado.get("deslocamento") or [],
                "casamento": casado.get("resumo") or {},
            }
        )
    if not faltando_rota:
        for item in frota:
            bruto = _cobertura_da_rota(item, codigo)
            if bruto:
                faltando_rota = _linhas_leves(bruto.get("trechos_faltando"))
                break
    coletando = [c for c in presentes if c["papel"] in {"ATUANDO", "APOIO"}]
    sobreposicao = len(coletando) >= 2
    if coletando:
        base = max(coletando, key=lambda c: float(c.get("percentual") or 0))
        pct = float(base.get("percentual") or 0)
    else:
        pct = 0.0
    conclusao = _conclusao(pct, bool(coletando))
    titulo = f"{codigo} — {setor.get('nome') or ''}".strip(" —")
    nome_conclusao = _nome_conclusao(conclusao)
    linhas = [
        f"Rota {titulo} · {dia.strftime('%d/%m/%Y')} · {setor.get('frequencia') or 'sem turno no mapa'}",
        f"Conclusão: {nome_conclusao} · {_fmt_pct(pct)} das ruas oficiais.",
    ]
    if not presentes:
        linhas.append("Nenhum caminhão da Segtrack entrou neste polígono no dia.")
    elif coletando:
        apoios = [c for c in coletando if c["papel"] == "APOIO"]
        linhas.append(
            f"{len(coletando)} caminhão(ões) trabalharam nesta rota. "
            + (
                "Apoio: " + ", ".join(_nome(c) for c in apoios) + "."
                if apoios
                else "Sem caminhão de apoio."
            )
        )
    elif any(c["papel"] == "PERMANENCIA" for c in presentes):
        linhas.append("Houve permanência neste polígono, sem coleta da rota. Isso não conta como rota feita.")
    else:
        linhas.append(
            f"{len(presentes)} caminhão(ões) passaram por este polígono sem coleta. A rota fica não feita."
        )
    for caminhao in sorted(presentes, key=lambda c: ({"ATUANDO": 0, "APOIO": 1, "PASSAGEM": 2}.get(c["papel"], 9), -float(c["percentual"] or 0))):
        papel = {
            "ATUANDO": "atuando na rota",
            "APOIO": "apoio nesta rota",
            "PERMANENCIA": "permaneceu sem coletar",
            "PASSAGEM": "só passagem",
        }.get(caminhao["papel"], caminhao["papel"])
        if caminhao["papel"] in {"ATUANDO", "APOIO"}:
            extra = ""
            if caminhao["papel"] == "APOIO":
                extra = f" · {int(caminhao.get('pulsos') or 0)} pulsos dentro da rota"
            linhas.append(
                f"{_nome(caminhao)} · {papel} · {_fmt_pct(caminhao['percentual'])} · "
                f"{caminhao['paradas']} paradas · {caminhao['minutos_parado']} min parado{extra}"
            )
        else:
            linhas.append(
                f"{_nome(caminhao)} · {papel} · {caminhao['paradas']} paradas · "
                f"{caminhao['minutos_parado']} min. Não entra na conclusão."
            )
    linhas.append(
        "O percentual conta só quem atuou ou fez apoio nesta rota. "
        "Apoio exige 45% das ruas e 15 minutos parado. Passagem fica de fora."
    )
    return {
        "tipo": "rota",
        "data": dia.isoformat(),
        "titulo": titulo,
        "texto": linhas,
        "conclusao": conclusao,
        "percentual": round(pct, 1),
        "sobreposicao": sobreposicao,
        "setores": [
            {
                "codigo": setor.get("codigo") or "",
                "nome": setor.get("nome") or "",
                "frequencia": setor.get("frequencia") or "",
                "poligonos": setor.get("poligonos") or [],
                "feitos": feitos_rota,
                "faltando": faltando_rota,
                "incertos": incertos_rota,
            }
        ],
        "caminhoes": presentes,
    }


def relatorio_placa(output_dir: Path, dia: date, consulta: str) -> dict[str, Any]:
    pasta = _pasta(output_dir, dia)
    frota = _ler(pasta, "frota.json") or []
    _aplicar_apoio(frota)
    resumo = _ler(pasta, "resumo.json") or {}
    item = _achar_caminhao(frota, consulta)
    if item is None:
        return {
            "tipo": "placa",
            "titulo": "Placa",
            "texto": [
                f"A placa «{consulta}» não teve pulso da Segtrack em Aparecida neste dia. "
                "Quando o rastreador for instalado e passar a pulsar na cidade, o relatório entra sozinho."
            ],
            "caminhoes": [],
            "setores": [],
        }
    placa = str(item.get("placa") or "")
    folha = next((c for c in (resumo.get("caminhoes") or []) if str(c.get("placa") or "") == placa), {})
    oficiais = _oficiais(_ler(pasta, "setores.json") or [])
    tocados = _tocados(item, oficiais=oficiais)
    atuando = [c for c in tocados if c["papel"] == "ATUANDO"]
    apoios = [c for c in tocados if c["papel"] == "APOIO"]
    permanencias = [c for c in tocados if c["papel"] == "PERMANENCIA"]
    passagens = [c for c in tocados if c["papel"] == "PASSAGEM"]
    saida = item.get("saida") or {}
    linhas = [
        f"{_nome(item)} · {dia.strftime('%d/%m/%Y')}",
        f"Saída: primeiro pulso às {saida.get('timestamp') or '—'}. O mapa começa nesse ponto.",
    ]
    if atuando:
        for rota in atuando:
            turno = str(rota.get("turno") or "").lower()
            linhas.append(
                f"Atuou no turno {turno or 'programado'}: {rota['setor']} · {_nome_conclusao(_conclusao(float(rota['percentual'] or 0), True))} · "
                f"{_fmt_pct(rota['percentual'])} · {rota['paradas']} paradas · {rota['minutos_parado']} min parado."
            )
    else:
        linhas.append("Não atuou em rota programada neste dia. O trajeto fica como passagem.")
    if apoios:
        linhas.append(
            "Apoio em: "
            + "; ".join(
                f"{c['setor']} · {_nome_conclusao(_conclusao(float(c['percentual'] or 0), True))} · {_fmt_pct(c['percentual'])} · {c['paradas']} paradas"
                for c in apoios
            )
        )
    if permanencias:
        linhas.append(
            "Permanência sem coleta em: "
            + "; ".join(f"{c['setor']} ({c['minutos_permanencia']} min)" for c in permanencias)
        )
    if passagens:
        linhas.append(
            f"Passou sem coletar em {len(passagens)} rota(s): "
            + ", ".join(str(c["codigo"] or c["setor"]) for c in passagens[:8])
        )
    linhas.append(
        "Cada rota abaixo teve coleta de verdade. Cruzar um polígono no caminho não entra nesta lista."
    )
    coleta = [c for c in tocados if c["papel"] in {"ATUANDO", "APOIO"}]
    return {
        "tipo": "placa",
        "data": dia.isoformat(),
        "titulo": _nome(item),
        "texto": linhas,
        "setores": [
            {
                "codigo": c["codigo"],
                "nome": c["setor"],
                "papel": c["papel"],
                "percentual": c["percentual"],
                "poligonos": c.get("poligonos") or ([c["poligono"]] if c.get("poligono") else []),
            }
            for c in coleta
            if c.get("poligonos") or c.get("poligono")
        ],
        "caminhoes": [
            {
                **folha,
                "placa": placa,
                "prefixo": item.get("prefixo") or "",
                "papel": item.get("papel") or "",
                "paradas": item.get("paradas") or 0,
                "minutos_parado": item.get("minutos_parado") or 0,
                "saida": saida,
                "paradas_pontos": item.get("paradas_pontos") or [],
                "gps": folha.get("gps") or [],
                "feitos": folha.get("feitos") or [],
                "faltando": folha.get("faltando") or [],
                "tocados": tocados,
            }
        ],
    }


def interpretar(texto: str) -> tuple[str, str]:
    bruto = (texto or "").strip()
    baixo = bruto.lower()
    if not bruto or baixo in {"frota", "completo", "cidade", "todos"}:
        return "frota", ""
    if baixo.startswith("rota "):
        return "rota", bruto.split(None, 1)[1].strip()
    if baixo.startswith("placa ") or baixo.startswith("caminhao ") or baixo.startswith("caminhão "):
        return "placa", bruto.split(None, 1)[1].strip()
    if norm_placa(bruto) in {norm_placa(p) for p in ("SDG1J76",)} or len(norm_placa(bruto)) == 7:
        return "placa", bruto
    return "rota", bruto


def montar(output_dir: Path, dia: date, tipo: str, consulta: str = "", *, com_gps: bool = True) -> dict[str, Any]:
    tipo = (tipo or "frota").lower()
    if tipo == "rota":
        return relatorio_rota(output_dir, dia, consulta, com_gps=com_gps)
    if tipo == "placa":
        return relatorio_placa(output_dir, dia, consulta)
    return relatorio_frota(output_dir, dia)
