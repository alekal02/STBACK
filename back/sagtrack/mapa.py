from __future__ import annotations

import math
import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from shapely import from_wkb
from shapely.geometry import LineString, Point
from pyproj import Transformer

from .config import ROOT

GPKG_CANDIDATOS = (
    ROOT / "data" / "mapa-coleta-aparecida.gpkg",
    Path.home() / "Downloads" / "Pacote de camadas Coleta RSD Aparecida.gpkg",
    Path.home() / "Downloads" / "MAPA COLETA RV02 - COM NUM MORADIAS E KM (1).gpkg",
    Path.home() / "Downloads" / "MAPA COLETA RV02 - COM NUM MORADIAS E KM.gpkg",
)
GPKG_LOCAL = ROOT / "data" / "mapa-coleta-aparecida.gpkg"
ENVELOPE = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
ALIAS_DIA = {0: "SEG", 1: "TER", 2: "QUA", 3: "QUI", 4: "SEX", 5: "SAB", 6: "DOM"}
NOMES_DIA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
DIAS_ORDEM = ("SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM")
TO_WGS = Transformer.from_crs(31982, 4326, always_xy=True)
TO_UTM = Transformer.from_crs(4326, 31982, always_xy=True)
_CODIGO = re.compile(r"\b(\d{2}[A-Z]{2})\b")


def caminho_gpkg() -> Path | None:
    for caminho in GPKG_CANDIDATOS:
        if caminho.is_file():
            return caminho
    return None


def _sincronizar_gpkg() -> Path | None:
    origem = caminho_gpkg()
    if not origem:
        return None
    GPKG_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    if origem.resolve() != GPKG_LOCAL.resolve():
        shutil.copy2(origem, GPKG_LOCAL)
    return GPKG_LOCAL if GPKG_LOCAL.is_file() else origem


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


