from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from shapely import from_wkb
from shapely.geometry import LineString, Point
from pyproj import Transformer

from .config import ROOT

GPKG_LOCAL = ROOT / "data" / "mapa-coleta-aparecida.gpkg"
ENVELOPE = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
ALIAS_DIA = {0: "SEG", 1: "TER", 2: "QUA", 3: "QUI", 4: "SEX", 5: "SAB", 6: "DOM"}
TO_WGS = Transformer.from_crs(31982, 4326, always_xy=True)
TO_UTM = Transformer.from_crs(4326, 31982, always_xy=True)
_CODIGO = re.compile(r"\b(\d{2}[A-Z]{2,4})\b")


def caminho_gpkg() -> Path | None:
    return GPKG_LOCAL if GPKG_LOCAL.is_file() else None


def _sincronizar_gpkg() -> Path | None:
    return caminho_gpkg()


def _gpkg_geom(blob: bytes):
    flags = blob[3]
    header = 8 + ENVELOPE.get((flags >> 1) & 7, 0)
    return from_wkb(blob[header:])


def _quote(nome: str) -> str:
    return '"' + (nome or "").replace('"', '""') + '"'


def _camadas(con: sqlite3.Connection) -> list[tuple[str, str]]:
    try:
        return [
            (str(tabela), str(tipo or ""))
            for tabela, tipo in con.execute(
                "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns"
            )
        ]
    except sqlite3.Error:
        return []


