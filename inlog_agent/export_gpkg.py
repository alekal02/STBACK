from __future__ import annotations

import json
import math
import sqlite3
import struct
from datetime import date
from pathlib import Path
from typing import Any

from pyproj import CRS
from shapely import to_wkb
from shapely.geometry import LineString, MultiLineString, Point

from .mapa_oficial import TO_UTM, aplicar_setores_oficiais, carregar_setores

SRS_ID = 31982


def _blob(geom, srs_id: int = SRS_ID) -> bytes:
    wkb = to_wkb(geom, byte_order=1)
    minx, miny, maxx, maxy = geom.bounds
    flags = 0x01 | (1 << 1)
    return (
        b"GP"
        + bytes([0, flags])
        + struct.pack("<i", srs_id)
        + struct.pack("<dddd", minx, maxx, miny, maxy)
        + wkb
    )


def _utm(ponto: dict[str, Any]) -> tuple[float, float] | None:
    lat = ponto.get("latitude")
    lon = ponto.get("longitude")
    if lat is None or lon is None:
        return None
    x, y = TO_UTM.transform(float(lon), float(lat))
    return (x, y)


def _linha_gps(pontos: list[dict[str, Any]]):
    trechos: list[list[tuple[float, float]]] = []
    atual: list[tuple[float, float]] = []
    anterior = None
    for ponto in pontos:
        xy = _utm(ponto)
        if xy is None:
            continue
        if anterior is not None and math.hypot(xy[0] - anterior[0], xy[1] - anterior[1]) > 2500:
            if len(atual) >= 2:
                trechos.append(atual)
            atual = [xy]
        else:
            atual.append(xy)
        anterior = xy
    if len(atual) >= 2:
        trechos.append(atual)
    if not trechos:
        return None
    if len(trechos) == 1:
        return LineString(trechos[0])
    return MultiLineString(trechos)