def interpretar_frequencia(freq: str) -> dict[str, Any]:
    """A coluna FREQUÊNCIA traz os dias e o turno, como 'TER/QUI/SAB - DIURNO'."""
    bruto = (freq or "").strip()
    blob = (
        bruto.upper()
        .replace("Á", "A")
        .replace("É", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ã", "A")
    )
    turno = "NOTURNO" if "NOTURNO" in blob else "DIURNO" if "DIURNO" in blob else ""
    diario = "DIARIO" in blob or "DIARIA" in blob
    if diario:
        dias = list(DIAS_ORDEM)
    else:
        tokens = set(re.findall(r"[A-Z]+", blob))
        dias = [dia for dia in DIAS_ORDEM if dia in tokens]
    return {"frequencia": bruto, "dias": dias, "turno": turno, "diario": diario}


def _freq_do_dia(freq: str, dia: date) -> bool:
    return ALIAS_DIA[dia.weekday()] in interpretar_frequencia(freq)["dias"]


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


_LINHAS_VIA: list[Any] | None = None
_ARVORE_VIA: Any = None


def vias_no_corredor(origem: tuple[float, float], destino: tuple[float, float], folga_m: float) -> list[list[list[float]]]:
    """Ruas oficiais entre dois pulsos. A trajetória longa escolhe o caminho por aqui."""
    global _LINHAS_VIA, _ARVORE_VIA
    from shapely.geometry import box
    from shapely.strtree import STRtree

    if _LINHAS_VIA is None:
        _LINHAS_VIA = carregar_vias()
        _ARVORE_VIA = STRtree(_LINHAS_VIA) if _LINHAS_VIA else False
    if not _ARVORE_VIA:
        return []
    ax, ay = TO_UTM.transform(float(origem[1]), float(origem[0]))
    bx, by = TO_UTM.transform(float(destino[1]), float(destino[0]))
    zona = box(min(ax, bx) - folga_m, min(ay, by) - folga_m, max(ax, bx) + folga_m, max(ay, by) + folga_m)
    saida: list[list[list[float]]] = []
    for indice in _ARVORE_VIA.query(zona):
        linha = _LINHAS_VIA[int(indice)]
        coords = []
        for x, y in linha.coords:
            lon, lat = TO_WGS.transform(float(x), float(y))
            coords.append([lat, lon])
        if len(coords) >= 2:
            saida.append(coords)
    return saida


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
        zona = geom.buffer(45)
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
        if codigo.upper().startswith("DIFIC") or codigo.upper().startswith("DIF"):
            if "ACESSO" in codigo.upper():
                continue
        agenda = interpretar_frequencia(str(freq or ""))
        if dia is not None and ALIAS_DIA[dia.weekday()] not in agenda["dias"]:
            continue
        if not blob:
            continue
        geom = _gpkg_geom(blob)
        oficial = _km_oficial(km)
        setores.append(
            {
                "codigo": codigo,
                "nome": str(nome or "").strip(),
                "frequencia": agenda["frequencia"],
                "dias": agenda["dias"],
                "turno": agenda["turno"],
                "diario": agenda["diario"],
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


def listar_agenda() -> list[dict[str, Any]]:
    """Dias e turnos oficiais, sem geometria. Uma rota diária entra em todos os dias."""
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
    select = ", ".join(_quote(c) if c else "NULL" for c in (col_codigo, col_nome, col_freq))
    agenda = []
    for codigo, nome, freq in con.execute(f"SELECT {select} FROM {_quote(tabela)}"):
        codigo = str(codigo or "").strip()
        if "ACESSO" in codigo.upper() and codigo.upper().startswith("DIF"):
            continue
        info = interpretar_frequencia(str(freq or ""))
        if not info["dias"]:
            continue
        agenda.append(
            {
                "codigo": codigo,
                "nome": str(nome or "").strip(),
                "frequencia": info["frequencia"],
                "dias": info["dias"],
                "turno": info["turno"],
                "diario": info["diario"],
            }
        )
    con.close()
    agenda.sort(key=lambda item: (item["turno"], item["codigo"]))
    return agenda


def texto_agenda(dia: date) -> list[str]:
    itens = listar_agenda()
    alias = ALIAS_DIA[dia.weekday()]
    nome = NOMES_DIA[dia.weekday()]
    do_dia = [item for item in itens if alias in item["dias"]]

    def codigos(turno: str, diario: bool) -> str:
        return ", ".join(
            item["codigo"]
            for item in do_dia
            if item["turno"] == turno and bool(item["diario"]) == diario and item["codigo"]
        )

    linhas = [
        f"Agenda de {nome}. A rota só conta no dia e no turno em que está programada no mapa.",
    ]
    diurno = codigos("DIURNO", False)
    noturno = codigos("NOTURNO", False)
    diario = codigos("NOTURNO", True)
    outros = ", ".join(item["codigo"] for item in do_dia if not item["turno"] and item["codigo"])
    if diurno:
        linhas.append(f"Diurno de {nome}: {diurno}.")
    if noturno:
        linhas.append(f"Noturno de {nome}: {noturno}.")
    if diario:
        linhas.append(f"Noturno diário, de segunda a domingo: {diario}.")
    if outros:
        linhas.append(f"Sem turno no mapa: {outros}.")
    contagem = []
    for indice, nome_dia in enumerate(NOMES_DIA):
        marca = ALIAS_DIA[indice]
        total = sum(1 for item in itens if marca in item["dias"])
        contagem.append(f"{nome_dia} {total}")
    linhas.append("De segunda a domingo: " + ", ".join(contagem) + ".")
    return linhas


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
    on_progress: Any | None = None,
) -> None:
    if not setores:
        return
    from .cobertura import avaliar_cobertura, pecas_da_rua, recortar_pontos
    from .verificacao import coberturas_executadas

    for setor in setores:
        if "_pecas" not in setor:
            setor["_pecas"] = pecas_da_rua(setor.get("ruas") or [])
    total = len(rotas)
    n_setores = len(setores)
    for indice, item in enumerate(rotas, start=1):
        placa = item.get("placa") or item.get("veiculo")
        if on_progress:
            on_progress(f"Mapa oficial {indice}/{total} · {placa}")
        pontos = item.get("pontos_dia") or item.get("pontos") or []
        coberturas = []
        melhor = None
        melhor_cob: dict[str, Any] | None = None
        melhor_pct = -1.0
        for ordem, setor in enumerate(setores, start=1):
            if on_progress and (ordem == 1 or ordem % 8 == 0):
                on_progress(f"Mapa oficial {indice}/{total} · {placa} · rota {ordem}/{n_setores}")
            clip = recortar_pontos(pontos, setor["geom"], 120)
            if len(clip) < 8:
                continue
            cob = avaliar_cobertura(
                [],
                clip,
                geom_setor=setor["geom"],
                km_oficial=setor.get("km_oficial"),
                pecas=setor.get("_pecas") or [],
            )
            km_feito = float(cob.get("km_coleta") or 0)
            n_pts = int(cob.get("pontos_no_setor") or 0)
            if n_pts < 8 and km_feito < 0.4:
                continue
            row = {
                "codigo_rota": setor["codigo"],
                "setor": setor["completo"] or f"{setor['codigo']} - {setor['nome']}",
                "rota": setor["frequencia"],
                "turno": setor.get("turno") or "",
                "dias": setor.get("dias") or [],
                "km_coleta": cob.get("km_coleta"),
                "km_total": cob.get("km_total"),
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
            item["rota_planejada"] = []
            item["pontos_dia"] = pontos
            item["pontos"] = pontos
            item["veiculo_inlog"] = item.get("inlog") or ""
        else:
            item["coberturas_inlog"] = coberturas
            uteis = [c for c in coberturas if str(c.get("execucao") or "").upper() != "PASSAGEM"]
            if not uteis:
                uteis = [c for c in coberturas if float(c.get("percentual") or 0) >= 5]
            if uteis and pontos:
                top = max(uteis, key=lambda row: float(row.get("percentual") or 0))
                item["percentual_concluido"] = top.get("percentual") or 0
                item["veredicto"] = top.get("execucao") or top.get("veredicto") or "NÃO FEITA"
                item["confianca"] = top.get("confianca") or "baixa"
                item["setor"] = top.get("setor") or ""
                item["rota"] = top.get("rota") or ""
                item["codigo_rota"] = top.get("codigo_rota")
                item["km_coleta"] = top.get("km_coleta")
                item["km_total"] = top.get("km_total")
                item["km_faltando"] = top.get("km_faltando")
                item["trechos_feitos"] = top.get("trechos_feitos") or []
                item["trechos_faltando"] = top.get("trechos_faltando") or []
                item["poligonos"] = top.get("poligonos") or []
                item["pontos_dia"] = pontos
                item["pontos"] = pontos
            else:
                item["percentual_concluido"] = 0.0
                item["veredicto"] = "SEM RASTREIO" if not pontos else "SEM COLETA"
                item["confianca"] = "baixa"
                item["poligonos"] = []
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
                item["pontos"] = pontos
