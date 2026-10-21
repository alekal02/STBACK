"""PDF de leitura do turno ao vivo.

Usa o estado já gravado no SQLite. Não chama a Fulltrack, não recalcula
cobertura e não grava percentual, status, cursor nem ponto.
"""

from __future__ import annotations

import io
import math
import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from .pdf_tiles import carimbar, enquadrar, obter_tiles, pintar_fundo, projetar, reiniciar_orcamento


_PDF = threading.Lock()
mapas_tiles = 0
mapas_fallback = 0
_BRT = ZoneInfo("America/Sao_Paulo")
_CORES = {
    "concluida": (27, 138, 90),
    "parcial": (197, 106, 26),
    "inicio": (29, 106, 138),
    "nao": (183, 195, 201),
    "passagem": (138, 61, 61),
    "apoio": (91, 75, 138),
}
_LEGENDAS = (
    ("concluida", "Concluída"),
    ("parcial", "Parcial"),
    ("nao", "Não iniciada"),
    ("apoio", "Apoio"),
)


def pdf_ocupado() -> bool:
    return _PDF.locked()


def gerar_operacao(data: str | None, turno: str | None) -> tuple[bytes, str]:
    """Lê um snapshot e devolve o PDF. A trava não é a do GPS."""
    dia, pedido = _validar(data, turno)
    if not _PDF.acquire(timeout=90):
        raise TimeoutError("Já existe um relatório em geração.")
    try:
        pacote = ler_pacote(dia.isoformat(), pedido)
        nome = f"SEGTRACK_Relatorio_Operacao_{dia.isoformat()}_{pedido.capitalize()}.pdf"
        return renderizar(pacote), nome
    finally:
        _PDF.release()


def _validar(data: str | None, turno: str | None) -> tuple[date, str]:
    from .config import parse_data

    bruto = (data or "").strip()
    if not bruto:
        raise ValueError("Informe a data.")
    dia = parse_data(bruto)
    if dia is None:
        raise ValueError("Data inválida.")
    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        raise ValueError("Informe o turno: diurno ou noturno.")
    return dia, pedido


def ler_pacote(data: str | None, turno: str | None) -> dict[str, Any]:
    """Uma transação de leitura. O desenho usa só este dicionário."""
    from .live_mapa import _linhas_geojson, _linhas_visuais, _marco, _poligonos
    from .live_report import _exibir_classificacao, _hora_curta, _periodo_padrao, _resumo
    from .live_turno import resumo_guardado
    from .mapa_oficial import _freq_do_dia
    from .unificado import _casa_turno

    dia, pedido = _validar(data, turno)
    con = _leitura()
    try:
        cab = con.execute(
            "SELECT * FROM turno WHERE data = ? AND turno = ?",
            (dia.isoformat(), pedido),
        ).fetchone()
        if cab is None:
            raise ValueError("Ainda não há estado salvo deste turno.")
        linhas = con.execute(
            "SELECT * FROM rota WHERE data = ? AND turno = ?",
            (dia.isoformat(), pedido),
        ).fetchall()
        brutos = con.execute(
            "SELECT placa, ts, lat, lon FROM ponto WHERE data = ? AND turno = ? ORDER BY placa, ts",
            (dia.isoformat(), pedido),
        ).fetchall()
    finally:
        con.close()
    rotas = [_rota_lida(row) for row in linhas]
    rotas.sort(key=_ordem_codigo)
    if not rotas:
        raise ValueError("Ainda não há estado salvo deste turno.")
    pontos: dict[str, list[tuple[str, float, float]]] = {}
    for row in brutos:
        pontos.setdefault(str(row["placa"] or ""), []).append((str(row["ts"] or ""), float(row["lat"]), float(row["lon"])))
    veiculos = []
    for placa, serie in pontos.items():
        linhas_visuais = _linhas_visuais(serie)
        if linhas_visuais:
            veiculos.append({"placa": placa, "linhas": linhas_visuais})
    _aplicar_recorte_basculante(veiculos, pontos, dia, pedido)
    detalhes = {}
    for row in linhas:
        codigo = str(row["codigo"] or "")
        detalhes[codigo] = {
            "entrada": _marco(row["entrada_lat"], row["entrada_lon"], row["entrada_hora"]),
            "saida": _marco(row["saida_lat"], row["saida_lon"], row["saida_hora"]),
            "trechos_feitos": _linhas_geojson(str(row["trechos_feitos"] or ""), "feitos"),
            "trechos_faltando": _linhas_geojson(str(row["trechos_faltando"] or ""), "faltando"),
        }
    codigos = {str(rota["codigo"] or "") for rota in rotas}
    features = []
    for setor in _poligonos():
        if setor["codigo"] not in codigos:
            continue
        if not _freq_do_dia(setor["frequencia"], dia):
            continue
        if not _casa_turno(setor["frequencia"], pedido):
            continue
        aneis = []
        for anel in setor["aneis"]:
            coords = [[float(par[1]), float(par[0])] for par in anel if len(par) >= 2]
            if len(coords) >= 4:
                aneis.append(coords)
        if not aneis:
            continue
        geometria = {"type": "Polygon", "coordinates": aneis[:1]} if len(aneis) == 1 else {"type": "MultiPolygon", "coordinates": [[anel] for anel in aneis]}
        features.append({"type": "Feature", "id": setor["codigo"], "properties": {"codigo": setor["codigo"], "nome": setor["nome"]}, "geometry": geometria})
    situacao = str(cab["situacao"] or "")
    if not situacao:
        situacao = "ENCERRADO" if not cab["aberto"] else "ABERTO"
    resumo = _resumo(rotas)
    guardado = resumo_guardado(cab["resumo_json"]) if situacao == "ENCERRADO" and "resumo_json" in cab.keys() else None
    if guardado:
        resumo = guardado
    atualizado = str(cab["atualizado_em"] or cab["ultima_sincronizacao"] or "")
    estado = _exibir_classificacao({
        "data": dia.isoformat(),
        "turno": pedido,
        "periodo": str(cab["periodo"] or "") or _periodo_padrao(dia, pedido),
        "situacao": situacao,
        "atualizado_em": atualizado,
        "atualizado_hora": _hora_curta(atualizado),
        "consolidar_ate": str(cab["consolidar_ate"] or "") if "consolidar_ate" in cab.keys() else "",
        "resumo": resumo,
        "rotas": rotas,
    })
    return {
        "estado": estado,
        "data": dia.isoformat(),
        "turno": pedido,
        "geometria": {"type": "FeatureCollection", "features": features},
        "trilhas": {"veiculos": veiculos},
        "pontos": pontos,
        "detalhes": detalhes,
        "gerado_em": datetime.now(_BRT).strftime("%d/%m/%Y %H:%M:%S"),
    }