def _iniciar(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA application_id = 1196444487")
    con.execute("PRAGMA user_version = 10300")
    con.execute(
        """
        CREATE TABLE gpkg_spatial_ref_sys (
          srs_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL PRIMARY KEY,
          organization TEXT NOT NULL,
          organization_coordsys_id INTEGER NOT NULL,
          definition TEXT NOT NULL,
          description TEXT
        )
        """
    )
    wgs = CRS.from_epsg(4326).to_wkt()
    utm = CRS.from_epsg(SRS_ID).to_wkt()
    con.executemany(
        "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
        [
            ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
            ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
            ("WGS 84", 4326, "EPSG", 4326, wgs, None),
            ("SIRGAS 2000 / UTM zone 22S", SRS_ID, "EPSG", SRS_ID, utm, "Aparecida de Goiânia"),
        ],
    )
    con.execute(
        """
        CREATE TABLE gpkg_contents (
          table_name TEXT NOT NULL PRIMARY KEY,
          data_type TEXT NOT NULL,
          identifier TEXT UNIQUE,
          description TEXT DEFAULT '',
          last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
          srs_id INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE gpkg_geometry_columns (
          table_name TEXT NOT NULL,
          column_name TEXT NOT NULL,
          geometry_type_name TEXT NOT NULL,
          srs_id INTEGER NOT NULL,
          z TINYINT NOT NULL,
          m TINYINT NOT NULL,
          PRIMARY KEY (table_name, column_name)
        )
        """
    )


def _registrar_camada(
    con: sqlite3.Connection,
    tabela: str,
    tipo: str,
    bounds: tuple[float, float, float, float] | None,
    descricao: str,
) -> None:
    minx = miny = maxx = maxy = None
    if bounds:
        minx, miny, maxx, maxy = bounds
    con.execute(
        "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (tabela, "features", tabela, descricao, minx, miny, maxx, maxy, SRS_ID),
    )
    con.execute(
        "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,0,0)",
        (tabela, "geom", tipo, SRS_ID),
    )


def _bounds(geoms: list[Any]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        minx, miny, maxx, maxy = geom.bounds
        xs.extend([minx, maxx])
        ys.extend([miny, maxy])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def escrever_gpkg(destino: Path, dia: date, rotas: list[dict[str, Any]], *, fonte: str = "argos") -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        destino.unlink()
    setores = carregar_setores(dia)
    con = sqlite3.connect(str(destino))
    try:
        _iniciar(con)
        linhas = []
        for item in rotas:
            geom = _linha_gps(item.get("pontos") or [])
            linhas.append((item, geom))
        con.execute(
            """
            CREATE TABLE percursos (
              fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
              geom BLOB,
              veiculo TEXT,
              placa TEXT,
              data TEXT,
              setor TEXT,
              rota TEXT,
              percentual REAL,
              km_gps REAL,
              km_coleta REAL,
              km_total REAL,
              pacmans REAL,
              pontos INTEGER
            )
            """
        )
        geoms_linha = [geom for _, geom in linhas if geom is not None]
        for item, geom in linhas:
            cob = (item.get("coberturas_inlog") or [{}])[0]
            con.execute(
                "INSERT INTO percursos (geom, veiculo, placa, data, setor, rota, percentual, km_gps, km_coleta, km_total, pacmans, pontos) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _blob(geom) if geom is not None else None,
                    item.get("veiculo") or item.get("placa"),
                    item.get("placa"),
                    dia.isoformat(),
                    item.get("setor") or cob.get("setor"),
                    item.get("rota") or cob.get("rota"),
                    item.get("percentual_concluido"),
                    item.get("km_dia"),
                    item.get("km_coleta") or cob.get("km_coleta"),
                    item.get("km_total") or cob.get("km_total"),
                    item.get("qtd_pacmans") or cob.get("qtd_pacmans"),
                    item.get("total_pontos") or len(item.get("pontos") or []),
                ),
            )
        _registrar_camada(con, "percursos", "MULTILINESTRING", _bounds(geoms_linha), f"Percurso GPS {fonte} do dia")

        con.execute(
            """
            CREATE TABLE pontos (
              fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
              geom BLOB,
              veiculo TEXT,
              placa TEXT,
              data TEXT,
              horario TEXT,
              velocidade REAL
            )
            """
        )
        geoms_pt = []
        for item in rotas:
            placa = item.get("placa")
            veiculo = item.get("veiculo") or placa
            for ponto in item.get("pontos") or []:
                xy = _utm(ponto)
                if xy is None:
                    continue
                geom = Point(xy)
                geoms_pt.append(geom)
                con.execute(
                    "INSERT INTO pontos (geom, veiculo, placa, data, horario, velocidade) VALUES (?,?,?,?,?,?)",
                    (_blob(geom), veiculo, placa, dia.isoformat(), ponto.get("timestamp"), ponto.get("velocidade")),
                )
        _registrar_camada(con, "pontos", "POINT", _bounds(geoms_pt), f"Pontos GPS {fonte}")

        planejadas = [(item, _linha_gps(item.get("rota_planejada") or [])) for item in rotas]
        if any(geom is not None for _, geom in planejadas):
            con.execute(
                """
                CREATE TABLE planejadas (
                  fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                  geom BLOB,
                  veiculo TEXT,
                  placa TEXT,
                  data TEXT,
                  setor TEXT,
                  rota TEXT
                )
                """
            )
            geoms_pl = [geom for _, geom in planejadas if geom is not None]
            for item, geom in planejadas:
                if geom is None:
                    continue
                con.execute(
                    "INSERT INTO planejadas (geom, veiculo, placa, data, setor, rota) VALUES (?,?,?,?,?,?)",
                    (
                        _blob(geom),
                        item.get("veiculo") or item.get("placa"),
                        item.get("placa"),
                        dia.isoformat(),
                        item.get("setor"),
                        item.get("rota"),
                    ),
                )
            _registrar_camada(con, "planejadas", "MULTILINESTRING", _bounds(geoms_pl), "Rotas planejadas Inlog")

        con.execute(
            """
            CREATE TABLE setores (
              fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
              geom BLOB,
              codigo TEXT,
              nome TEXT,
              frequencia TEXT,
              moradias REAL,
              km_oficial REAL
            )
            """
        )
        geoms_st = []
        for setor in setores:
            geom = setor["geom"]
            geoms_st.append(geom)
            con.execute(
                "INSERT INTO setores (geom, codigo, nome, frequencia, moradias, km_oficial) VALUES (?,?,?,?,?,?)",
                (
                    _blob(geom),
                    setor.get("codigo"),
                    setor.get("nome"),
                    setor.get("frequencia"),
                    setor.get("moradias"),
                    setor.get("km_oficial"),
                ),
            )
        _registrar_camada(con, "setores", "MULTIPOLYGON", _bounds(geoms_st), "Setores oficiais do dia")
        con.commit()
    finally:
        con.close()
    return destino


def gerar_gpkg_inlog(pasta: Path) -> Path:
    pasta = Path(pasta)
    arquivo = pasta / "inlog-rotas.gpkg"
    rotas_json = pasta / "rotas.json"
    if not rotas_json.is_file():
        raise FileNotFoundError(f"Não achei as rotas Inlog em {rotas_json}")
    dados = json.loads(rotas_json.read_text(encoding="utf-8"))
    dia = date.fromisoformat(str(dados.get("data") or pasta.name))
    rotas = list(dados.get("rotas") or [])
    oficiais = carregar_setores(dia)
    if oficiais:
        aplicar_setores_oficiais(rotas, oficiais, dia, usar_planejado_inlog=False)
    return escrever_gpkg(arquivo, dia, rotas, fonte="inlog")


def gerar_gpkg_argos(pasta: Path) -> Path:
    pasta = Path(pasta)
    arquivo = pasta / "argos-rotas.gpkg"
    rotas_json = pasta / "rotas.json"
    if not rotas_json.is_file():
        raise FileNotFoundError(f"Não achei o percurso Argos em {rotas_json}")
    dados = json.loads(rotas_json.read_text(encoding="utf-8"))
    dia = date.fromisoformat(str(dados.get("data") or pasta.name))
    rotas = list(dados.get("rotas") or [])
    oficiais = carregar_setores(dia)
    if oficiais:
        aplicar_setores_oficiais(rotas, oficiais, dia)
    return escrever_gpkg(arquivo, dia, rotas, fonte="argos")


def gerar_gpkg_segtrack(pasta: Path) -> Path:
    pasta = Path(pasta)
    arquivo = pasta / "segtrack-rotas.gpkg"
    rotas_json = pasta / "rotas.json"
    if not rotas_json.is_file():
        raise FileNotFoundError(f"Não achei o percurso Segtrack em {rotas_json}")
    dados = json.loads(rotas_json.read_text(encoding="utf-8"))
    dia = date.fromisoformat(str(dados.get("data") or pasta.name))
    rotas = list(dados.get("rotas") or [])
    oficiais = carregar_setores(dia)
    if oficiais:
        aplicar_setores_oficiais(rotas, oficiais, dia)
    return escrever_gpkg(arquivo, dia, rotas, fonte="segtrack")