def _colunas(con: sqlite3.Connection, tabela: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({_quote(tabela)})")]


def _achar_coluna(cols: list[str], *nomes: str) -> str | None:
    mapa = {col.upper(): col for col in cols}
    for nome in nomes:
        if nome.upper() in mapa:
            return mapa[nome.upper()]
    for col in cols:
        chave = col.upper()
        if any(nome.upper() in chave for nome in nomes):
            return col
    return None


def _tabela_setores(con: sqlite3.Connection) -> str | None:
    for tabela, tipo in _camadas(con):
        if "POLYGON" not in tipo.upper():
            continue
        cols = [c.upper() for c in _colunas(con, tabela)]
        if any("CODIGO" in c or "CODINOME" in c for c in cols):
            return tabela
    nomes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "c" in nomes:
        return "c"
    return None


def _tabela_vias(con: sqlite3.Connection) -> str | None:
    for tabela, tipo in _camadas(con):
        if "LINE" in tipo.upper():
            return tabela
    return None


def _freq_do_dia(freq: str, dia: date) -> bool:
    blob = (freq or "").upper().replace("Á", "A").replace("É", "E").replace("Í", "I")
    if "DIARIO" in blob or "DIARIA" in blob:
        return True
    alias = ALIAS_DIA[dia.weekday()]
    return alias in blob


def _km_oficial(valor: Any) -> float | None:
    if valor in (None, ""):
        return None
    numero = float(valor)
    if numero > 200:
        numero = numero / 1000.0
    return numero


def _contorno_wgs(geom) -> list[dict[str, Any]]:
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    pontos = []
    for poly in polys:
        for coord in poly.exterior.coords:
            lon, lat = TO_WGS.transform(coord[0], coord[1])
            pontos.append({"latitude": lat, "longitude": lon, "fonte": "setor-oficial"})
    return pontos


def _poligonos_wgs(geom) -> list[list[list[float]]]:
    polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    aneis = []
    for poly in polys:
        anel = []
        for coord in poly.exterior.coords:
            lon, lat = TO_WGS.transform(coord[0], coord[1])
            anel.append([lat, lon])
        if len(anel) >= 4:
            aneis.append(anel)
    return aneis


def _codigo_setor(texto: str) -> str:
    achado = _CODIGO.search((texto or "").upper())
    return achado.group(1) if achado else ""


def eh_area_dificil(codigo: str) -> bool:
    """Polígono oficial de difícil acesso. Não entra na grade nem na cobertura."""
    bruto = (codigo or "").strip().upper()
    if not bruto.startswith("DIFIC") and not bruto.startswith("DIF"):
        return False
    return "ACESSO" in bruto


def geometrias_inlog(dia: date | None = None) -> dict[str, list[dict[str, Any]]]:
    base = ROOT / "output" / "rotas"
    if not base.exists():
        return {}
    pastas = [p for p in base.iterdir() if p.is_dir() and p.name != "comparacao"]
    pastas.sort(key=lambda p: p.name, reverse=True)
    if dia is not None:
        hoje = base / dia.isoformat()
        pastas = [hoje] + [p for p in pastas if p != hoje]
    mapa: dict[str, list[dict[str, Any]]] = {}
    for pasta in pastas:
        arquivo = pasta / "rotas.json"
        if not arquivo.is_file():
            continue
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in dados.get("rotas") or []:
            pontos = item.get("rota_planejada") or []
            if not pontos:
                continue
            codigo = _codigo_setor(str(item.get("setor") or ""))
            if codigo and codigo not in mapa:
                mapa[codigo] = pontos
        if dia is not None and pasta.name == dia.isoformat() and mapa:
            break
    return mapa


def carregar_vias() -> list[Any]:
    arquivo = _sincronizar_gpkg()
    if not arquivo:
        return []
    con = sqlite3.connect(str(arquivo))
    tabela = _tabela_vias(con)
    if not tabela:
        con.close()
        return []
    cols = _colunas(con, tabela)
    geom_col = _achar_coluna(cols, "geom", "geometry") or "geom"
    linhas = []
    for (blob,) in con.execute(f"SELECT {_quote(geom_col)} FROM {_quote(tabela)}"):
        if not blob:
            continue
        try:
            geom = _gpkg_geom(blob)
        except Exception:
            continue
        if geom is None or geom.is_empty:
            continue
        partes = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for parte in partes:
            if parte.geom_type == "LineString" and len(parte.coords) >= 2:
                linhas.append(parte)
    con.close()
    return linhas


def _vias_wgs(linhas: list[Any], limite: int = 80) -> list[list[list[float]]]:
    saida: list[list[list[float]]] = []
    for linha in linhas[:limite]:
        try:
            simpl = linha.simplify(8, preserve_topology=False)
        except Exception:
            simpl = linha
        coords = list(simpl.coords)
        if len(coords) > 40:
            coords = coords[::2]
        anel = []
        for x, y in coords:
            lon, lat = TO_WGS.transform(x, y)
            anel.append([lat, lon])
        if len(anel) >= 2:
            saida.append(anel)
    return saida


def _anexar_vias(setores: list[dict[str, Any]]) -> None:
    from shapely.strtree import STRtree

    malha = carregar_vias()
    if not malha:
        return
    arvore = STRtree(malha)
    for setor in setores:
        geom = setor.get("geom")
        if geom is None:
            setor["ruas"] = []
            setor["vias"] = []
            continue
        zona = geom.buffer(20)
        recorte = []
        try:
            indices = list(arvore.query(zona, predicate="intersects"))
        except TypeError:
            indices = [i for i, linha in enumerate(malha) if linha.intersects(zona)]
        for idx in indices:
            linha = malha[int(idx)]
            inter = linha.intersection(zona)
            if inter.is_empty:
                continue
            partes = list(inter.geoms) if inter.geom_type == "MultiLineString" else [inter]
            for parte in partes:
                if parte.geom_type == "LineString" and parte.length >= 10:
                    recorte.append(parte)
        setor["ruas"] = recorte
        setor["vias"] = _vias_wgs(recorte)


def listar_rotas_oficiais(dia: date | None = None) -> list[dict[str, str]]:
    """Rotas do GPKG. Se passar o dia, só a grade daquele weekday."""
    arquivo = _sincronizar_gpkg()
    if not arquivo:
        return []
    con = sqlite3.connect(str(arquivo))
    tabela = _tabela_setores(con)
    if not tabela:
        con.close()
        return []
    cols = _colunas(con, tabela)
    col_codigo = _achar_coluna(cols, "NOVO_CODIGO", "CODIGO")
    col_nome = _achar_coluna(cols, "NOVO_CODINOME", "CODINOME", "NOME")
    col_freq = _achar_coluna(cols, "FREQUÊNCIA", "FREQUENCIA")
    col_completo = _achar_coluna(cols, "CODIGO COMPLETO", "COMPLETO")
    campos = [col_codigo, col_nome, col_freq, col_completo]
    select = ", ".join(_quote(c) if c else "NULL" for c in campos)
    saida: list[dict[str, str]] = []
    vistos: set[str] = set()
    for codigo, nome, freq, completo in con.execute(f"SELECT {select} FROM {_quote(tabela)}"):
        codigo = str(codigo or "").strip()
        if not codigo or eh_area_dificil(codigo):
            continue
        freq_txt = str(freq or "").strip()
        if dia is not None and not _freq_do_dia(freq_txt, dia):
            continue
        chave = codigo.upper()
        if chave in vistos:
            continue
        vistos.add(chave)
        rotulo = str(completo or "").strip() or (f"{codigo} - {nome}" if nome else codigo)
        saida.append(
            {
                "codigo": codigo,
                "nome": str(nome or "").strip(),
                "frequencia": freq_txt,
                "rotulo": rotulo,
            }
        )
    con.close()
    saida.sort(key=lambda row: str(row.get("codigo") or ""))
    return saida


def carregar_setores(dia: date | None = None) -> list[dict[str, Any]]:
    arquivo = _sincronizar_gpkg()
    if not arquivo:
        return []
    con = sqlite3.connect(str(arquivo))
    tabela = _tabela_setores(con)
    if not tabela:
        con.close()
        return []
    cols = _colunas(con, tabela)
    geom_col = _achar_coluna(cols, "geom", "geometry") or "geom"
    col_codigo = _achar_coluna(cols, "NOVO_CODIGO", "CODIGO")
    col_nome = _achar_coluna(cols, "NOVO_CODINOME", "CODINOME", "NOME")
    col_freq = _achar_coluna(cols, "FREQUÊNCIA", "FREQUENCIA")
    col_moradias = _achar_coluna(cols, "MORADIAS")
    col_km = _achar_coluna(cols, "KM DO SETOR", "KM")
    col_completo = _achar_coluna(cols, "CODIGO COMPLETO", "COMPLETO")
    campos = [geom_col, col_codigo, col_nome, col_freq, col_moradias, col_km, col_completo]
    select = ", ".join(_quote(c) if c else "NULL" for c in campos)
    setores = []
    for blob, codigo, nome, freq, moradias, km, completo in con.execute(
        f"SELECT {select} FROM {_quote(tabela)}"
    ):
        codigo = str(codigo or "").strip()
        if eh_area_dificil(codigo):
            continue
        if dia is not None and not _freq_do_dia(str(freq or ""), dia):
            continue
        if not blob:
            continue
        geom = _gpkg_geom(blob)
        oficial = _km_oficial(km)
        setores.append(
            {
                "codigo": codigo,
                "nome": str(nome or "").strip(),
                "frequencia": str(freq or "").strip(),
                "moradias": moradias,
                "km_oficial": oficial,
                "completo": str(completo or "").strip() or (f"{codigo} - {nome}" if codigo else str(nome or "")),
                "geom": geom,
                "contorno": _contorno_wgs(geom),
                "poligonos": _poligonos_wgs(geom),
            }
        )
    con.close()
    _anexar_vias(setores)
    return setores


def _id_area(nome: str, aneis: list[list[list[float]]], usados: set[str]) -> str:
    """Identidade estável do polígono. Os dois Chácara São Pedro não se misturam."""
    lat, lon = aneis[0][0]
    base = f"{nome}|{float(lat):.5f}|{float(lon):.5f}"
    ident = base
    extra = 2
    while ident in usados:
        ident = f"{base}#{extra}"
        extra += 1
    usados.add(ident)
    return ident


def carregar_areas_dificil() -> list[dict[str, Any]]:
    """Os 11 polígonos oficiais. Não anexa vias e não alimenta a cobertura."""
    arquivo = _sincronizar_gpkg()
    if not arquivo:
        return []
    con = sqlite3.connect(str(arquivo))
    try:
        tabela = _tabela_setores(con)
        if not tabela:
            return []
        cols = _colunas(con, tabela)
        geom_col = _achar_coluna(cols, "geom", "geometry") or "geom"
        col_codigo = _achar_coluna(cols, "NOVO_CODIGO", "CODIGO")
        col_nome = _achar_coluna(cols, "NOVO_CODINOME", "CODINOME", "NOME")
        col_freq = _achar_coluna(cols, "FREQUÊNCIA", "FREQUENCIA")
        col_completo = _achar_coluna(cols, "CODIGO COMPLETO", "COMPLETO")
        campos = [geom_col, col_codigo, col_nome, col_freq, col_completo]
        select = ", ".join(_quote(c) if c else "NULL" for c in campos)
        areas: list[dict[str, Any]] = []
        usados: set[str] = set()
        for blob, codigo, nome, freq, completo in con.execute(f"SELECT {select} FROM {_quote(tabela)}"):
            codigo = str(codigo or "").strip()
            if not eh_area_dificil(codigo) or not blob:
                continue
            geom = _gpkg_geom(blob)
            aneis = _poligonos_wgs(geom)
            if not aneis:
                continue
            bairro = str(nome or "").strip()
            areas.append(
                {
                    "id": _id_area(bairro or codigo, aneis, usados),
                    "codigo": codigo,
                    "nome": bairro,
                    "frequencia": str(freq or "").strip(),
                    "completo": str(completo or "").strip() or (f"{codigo} - {bairro}" if bairro else codigo),
                    "poligonos": aneis,
                }
            )
        return areas
    finally:
        con.close()


_MALHA_AREAS: dict[str, dict[str, Any]] | None = None


def _linhas_no_poligono(geom) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo == "LineString":
        return [geom] if len(geom.coords) >= 2 and geom.length >= 10 else []
    if tipo in {"MultiLineString", "GeometryCollection"}:
        saida: list[Any] = []
        for parte in geom.geoms:
            saida.extend(_linhas_no_poligono(parte))
        return saida
    return []


def malha_areas_dificil() -> dict[str, dict[str, Any]]:
    """Vias oficiais recortadas por polígono. Não entra na malha das rotas."""
    global _MALHA_AREAS
    if _MALHA_AREAS is not None:
        return _MALHA_AREAS
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    saida: dict[str, dict[str, Any]] = {}
    malha = carregar_vias()
    arvore = STRtree(malha) if malha else None
    for area in carregar_areas_dificil():
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
            continue
        geom = unary_union(polys) if len(polys) > 1 else polys[0]
        recorte: list[Any] = []
        if arvore is not None and not geom.is_empty:
            try:
                indices = list(arvore.query(geom, predicate="intersects"))
            except TypeError:
                indices = [i for i, linha in enumerate(malha) if linha.intersects(geom)]
            for idx in indices:
                linha = malha[int(idx)]
                inter = linha.intersection(geom)
                recorte.extend(_linhas_no_poligono(inter))
        saida[str(area.get("id") or "")] = {"geom": geom, "ruas": recorte}
    _MALHA_AREAS = saida
    return saida


def recortar_se_houver(pontos: list[dict[str, Any]], geom) -> list[dict[str, Any]]:
    from .cobertura import recortar_pontos

    recorte = recortar_pontos(pontos, geom, 120)
    return recorte or pontos


def km_no_setor(pontos: list[dict[str, Any]], geom, buffer_m: float = 80) -> tuple[float, int]:
    zona = geom.buffer(buffer_m)
    total = 0.0
    dentro = 0
    anterior = None
    for ponto in pontos:
        lon = ponto.get("longitude")
        lat = ponto.get("latitude")
        if lon is None or lat is None:
            continue
        x, y = TO_UTM.transform(float(lon), float(lat))
        atual = (x, y)
        if zona.covers(Point(x, y)):
            dentro += 1
        if anterior is not None:
            dist = math.hypot(x - anterior[0], y - anterior[1])
            if 0 < dist < 2500:
                inter = LineString([anterior, atual]).intersection(zona)
                if not inter.is_empty:
                    total += inter.length
        anterior = atual
    return total / 1000.0, dentro


def aplicar_setores_oficiais(
    rotas: list[dict[str, Any]],
    setores: list[dict[str, Any]],
    dia: date | None = None,
    *,
    usar_planejado_inlog: bool = True,
) -> None:
    if not setores:
        return
    from .cobertura import avaliar_cobertura, recortar_pontos
    from .verificacao import coberturas_executadas

    linhas_inlog = geometrias_inlog(dia) if usar_planejado_inlog else {}

    for item in rotas:
        pontos = item.get("pontos_dia") or item.get("pontos") or []
        coberturas = []
        melhor = None
        melhor_cob: dict[str, Any] | None = None
        melhor_pct = -1.0
        for setor in setores:
            clip = recortar_pontos(pontos, setor["geom"], 90)
            if len(clip) < 8:
                continue
            cob = avaliar_cobertura(
                linhas_inlog.get(setor["codigo"]) or [],
                pontos,
                geom_setor=setor["geom"],
                km_oficial=setor.get("km_oficial"),
                ruas=setor.get("ruas") or [],
            )
            km_feito = float(cob.get("km_coleta") or 0)
            n_pts = int(cob.get("pontos_no_setor") or 0)
            if n_pts < 8 and km_feito < 0.4:
                continue
            row = {
                "codigo_rota": setor["codigo"],
                "setor": setor["completo"] or f"{setor['codigo']} - {setor['nome']}",
                "rota": setor["frequencia"],
                "km_coleta": cob.get("km_coleta"),
                "km_total": setor.get("km_oficial") or cob.get("km_total"),
                "km_faltando": cob.get("km_faltando"),
                "qtd_pacmans": setor.get("moradias"),
                "percentual": cob.get("percentual") or 0,
                "pontos_no_setor": n_pts,
                "veredicto": cob.get("veredicto"),
                "confianca": cob.get("confianca"),
                "metodo": cob.get("metodo"),
                "veiculo_inlog": item.get("inlog") or "",
                "trechos_feitos": cob.get("trechos_feitos") or [],
                "trechos_faltando": cob.get("trechos_faltando") or [],
                "trechos_planejados": cob.get("trechos_planejados") or [],
                "trechos_gps": cob.get("trechos_gps") or [],
                "poligonos": setor.get("poligonos") or [],
                "vias": setor.get("vias") or [],
                "detectado_fora_da_grade": bool(setor.get("detectado_fora_da_grade")),
                "motivo_agente": setor.get("motivo_agente") or "",
            }
            coberturas.append(row)
            pct = float(row["percentual"] or 0)
            if pct > melhor_pct or (pct == melhor_pct and km_feito > float((melhor_cob or {}).get("km_coleta") or 0)):
                melhor_pct = pct
                melhor = setor
                melhor_cob = row
        coberturas.sort(key=lambda row: float(row.get("percentual") or 0), reverse=True)
        item["coberturas_inlog"] = coberturas
        visiveis = coberturas_executadas(item, max_n=5)
        if visiveis:
            melhor_cob = visiveis[0]
            codigo = str(melhor_cob.get("codigo_rota") or "")
            for setor in setores:
                if str(setor.get("codigo")) == codigo:
                    melhor = setor
                    break
        if visiveis and melhor_cob and melhor:
            item["percentual_concluido"] = melhor_cob.get("percentual") or 0
            item["veredicto"] = melhor_cob.get("execucao") or melhor_cob.get("veredicto")
            item["confianca"] = melhor_cob.get("confianca")
            item["setor"] = melhor["completo"] or f"{melhor['codigo']} - {melhor['nome']}"
            item["rota"] = melhor["frequencia"]
            item["codigo_rota"] = melhor["codigo"]
            item["km_coleta"] = melhor_cob.get("km_coleta")
            item["km_total"] = melhor.get("km_oficial")
            item["km_faltando"] = melhor_cob.get("km_faltando")
            item["qtd_pacmans"] = melhor.get("moradias")
            item["trechos_feitos"] = [t for row in visiveis for t in (row.get("trechos_feitos") or [])]
            item["trechos_faltando"] = [t for row in visiveis for t in (row.get("trechos_faltando") or [])]
            item["trechos_planejados"] = [t for row in visiveis for t in (row.get("trechos_planejados") or [])]
            item["trechos_gps"] = [t for row in visiveis for t in (row.get("trechos_gps") or [])]
            item["poligonos"] = [anel for row in visiveis for anel in (row.get("poligonos") or [])]
            item["vias"] = [linha for row in visiveis for linha in (row.get("vias") or [])]
            item["rota_planejada"] = linhas_inlog.get(str(melhor.get("codigo") or ""), [])
            item["pontos_dia"] = pontos
            item["pontos"] = pontos
            item["veiculo_inlog"] = item.get("inlog") or ""
        else:
            item["percentual_concluido"] = 0.0
            item["veredicto"] = "SEM RASTREIO" if not pontos else "SEM COLETA"
            item["confianca"] = "baixa"
            item["poligonos"] = []
            item["coberturas_inlog"] = coberturas
            item["codigo_rota"] = None
            item["km_coleta"] = item.get("km_dia")
            item["km_total"] = item.get("km_dia")
            item["km_faltando"] = None
            item["qtd_pacmans"] = None
            item["rota_planejada"] = []
            item["trechos_feitos"] = []
            item["trechos_faltando"] = []
            item["trechos_planejados"] = []
            item["trechos_gps"] = []
            item["pontos_dia"] = pontos