def _leitura() -> sqlite3.Connection:
    from .live_db import caminho_banco

    uri = caminho_banco().resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    return con


def _rota_lida(row: sqlite3.Row) -> dict[str, Any]:
    from .cobertura import ler_executores_json

    info = ler_executores_json(row["executores_json"] if "executores_json" in row.keys() else "")
    return {
        "codigo": row["codigo"],
        "rota": row["rota"],
        "frequencia": row["frequencia"],
        "veiculo": info["veiculo_principal"] or row["veiculo"],
        "placa": info["placa_principal"] or row["placa"],
        "placa_principal": info["placa_principal"],
        "placas_apoio": info["placas_apoio"],
        "apoio": info["apoio"],
        "sobrepostos": info["sobrepostos"],
        "cobertura_sobreposta": info["cobertura_sobreposta"],
        "executores": info["executores"],
        "passagens": info["passagens"],
        "nao_confirmados": info["nao_confirmados"],
        "resumo_veiculos": info["resumo_veiculos"],
        "status": row["status"],
        "grupo": row["grupo"],
        "percentual": row["percentual"],
        "inicio": row["inicio"],
        "ultimo_gps": row["ultimo_gps"],
        "na_grade": bool(row["na_grade"]),
    }


_ORDEM = re.compile(r"^(\d+)(.*)$")


def _ordem_codigo(rota: dict[str, Any]) -> tuple[int, str]:
    codigo = str(rota.get("codigo") or "")
    achado = _ORDEM.match(codigo)
    if not achado:
        return (10**9, codigo)
    return (int(achado.group(1)), achado.group(2))


def _aplicar_recorte_basculante(
    veiculos: list[dict[str, Any]],
    pontos: dict[str, list[tuple[str, float, float]]],
    dia: date,
    turno: str,
) -> None:
    """No mapa geral, o basculante só leva o trecho interno. A linha da rota não muda."""
    from .dificil_acesso import anotar_programacao, eh_basculante
    from .live_mapa import areas_dificil, trechos_internos_dificil

    programadas = [area for area in anotar_programacao(areas_dificil(), dia, turno) if area.get("programada")]
    for item in veiculos:
        placa = str(item.get("placa") or "")
        if not eh_basculante(placa):
            continue
        item["trechos_internos"] = trechos_internos_dificil(pontos.get(placa) or [], programadas)


def linhas_do_mapa_geral(veiculos: list[dict[str, Any]] | None) -> list:
    """Basculante usa o recorte. As demais placas continuam com a trilha já montada."""
    saida: list = []
    for item in veiculos or []:
        if "trechos_internos" in item:
            saida.extend(item.get("trechos_internos") or [])
        else:
            saida.extend(item.get("linhas") or [])
    return saida


def bytes_do_pacote(pacote: dict[str, Any]) -> bytes:
    """Uma geração por vez. Não usa a trava do sincronizador de GPS."""
    if not _PDF.acquire(timeout=90):
        raise TimeoutError("Já existe um relatório em geração.")
    try:
        return renderizar(pacote)
    finally:
        _PDF.release()


