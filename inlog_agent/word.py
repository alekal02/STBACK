"""Relatório Word igual ao PDF, com mapas e campos para justificar."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .pdf import (
    _codigo_setor,
    _fonte_nome,
    _legenda_placas,
    _linhas_capa,
    _linhas_dia,
    _pct_num,
    _pct_ontem_num,
    _situacao_linha,
    capturar_mapas_dia,
)

NAVY = RGBColor(0x14, 0x36, 0x4A)
TEAL = RGBColor(0x00, 0x7F, 0x82)
MUTED = RGBColor(0x53, 0x6A, 0x76)
OK = RGBColor(0x1B, 0x8A, 0x5A)
MID = RGBColor(0xD1, 0x7A, 0x25)
NO = RGBColor(0xB3, 0x41, 0x45)
APOIO = RGBColor(0x6B, 0x4C, 0x9A)
JUST = "FFF8E7"
LARGURA_MAPA = Cm(17.6)


def gerar_word_dia(pasta: Path) -> Path:
    pasta = Path(pasta)
    print("Capturando os mesmos mapas do PDF para o Word...", flush=True)
    cap = capturar_mapas_dia(pasta)
    dados = cap["dados"]
    periodo = cap["dia_br"]
    fonte = cap["fonte"]
    linhas = _linhas_dia(dados.get("linhas") or [], fonte)
    barras = _linhas_capa(dados.get("linhas") or [])
    por_setor = {_codigo_setor(str(row.get("Setor") or ""))[0]: row for row in linhas}

    doc = Document()
    _estilo_base(doc)
    _capa(doc, periodo, fonte, barras, dados.get("recuperadas") or [])
    doc.add_page_break()
    _mapa_frota(doc, periodo, fonte, cap["geral"], cap["frota"])
    for folha in cap["folhas"]:
        doc.add_page_break()
        _folha_rota(doc, periodo, fonte, folha, por_setor)

    dest = pasta / "relatorio-rotas.docx"
    doc.save(dest)
    print(f"Word com mapas: {dest}", flush=True)
    return dest


def _estilo_base(doc: Document) -> None:
    sec = doc.sections[0]
    sec.top_margin = Cm(1.4)
    sec.bottom_margin = Cm(1.4)
    sec.left_margin = Cm(1.5)
    sec.right_margin = Cm(1.5)
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    style.font.color.rgb = NAVY
    style._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")


def _capa(
    doc: Document,
    periodo: str,
    fonte: str,
    barras: list[dict[str, Any]],
    recuperadas: list[dict[str, Any]] | None = None,
) -> None:
    if recuperadas:
        _kicker(doc, "Dia anterior")
        _titulo(doc, "Ontem não feitas · GPS hoje")
        _sub(doc, "Grade de ontem incompleta, só se o rastreador entrou no polígono hoje. O % é o de hoje.")
        extra = doc.add_table(rows=1, cols=2)
        extra.style = "Table Grid"
        for i, nome in enumerate(("Rota", "% hoje")):
            cel = extra.rows[0].cells[i]
            cel.text = ""
            p = cel.paragraphs[0]
            run = p.add_run(nome)
            run.bold = True
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            _sombra(cel, "14364A")
        for row in sorted(recuperadas, key=lambda item: (-_pct_num(item), str(item.get("Setor") or ""))):
            pct = _pct_num(row)
            codigo, nome = _codigo_setor(str(row.get("Setor") or ""))
            label = f"{codigo} — {nome}" if nome else codigo
            cls = "ok" if pct >= 80 else "mid" if pct >= 45 else "no"
            celulas = extra.add_row().cells
            for i, valor in enumerate((f"{label} (dia anterior)", f"{pct:.0f}%")):
                celulas[i].text = ""
                p = celulas[i].paragraphs[0]
                run = p.add_run(valor)
                run.font.size = Pt(10)
                run.font.color.rgb = _cor_sit(cls) if i == 1 else NAVY
                run.bold = True
    _kicker(doc, "Resumo da grade")
    _titulo(doc, "Rotas do dia")
    _sub(doc, f"{_fonte_nome(fonte)}  ·  {periodo}  ·  grade de hoje · só rota e %")
    ordenadas = sorted(barras, key=lambda row: (-_pct_num(row), str(row.get("Setor") or "")))
    grade = doc.add_table(rows=1, cols=2)
    grade.style = "Table Grid"
    for i, nome in enumerate(("Rota", "%")):
        cel = grade.rows[0].cells[i]
        cel.text = ""
        p = cel.paragraphs[0]
        run = p.add_run(nome)
        run.bold = True
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _sombra(cel, "14364A")
    for row in ordenadas:
        pct = _pct_num(row)
        codigo, nome = _codigo_setor(str(row.get("Setor") or ""))
        st = str(row.get("Status") or "").upper()
        cls = "apoio" if "APOIO" in st else ("ok" if pct >= 80 else "mid" if pct >= 45 else "no")
        celulas = grade.add_row().cells
        valores = [
            f"{codigo} — {nome}" if nome else codigo,
            f"{pct:.0f}%",
        ]
        for i, valor in enumerate(valores):
            celulas[i].text = ""
            p = celulas[i].paragraphs[0]
            run = p.add_run(valor)
            run.font.size = Pt(10)
            run.font.color.rgb = _cor_sit(cls) if i == 1 else NAVY
            run.bold = True


def _tabela_rotas(doc: Document, titulo: str, linhas: list[dict[str, Any]]) -> None:
    heading = doc.add_heading(titulo, level=2)
    heading.runs[0].font.color.rgb = NAVY
    cols = [
        "Setor",
        "Turno",
        "Situação",
        "%",
        "Caminhão",
        "Placa",
        "Km no setor",
        "Km oficial",
        "Horário",
        "Justificativa",
    ]
    tabela = doc.add_table(rows=1, cols=len(cols))
    tabela.style = "Table Grid"
    tabela.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, nome in enumerate(cols):
        cel = tabela.rows[0].cells[i]
        cel.text = ""
        p = cel.paragraphs[0]
        run = p.add_run(nome)
        run.bold = True
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _sombra(cel, "14364A")
    for row in linhas:
        sit, cls = _situacao_linha(row)
        codigo, nome = _codigo_setor(str(row.get("Setor") or ""))
        valores = [
            f"{codigo} — {nome}" if nome else codigo,
            str(row.get("Rota") or ""),
            sit,
            f"{_pct_num(row):.0f}%",
            str(row.get("Veículo") or "—"),
            str(row.get("Placa") or "—"),
            str(row.get("Km Coleta") or "—"),
            str(row.get("Km Total") or "—"),
            str(row.get("Horário") or row.get("Horario") or "—"),
            str(row.get("Justificativa") or ""),
        ]
        celulas = tabela.add_row().cells
        for i, valor in enumerate(valores):
            celulas[i].text = ""
            p = celulas[i].paragraphs[0]
            run = p.add_run(valor or ("escreva aqui" if i == 9 else valor))
            run.font.size = Pt(8)
            run.italic = i == 9 and not str(row.get("Justificativa") or "").strip()
            run.font.color.rgb = (
                RGBColor(0xB0, 0x96, 0x5A) if i == 9 and not str(row.get("Justificativa") or "").strip()
                else _cor_sit(cls) if i in {2, 3} else NAVY
            )
            if i == 9:
                _sombra(celulas[i], JUST)
    doc.add_paragraph()


def _frota(doc: Document, frota: list[dict[str, Any]]) -> None:
    _kicker(doc, "Frota")
    _titulo(doc, "Caminhões com GPS no ciclo")
    if not frota:
        doc.add_paragraph("Nenhum GPS neste ciclo.")
        return
    cols = ["Empresa", "Caminhão", "Placa", "Pontos GPS"]
    tabela = doc.add_table(rows=1, cols=len(cols))
    tabela.style = "Table Grid"
    for i, nome in enumerate(cols):
        cel = tabela.rows[0].cells[i]
        cel.text = ""
        p = cel.paragraphs[0]
        run = p.add_run(nome)
        run.bold = True
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _sombra(cel, "14364A")
    for item in frota:
        celulas = tabela.add_row().cells
        valores = [
            "Segtrack",
            str(item.get("Veículo") or item.get("Veiculo") or "—"),
            str(item.get("Placa") or "—"),
            str(item.get("Pontos GPS") or item.get("pontos") or "—"),
        ]
        for i, valor in enumerate(valores):
            celulas[i].text = ""
            p = celulas[i].paragraphs[0]
            run = p.add_run(valor)
            run.font.size = Pt(9)
            run.font.color.rgb = NAVY


def _mapa_frota(doc: Document, periodo: str, fonte: str, imagem: bytes, frota: list[dict[str, Any]]) -> None:
    _kicker(doc, "Presença territorial")
    _titulo(doc, "Trajeto do dia — frota")
    _sub(doc, "Cada veículo no mapa. Contorno marrom = deliminação oficial da rota.")
    chips = []
    vistos: set[str] = set()
    for rota in frota:
        nome = str(rota.get("placa") or rota.get("veiculo") or "").strip()
        if not nome or nome in vistos:
            continue
        if not (rota.get("trechos") or rota.get("executada")):
            continue
        vistos.add(nome)
        chips.append(nome)
    if chips:
        leg = doc.add_paragraph()
        run = leg.add_run("  ·  ".join(chips))
        run.font.size = Pt(9)
        run.font.color.rgb = MUTED
    _inserir_mapa(doc, imagem)


def _folha_rota(
    doc: Document,
    periodo: str,
    fonte: str,
    folha: dict[str, Any],
    por_setor: dict[str, dict[str, Any]],
) -> None:
    setor = str(folha.get("setor") or "")
    codigo, nome = _codigo_setor(setor)
    linha = por_setor.get(codigo) or {}
    pct = float(folha.get("percentual") or 0)
    cls = "ok" if pct >= 80 else "mid" if pct >= 45 else "no"
    _kicker(doc, "Rota")
    titulo = f"{codigo}  ·  {nome}" if nome and setor != "Percurso do dia" else (setor or codigo or "Rota")
    _titulo(doc, titulo)
    if str(linha.get("Rota de ontem") or "").lower() == "sim" or folha.get("ontem"):
        aviso = doc.add_paragraph()
        run = aviso.add_run(
            "Rota de ontem (não concluída). Mapa e % de hoje."
        )
        run.bold = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x1A, 0x4E, 0x86)
    pct_p = doc.add_paragraph()
    run = pct_p.add_run(f"{pct:.0f}%")
    run.bold = True
    run.font.size = Pt(16)
    run.font.color.rgb = _cor_sit(cls)
    placas_rota = _legenda_placas(folha)
    if placas_rota:
        lista = doc.add_paragraph()
        run = lista.add_run(placas_rota)
        run.font.size = Pt(9)
        run.font.color.rgb = MUTED
    _inserir_mapa(doc, folha.get("img") or b"")
    rodape = doc.add_paragraph()
    rodape.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = rodape.add_run(f"{periodo} · {_fonte_nome(fonte)}")
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED


def _inserir_mapa(doc: Document, imagem: bytes) -> None:
    if not imagem:
        p = doc.add_paragraph("Mapa não disponível para esta folha.")
        p.runs[0].font.color.rgb = MUTED
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run()
    run.add_picture(BytesIO(imagem), width=LARGURA_MAPA)


def _kicker(doc: Document, texto: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(texto.upper())
    run.bold = True
    run.font.size = Pt(9)
    run.font.color.rgb = TEAL


def _titulo(doc: Document, texto: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(texto)
    run.bold = True
    run.font.size = Pt(20)
    run.font.color.rgb = NAVY


def _sub(doc: Document, texto: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(8)
    run = p.add_run(texto)
    run.font.size = Pt(11)
    run.font.color.rgb = MUTED


def _cor_sit(cls: str) -> RGBColor:
    return {"ok": OK, "mid": MID, "no": NO, "apoio": APOIO}.get(cls, NAVY)


def _sombra(cel, hex_cor: str) -> None:
    tc = cel._tc
    tc_pr = tc.get_or_add_tcPr()
    shd = tc_pr.makeelement(qn("w:shd"), {
        qn("w:val"): "clear",
        qn("w:color"): "auto",
        qn("w:fill"): hex_cor,
    })
    tc_pr.append(shd)