def renderizar(pacote: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    regular, negrito = _fontes()
    if regular:
        pdfmetrics.registerFont(TTFont("SagRegular", regular))
        pdfmetrics.registerFont(TTFont("SagBold", negrito or regular))
        fonte, forte = "SagRegular", "SagBold"
    else:
        fonte, forte = "Helvetica", "Helvetica-Bold"
    estado = pacote["estado"]
    todas = sorted(estado.get("rotas") or [], key=_ordem_codigo)
    rotas = [r for r in todas if r.get("na_grade") and r.get("grupo") != "apoio"]
    apoios = [r for r in todas if r.get("grupo") == "apoio"]
    poligonos = _poligonos(pacote.get("geometria") or {})
    _anotar_fora(pacote, poligonos)
    pacote.pop("pontos", None)
    trilhas = {
        str(item.get("placa") or ""): item.get("linhas") or []
        for item in (pacote.get("trilhas") or {}).get("veiculos") or []
    }
    global mapas_tiles, mapas_fallback
    mapas_tiles = 0
    mapas_fallback = 0
    reiniciar_orcamento()
    buf = io.BytesIO()

    class _Folha(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            canvas.Canvas.__init__(self, *args, **kwargs)
            self._guardadas = []

        def showPage(self):
            self._guardadas.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._guardadas)
            for estado_pagina in self._guardadas:
                self.__dict__.update(estado_pagina)
                self.setFillColorRGB(0.35, 0.42, 0.46)
                self.setFont(fonte, 8)
                self.drawString(14 * mm, 8 * mm, f"SEGTRACK  ·  {_data_br(estado.get('data'))}  ·  {str(estado.get('turno') or '').capitalize()}")
                self.drawCentredString(largura / 2, 8 * mm, f"Gerado em {pacote.get('gerado_em')}")
                self.drawRightString(largura - 14 * mm, 8 * mm, f"Página {self._pageNumber} de {total}")
                canvas.Canvas.showPage(self)
            canvas.Canvas.save(self)

    pdf = _Folha(buf, pagesize=A4)
    pdf.setTitle("SEGTRACK — Relatório Operacional de Conclusão de Rotas")
    pdf.setAuthor("SEGTRACK")
    pdf.setSubject(f"{_data_br(estado.get('data'))} · {str(estado.get('turno') or '').capitalize()}")
    largura, altura = A4
    _pagina_resumo(pdf, pacote, rotas, apoios, fonte, forte, largura, altura, mm)
    pdf.showPage()
    geral, itens_geral = _mapa(
        poligonos,
        linhas_do_mapa_geral((pacote.get("trilhas") or {}).get("veiculos") or []),
        rotas,
        largura=1500,
        altura_px=1280,
        individual=False,
    )
    _pagina_mapa(pdf, "Mapa geral da operação", geral, itens_geral, fonte, forte, largura, altura, mm)
    pdf.showPage()
    for rota in rotas + apoios:
        codigo = str(rota.get("codigo") or "")
        aneis = poligonos.get(codigo) or []
        detalhe = (pacote.get("detalhes") or {}).get(codigo) or {}
        figura, itens = _mapa_rota(rota, aneis, trilhas.get(str(rota.get("placa") or ""), []), detalhe)
        _pagina_rota(pdf, rota, detalhe, figura, itens, fonte, forte, largura, altura, mm)
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _fontes() -> tuple[str, str]:
    pares = (
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    )
    for regular, negrito in pares:
        if Path(regular).is_file():
            return regular, negrito if Path(negrito).is_file() else regular
    return "", ""


def _situacao(estado: dict[str, Any]) -> tuple[str, str, str]:
    situacao = str(estado.get("situacao") or "")
    if situacao == "ENCERRADO":
        return "RELATÓRIO FINAL DA OPERAÇÃO", "", "Encerrado"
    if situacao == "FINALIZANDO":
        ate = _hora(str(estado.get("consolidar_ate") or ""))
        return "RELATÓRIO PARCIAL", "OPERAÇÃO EM CONSOLIDAÇÃO", "Finalizando"
    return "RELATÓRIO PARCIAL", "OPERAÇÃO EM ANDAMENTO", "Ao vivo"


def _hora(texto: str) -> str:
    bruto = texto.replace("Z", "")
    if "T" in bruto:
        return bruto.split("T", 1)[1][:8]
    return bruto[:8]


def _data_br(iso: str) -> str:
    partes = str(iso or "").split("-")
    if len(partes) == 3:
        return f"{partes[2]}/{partes[1]}/{partes[0]}"
    return iso or "—"


def _texto(valor: Any) -> str:
    bruto = str(valor or "").strip()
    if not bruto or bruto in {"—", "-"}:
        return "—"
    return bruto


def _pct(valor: Any) -> str:
    try:
        numero = float(valor or 0)
    except (TypeError, ValueError):
        numero = 0
    if abs(numero - round(numero)) < 0.05:
        return f"{int(round(numero))}%"
    return f"{numero:.1f}%".replace(".", ",")


def _nome(rota: dict[str, Any]) -> str:
    bruto = str(rota.get("rota") or rota.get("codigo") or "")
    codigo = str(rota.get("codigo") or "")
    if " - " in bruto:
        return bruto.split(" - ", 1)[1].strip() or codigo
    return bruto or codigo


def _pagina_resumo(pdf, pacote, rotas, apoios, fonte, forte, largura, altura, mm) -> None:
    estado = pacote["estado"]
    resumo = estado.get("resumo") or {}
    titulo, faixa, rotulo = _situacao(estado)
    pdf.setFillColorRGB(0.07, 0.16, 0.22)
    pdf.rect(0, altura - 32 * mm, largura, 32 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont(forte, 9)
    pdf.drawString(14 * mm, altura - 10 * mm, "SEGTRACK")
    pdf.setFont(forte, 16)
    pdf.drawString(14 * mm, altura - 18 * mm, "RELATÓRIO OPERACIONAL")
    pdf.setFont(fonte, 10)
    pdf.drawString(14 * mm, altura - 25 * mm, "CONCLUSÃO DE ROTAS")
    y = altura - 42 * mm
    pdf.setFillColorRGB(0.93, 0.96, 0.96)
    pdf.rect(14 * mm, y - 12 * mm, 182 * mm, 16 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(0.08, 0.28, 0.32)
    pdf.setFont(forte, 12)
    pdf.drawString(18 * mm, y - 2 * mm, titulo)
    if faixa:
        pdf.setFont(fonte, 9)
        pdf.drawString(18 * mm, y - 8 * mm, faixa)
    y -= 20 * mm
    pdf.setFillColorRGB(0.15, 0.18, 0.2)
    pdf.setFont(fonte, 10)
    linhas = [
        f"Data: {_data_br(estado.get('data'))}",
        f"Turno: {str(estado.get('turno') or '').capitalize()}",
        f"Período: {_texto(estado.get('periodo'))}",
        f"Situação: {rotulo}",
        f"Atualizado até: {_texto(estado.get('atualizado_hora') or _hora(str(estado.get('atualizado_em') or '')))}",
        f"Gerado em: {pacote.get('gerado_em')}",
    ]
    if str(estado.get("situacao") or "") == "FINALIZANDO":
        ate = _hora(str(estado.get("consolidar_ate") or ""))
        linhas.append(f"Consolidando GPS até {ate or '—'}")
    for linha in linhas:
        pdf.drawString(14 * mm, y, linha[:110])
        y -= 5 * mm
    y -= 3 * mm
    pdf.setFont(fonte, 9)
    pdf.drawString(14 * mm, y + 1 * mm, "EXECUÇÃO GERAL")
    pdf.setFont(forte, 22)
    pdf.drawString(52 * mm, y - 1 * mm, f"{int(resumo.get('percentual_geral') or 0)}%")
    y -= 14 * mm
    cards = [
        ("Previstas", resumo.get("previstas")),
        ("Concluídas", resumo.get("concluidas")),
        ("Parciais", resumo.get("parciais")),
        ("Não iniciadas", resumo.get("nao_iniciadas")),
        ("Apoio", resumo.get("apoios")),
    ]
    x = 14 * mm
    for nome, valor in cards:
        pdf.setStrokeColorRGB(0.75, 0.8, 0.82)
        pdf.setFillColorRGB(0.96, 0.97, 0.98)
        pdf.roundRect(x, y - 14 * mm, 25 * mm, 18 * mm, 2 * mm, fill=1, stroke=1)
        pdf.setFillColorRGB(0.1, 0.16, 0.2)
        pdf.setFont(forte, 12)
        pdf.drawCentredString(x + 12.5 * mm, y - 3 * mm, str(valor if valor is not None else 0))
        pdf.setFont(fonte, 6.5)
        pdf.drawCentredString(x + 12.5 * mm, y - 9 * mm, nome)
        x += 26.2 * mm
    y -= 24 * mm
    pdf.setFont(forte, 11)
    pdf.setFillColorRGB(0.1, 0.16, 0.2)
    pdf.drawString(14 * mm, y, "Rotas da operação")
    y -= 6 * mm
    colunas = [14, 29, 64, 82, 102, 124, 162]
    cabecalhos = ["ROTA", "VEÍCULO", "PLACA", "INÍCIO", "EXECUÇÃO", "STATUS", "ÚLTIMO GPS"]
    pdf.setFont(forte, 7)
    for xcol, texto in zip(colunas, cabecalhos):
        pdf.drawString(xcol * mm, y, texto)
    y -= 2 * mm
    pdf.setStrokeColorRGB(0.7, 0.75, 0.78)
    pdf.line(14 * mm, y, 200 * mm, y)
    pdf.setFont(fonte, 8)
    for rota in rotas + apoios:
        y -= 5 * mm
        if y < 18 * mm:
            pdf.showPage()
            y = altura - 16 * mm
            pdf.setFont(fonte, 8)
        valores = [
            _texto(rota.get("codigo")),
            _texto(rota.get("veiculo"))[:22],
            _texto(rota.get("placa")),
            _texto(rota.get("inicio")),
            _pct(rota.get("percentual")),
            _texto(rota.get("status")),
            _texto(rota.get("ultimo_gps")),
        ]
        pdf.setFillColorRGB(0.12, 0.16, 0.18)
        for xcol, texto in zip(colunas, valores):
            pdf.drawString(xcol * mm, y, texto)
        apoio = _texto(rota.get("apoio") or rota.get("resumo_veiculos"))
        if apoio:
            y -= 3.6 * mm
            pdf.setFillColorRGB(0.29, 0.36, 0.4)
            pdf.setFont(fonte, 6.5)
            pdf.drawString(29 * mm, y, apoio[:150])
            pdf.setFont(fonte, 8)
            pdf.setFillColorRGB(0.12, 0.16, 0.18)


def _legenda_pdf(pdf, itens: list[tuple[str, str]], x: float, y: float, fonte: str, mm) -> None:
    """Só o que o mapa desta página realmente desenha."""
    cursor = x
    for chave, nome in itens:
        if chave in {"gps", "trilha"}:
            cor_linha = (0.11, 0.31, 0.85) if chave == "gps" else (0.16, 0.22, 0.28)
            pdf.setStrokeColorRGB(*cor_linha)
            pdf.setLineWidth(1.8)
            pdf.line(cursor, y + 1.2 * mm, cursor + 7 * mm, y + 1.2 * mm)
            cursor += 8 * mm
        elif chave == "area":
            pdf.setFillColorRGB(0.92, 0.35, 0.05)
            pdf.rect(cursor, y, 4 * mm, 3 * mm, fill=1, stroke=0)
            cursor += 5.5 * mm
        else:
            cor = _CORES.get(chave, _CORES["nao"])
            pdf.setFillColorRGB(cor[0] / 255, cor[1] / 255, cor[2] / 255)
            pdf.rect(cursor, y, 3.5 * mm, 3.5 * mm, fill=1, stroke=0)
            cursor += 5 * mm
        pdf.setFillColorRGB(0.15, 0.18, 0.2)
        pdf.setFont(fonte, 8)
        pdf.drawString(cursor, y + 0.4 * mm, nome)
        cursor += pdf.stringWidth(nome, fonte, 8) + 4 * mm


def _pagina_mapa(pdf, titulo, imagem, itens, fonte, forte, largura, altura, mm) -> None:
    pdf.setFillColorRGB(0.07, 0.16, 0.22)
    pdf.rect(0, altura - 16 * mm, largura, 16 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont(forte, 13)
    pdf.drawString(14 * mm, altura - 10 * mm, titulo)
    largura_mapa = 186 * mm
    altura_mapa = 175 * mm
    y_mapa = altura - 20 * mm - altura_mapa
    pdf.drawImage(ImageReader_de(imagem), 12 * mm, y_mapa, width=largura_mapa, height=altura_mapa, preserveAspectRatio=True, anchor="c")
    _legenda_pdf(pdf, itens, 14 * mm, y_mapa - 6 * mm, fonte, mm)


def _pagina_rota(pdf, rota, detalhe, imagem, itens, fonte, forte, largura, altura, mm) -> None:
    codigo = _texto(rota.get("codigo"))
    pdf.setFillColorRGB(0.07, 0.16, 0.22)
    pdf.rect(0, altura - 18 * mm, largura, 18 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont(forte, 14)
    pdf.drawString(14 * mm, altura - 8 * mm, f"ROTA {codigo}")
    pdf.setFont(fonte, 9)
    pdf.drawString(14 * mm, altura - 14 * mm, f"{_texto(rota.get('status')).upper()}   {_pct(rota.get('percentual'))}   {_nome(rota)}")
    entrada = (detalhe or {}).get("entrada") or {}
    saida = (detalhe or {}).get("saida") or {}
    fatos = [
        ("Status", _texto(rota.get("status"))),
        ("Execução", _pct(rota.get("percentual"))),
        ("Veículo", _texto(rota.get("veiculo"))),
        ("Placa", _texto(rota.get("placa"))),
        ("Início", _texto(rota.get("inicio"))),
        ("Último GPS", _texto(rota.get("ultimo_gps"))),
        ("Entrada", _texto(entrada.get("hora")) if entrada else "—"),
        ("Saída", _texto(saida.get("hora")) if saida else "—"),
    ]
    y = altura - 28 * mm
    pdf.setFillColorRGB(0.12, 0.16, 0.18)
    for nome, valor in fatos:
        pdf.setFont(forte, 9)
        pdf.drawString(14 * mm, y, nome)
        pdf.setFont(fonte, 9)
        pdf.drawString(42 * mm, y, valor[:70])
        y -= 5.4 * mm
    if str(rota.get("grupo") or "") == "nao":
        pdf.setFont(fonte, 9)
        pdf.drawString(14 * mm, y - 1 * mm, "Área prevista sem execução registrada até o momento.")
        y -= 6 * mm
    fora = rota.get("fora") or {}
    if fora:
        pdf.setFillColorRGB(0.45, 0.28, 0.05)
        pdf.setFont(forte, 8)
        pdf.drawString(14 * mm, y - 1 * mm, "GPS também registrado fora da área oficial")
        pdf.setFillColorRGB(0.12, 0.16, 0.18)
        pdf.setFont(forte, 8)
        pdf.drawString(14 * mm, y - 6 * mm, "OBSERVAÇÃO DE TELEMETRIA")
        pdf.setFont(fonte, 8)
        pdf.drawString(14 * mm, y - 10.5 * mm, "Há registros de GPS associados ao veículo fora da área oficial desta rota.")
        pdf.drawString(14 * mm, y - 14.5 * mm, "Esses registros são informativos e não interferem no percentual de execução.")
        extra = _texto_fora(fora)
        if extra:
            pdf.setFillColorRGB(0.35, 0.42, 0.46)
            pdf.drawString(14 * mm, y - 19 * mm, extra)
            y -= 24 * mm
        else:
            y -= 18 * mm
    if itens:
        _legenda_pdf(pdf, itens, 14 * mm, y - 1 * mm, fonte, mm)
        y -= 6 * mm
    base = 16 * mm
    topo = max(y - 2 * mm, base + 60 * mm)
    pdf.drawImage(ImageReader_de(imagem), 12 * mm, base, width=186 * mm, height=topo - base, preserveAspectRatio=True, anchor="c")


def ImageReader_de(imagem: bytes):
    from reportlab.lib.utils import ImageReader

    return ImageReader(io.BytesIO(imagem))


def _anotar_fora(pacote: dict[str, Any], poligonos: dict[str, list]) -> None:
    """Conta GPS já gravado fora do polígono. Não altera percentual nem status."""
    from shapely.geometry import Point, Polygon
    from shapely.prepared import prep

    pontos = pacote.get("pontos") or {}
    trilhas = {
        str(item.get("placa") or ""): item.get("linhas") or []
        for item in (pacote.get("trilhas") or {}).get("veiculos") or []
    }
    preparados: dict[str, Any] = {}
    for rota in pacote["estado"].get("rotas") or []:
        rota["fora"] = None
        if str(rota.get("grupo") or "") == "nao":
            continue
        placa = str(rota.get("placa") or "").strip()
        codigo = str(rota.get("codigo") or "")
        aneis = poligonos.get(codigo) or []
        if not placa or not aneis:
            continue
        if codigo not in preparados:
            poly = Polygon(aneis[0])
            if not poly.is_valid:
                poly = poly.buffer(0)
            preparados[codigo] = prep(poly)
        serie = pontos.get(placa)
        if serie:
            rota["fora"] = _medir_pontos(serie, preparados[codigo])
        else:
            rota["fora"] = _medir_linhas(trilhas.get(placa) or [], preparados[codigo])


def _medir_pontos(serie: list[tuple[str, float, float]], pronto: Any) -> dict[str, Any] | None:
    from shapely.geometry import Point

    fora_n = 0
    km = 0.0
    inicio = fim = ""
    anterior = None
    anterior_fora = False
    for ts, lat, lon in serie:
        esta = not pronto.covers(Point(float(lon), float(lat)))
        if esta:
            fora_n += 1
            hora = _hora(str(ts))
            if not inicio:
                inicio = hora
            fim = hora
            if anterior is not None and anterior_fora:
                km += _km(anterior[0], anterior[1], lat, lon)
        anterior = (lat, lon)
        anterior_fora = esta
    if not fora_n:
        return None
    return {"pontos": fora_n, "km": round(km, 1), "inicio": inicio, "fim": fim}


def _medir_linhas(linhas: list, pronto: Any) -> dict[str, Any] | None:
    from shapely.geometry import Point

    achou = False
    km = 0.0
    for linha in linhas:
        anterior = None
        anterior_fora = False
        for par in linha:
            if not isinstance(par, (list, tuple)) or len(par) < 2:
                continue
            lat, lon = float(par[0]), float(par[1])
            esta = not pronto.covers(Point(lon, lat))
            if esta:
                achou = True
                if anterior is not None and anterior_fora:
                    km += _km(anterior[0], anterior[1], lat, lon)
            anterior = (lat, lon)
            anterior_fora = esta
    if not achou:
        return None
    return {"pontos": None, "km": round(km, 1), "inicio": "", "fim": ""}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    raio = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * raio * math.asin(min(1.0, math.sqrt(a)))


def _texto_fora(fora: dict[str, Any]) -> str:
    """Quantidade e horário, quando vêm dos pontos persistidos. Sem quilometragem."""
    partes = []
    if fora.get("pontos"):
        partes.append(f"{fora['pontos']} pontos")
    if fora.get("inicio"):
        partes.append(f"de {fora['inicio']} a {fora['fim']}")
    return " · ".join(partes)


def _mapa_rota(rota: dict[str, Any], aneis: list, linhas: list, detalhe: dict[str, Any]) -> tuple[bytes, list[tuple[str, str]]]:
    codigo = str(rota.get("codigo") or "")
    grupo = str(rota.get("grupo") or "nao")
    if grupo == "nao":
        return _mapa({codigo: aneis}, [], [rota], largura=1600, altura_px=1120, individual=True)
    return _mapa(
        {codigo: aneis},
        linhas,
        [rota],
        largura=1600,
        altura_px=1120,
        individual=True,
        entrada=(detalhe or {}).get("entrada"),
        saida=(detalhe or {}).get("saida"),
    )


def _poligonos(colecao: dict[str, Any]) -> dict[str, list[list[tuple[float, float]]]]:
    saida: dict[str, list[list[tuple[float, float]]]] = {}
    for feat in colecao.get("features") or []:
        props = feat.get("properties") or {}
        codigo = str(props.get("codigo") or feat.get("id") or "").upper()
        geom = feat.get("geometry") or {}
        tipo = geom.get("type")
        coords = geom.get("coordinates") or []
        aneis = []
        if tipo == "Polygon":
            aneis = [_anel(coords[0])] if coords else []
        elif tipo == "MultiPolygon":
            aneis = [_anel(poly[0]) for poly in coords if poly]
        aneis = [anel for anel in aneis if len(anel) >= 3]
        if codigo and aneis:
            saida[codigo] = aneis
    return saida


def _anel(pontos: list) -> list[tuple[float, float]]:
    anel = []
    for par in pontos or []:
        if isinstance(par, (list, tuple)) and len(par) >= 2:
            anel.append((float(par[0]), float(par[1])))
    return anel


def _legenda_de(
    rotas: list[dict[str, Any]],
    trilhas: list,
    *,
    tiles: bool,
    tem_area: bool,
) -> list[tuple[str, str]]:
    itens: list[tuple[str, str]] = []
    if tiles:
        if tem_area:
            itens.append(("area", "Área oficial"))
        if trilhas:
            itens.append(("gps", "GPS"))
        return itens
    vistos: list[str] = []
    for rota in rotas:
        grupo = str(rota.get("grupo") or "nao")
        if grupo not in vistos and grupo in _CORES:
            vistos.append(grupo)
    nomes = {chave: nome for chave, nome in _LEGENDAS}
    for grupo in vistos:
        itens.append((grupo, nomes.get(grupo, grupo)))
    if trilhas:
        itens.append(("trilha", "GPS"))
    return itens


def _mapa(
    poligonos: dict[str, list],
    trilhas: list,
    rotas: list[dict[str, Any]],
    *,
    largura: int,
    altura_px: int,
    individual: bool,
    entrada: dict | None = None,
    saida: dict | None = None,
) -> tuple[bytes, list[tuple[str, str]]]:
    global mapas_tiles, mapas_fallback
    try:
        desenhado = _mapa_tiles(poligonos, trilhas, largura, altura_px, individual)
        if desenhado is not None:
            mapas_tiles += 1
            return desenhado, _legenda_de(rotas, trilhas, tiles=True, tem_area=bool(poligonos))
    except Exception as exc:
        print(f"[PDF] mapa sem fundo cartográfico: {exc}", flush=True)
    mapas_fallback += 1
    return (
        _mapa_simples(
            poligonos,
            trilhas,
            rotas,
            largura=largura,
            altura_px=altura_px,
            individual=individual,
            entrada=entrada,
            saida=saida,
        ),
        _legenda_de(rotas, trilhas, tiles=False, tem_area=bool(poligonos)),
    )


def _pontos_quadro(poligonos: dict[str, list], trilhas: list) -> list[tuple[float, float]]:
    pontos: list[tuple[float, float]] = []
    for aneis in poligonos.values():
        for anel in aneis:
            pontos.extend(anel)
    if pontos:
        return pontos
    for linha in trilhas or []:
        for ponto in linha:
            if isinstance(ponto, (list, tuple)) and len(ponto) >= 2:
                pontos.append((float(ponto[1]), float(ponto[0])))
    return pontos


def _mapa_tiles(
    poligonos: dict[str, list],
    trilhas: list,
    largura: int,
    altura_px: int,
    individual: bool,
) -> bytes | None:
    """Modelo travado em 19/10/2026: área oficial laranja e GPS azul.

    Não desenha trecho feito, trecho faltando nem outra malha por cima.
    """
    pontos = _pontos_quadro(poligonos, trilhas)
    vista = enquadrar(pontos, largura, altura_px)
    if not vista:
        return None
    tiles = obter_tiles(vista["tiles"])
    if not tiles:
        return None
    img = pintar_fundo(vista, tiles)
    if img is None:
        return None
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    desenho = ImageDraw.Draw(overlay)

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return projetar(vista, lon, lat)

    for aneis in poligonos.values():
        for anel in aneis:
            seq = [xy(lon, lat) for lon, lat in anel]
            if len(seq) < 3:
                continue
            desenho.polygon(seq, fill=(244, 168, 40, 78))
            desenho.line(seq + [seq[0]], fill=(234, 88, 12, 255), width=5 if individual else 3, joint="curve")
    for linha in trilhas or []:
        seq = []
        for ponto in linha:
            if not isinstance(ponto, (list, tuple)) or len(ponto) < 2:
                continue
            seq.append(xy(float(ponto[1]), float(ponto[0])))
        if len(seq) >= 2:
            desenho.line(seq, fill=(29, 78, 216, 235), width=4 if individual else 2, joint="curve")
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    carimbar(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72, optimize=True)
    return buf.getvalue()


def _mapa_simples(
    poligonos: dict[str, list],
    trilhas: list,
    rotas: list[dict[str, Any]],
    *,
    largura: int,
    altura_px: int,
    individual: bool,
    entrada: dict | None = None,
    saida: dict | None = None,
) -> bytes:
    grupos = {str(r.get("codigo") or ""): str(r.get("grupo") or "nao") for r in rotas}
    pontos = []
    for aneis in poligonos.values():
        for anel in aneis:
            pontos.extend(anel)
    if not individual:
        for linha in trilhas:
            pontos.extend(_par_trilha(p) for p in _amostra(linha, 80))
    for marco in (entrada, saida):
        if marco and marco.get("lat") is not None:
            pontos.append((float(marco["lon"]), float(marco["lat"])))
    if not pontos:
        pontos = [(-49.30, -16.70), (-49.18, -16.90)]
    oeste = min(p[0] for p in pontos)
    leste = max(p[0] for p in pontos)
    sul = min(p[1] for p in pontos)
    norte = max(p[1] for p in pontos)
    folga_x = max((leste - oeste) * 0.08, 0.002)
    folga_y = max((norte - sul) * 0.08, 0.002)
    oeste -= folga_x
    leste += folga_x
    sul -= folga_y
    norte += folga_y
    img = Image.new("RGB", (largura, altura_px), (236, 240, 242))
    desenho = ImageDraw.Draw(img, "RGBA")

    def xy(lon: float, lat: float) -> tuple[float, float]:
        margem = 28
        x = margem + (lon - oeste) / (leste - oeste) * (largura - 2 * margem)
        y = margem + (norte - lat) / (norte - sul) * (altura_px - 2 * margem)
        return x, y

    for codigo, aneis in poligonos.items():
        cor = _CORES.get(grupos.get(codigo, "nao"), _CORES["nao"])
        for anel in aneis:
            seq = [xy(lon, lat) for lon, lat in anel]
            desenho.polygon(seq, fill=cor + (150,))
            if len(seq) >= 2:
                desenho.line(seq + [seq[0]], fill=cor + (255,), width=2)
    for linha in trilhas or []:
        seq = []
        for ponto in linha:
            if not isinstance(ponto, (list, tuple)) or len(ponto) < 2:
                continue
            seq.append(xy(float(ponto[1]), float(ponto[0])))
        if len(seq) >= 2:
            desenho.line(seq, fill=(40, 55, 70, 230), width=3 if individual else 2)
    if entrada:
        desenho.ellipse(_bolha(xy(float(entrada["lon"]), float(entrada["lat"]))), fill=(27, 138, 90, 255))
    if saida:
        desenho.ellipse(_bolha(xy(float(saida["lon"]), float(saida["lat"]))), fill=(179, 65, 69, 255))
    if individual and rotas:
        codigo = str(rotas[0].get("codigo") or "")
        aneis = poligonos.get(codigo) or []
        if aneis:
            xs = [p[0] for p in aneis[0]]
            ys = [p[1] for p in aneis[0]]
            desenho.text(xy(sum(xs) / len(xs), sum(ys) / len(ys)), codigo, fill=(16, 32, 40, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=72, optimize=True)
    return buf.getvalue()


def _par_trilha(ponto: Any) -> tuple[float, float]:
    if isinstance(ponto, (list, tuple)) and len(ponto) >= 2:
        return float(ponto[1]), float(ponto[0])
    return 0.0, 0.0


def _amostra(linha: list, limite: int) -> list:
    if len(linha) <= limite:
        return linha
    passo = max(1, len(linha) // limite)
    return linha[::passo]


def _bolha(ponto: tuple[float, float]) -> list[float]:
    x, y = ponto
    return [x - 6, y - 6, x + 6, y + 6]
