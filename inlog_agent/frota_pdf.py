"""PDF das paradas do turno. Reaproveita paradas, telemetria e a operação.

Um caminhão responde no GET. A operação inteira vai para job, porque o
repasse da Vercel corta a requisição aos 25 s.
"""

from __future__ import annotations

import io
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .conducao import AVISO_ESTIMADO
from .frota import (
    _agora_brt,
    _fmt_placa,
    _operacao_efetiva,
    _param,
    _placas_operacao,
    ao_vivo,
    consultar_paradas,
    consultar_telemetria,
    esperar_enderecos,
)
from .live_turno import horarios_do_turno

_FONTE = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
_FONTE_NEGRITO = Path(__file__).resolve().parent / "fonts" / "DejaVuSans-Bold.ttf"
_REGISTRO = False
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_FORA = Color(1, 0.90, 0.90)
_CINZA = HexColor("#F4F4F4")
_LINHA = HexColor("#D0D0D0")
_AZUL = HexColor("#1A56C4")


def gerar_paradas_pdf(
    qs: dict[str, list[str]],
    progresso: Any = None,
) -> tuple[bytes, str]:
    _avisar(progresso, 15, "Consultando paradas e telemetria.")
    pacote = _montar(qs)
    _avisar(progresso, 70, "Montando o PDF.")
    pdf = _desenhar(pacote)
    _avisar(progresso, 100, "PDF pronto.")
    return pdf, pacote["nome"]


def iniciar_paradas_pdf(qs: dict[str, list[str]]) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    job = {
        "pronto": False,
        "progresso": 0,
        "msg": "Na fila.",
        "erro": "",
        "pdf": b"",
        "nome": "",
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def _rodar() -> None:
        def avisar(valor: int, texto: str) -> None:
            job["progresso"] = int(valor)
            job["msg"] = texto

        try:
            pdf, nome = gerar_paradas_pdf(qs, avisar)
            job["pdf"] = pdf
            job["nome"] = nome
            job["progresso"] = 100
            job["pronto"] = True
            job["msg"] = "PDF pronto."
        except Exception as exc:
            job["erro"] = str(exc)
            job["msg"] = str(exc)
            job["pronto"] = False

    threading.Thread(target=_rodar, daemon=True, name=f"paradas-pdf-{job_id[:8]}").start()
    return {"ok": True, "job_id": job_id}


def andamento_paradas_pdf(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    return {
        "ok": not job["erro"],
        "pronto": bool(job["pronto"]),
        "progresso": int(job["progresso"]),
        "msg": str(job["msg"] or ""),
        "erro": job["erro"] or None,
    }


def baixar_paradas_pdf(job_id: str) -> tuple[bytes, str]:
    job = _job(job_id)
    if job["erro"]:
        raise ValueError(str(job["erro"]))
    if not job["pronto"] or not job["pdf"]:
        raise ValueError("O PDF ainda não está pronto.")
    return bytes(job["pdf"]), str(job["nome"])


def _job(job_id: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        job = _JOBS.get((job_id or "").strip())
    if job is None:
        raise ValueError("Job não encontrado.")
    return job


def _avisar(progresso: Any, valor: int, texto: str) -> None:
    if progresso is not None:
        progresso(valor, texto)


def _montar(qs: dict[str, list[str]]) -> dict[str, Any]:
    inicio, _fim, dia, turno = _periodo(qs)
    if turno not in {"diurno", "noturno"}:
        raise ValueError("Informe o turno: diurno ou noturno.")
    minimo = _numero(qs, "min", 5)
    maximo = _numero(qs, "max", 60)
    so_fora = _param(qs, "so_fora_da_rota").lower() in {"1", "true", "sim", "yes", "on"}
    op = _operacao_efetiva(qs)
    nome_op = "Todas" if op is None else str(op["nome"])
    placa = _fmt_placa(_param(qs, "placa")) if _param(qs, "placa") else ""
    placas = [placa] if placa else _lista_placas(qs, op)
    consulta = consultar_paradas(qs)
    esperar_enderecos(list(consulta.get("paradas") or []))
    grupos: dict[str, list[dict[str, Any]]] = {item: [] for item in placas}
    for parada in consulta.get("paradas") or []:
        if so_fora and parada.get("dentro_da_rota") is not False:
            continue
        destino = grupos.setdefault(str(parada.get("placa") or ""), [])
        destino.append(parada)
    descricoes = _descricoes(qs)
    caminhoes: list[dict[str, Any]] = []
    for item in placas:
        tele = _telemetria(qs, item)
        paradas = grupos.get(item) or []
        caminhoes.append(
            {
                "placa": item,
                "descricao": descricoes.get(item) or item,
                "telemetria": tele,
                "paradas": paradas,
                "tempo_paradas": sum(float(p.get("duracao_min") or 0) for p in paradas),
            }
        )
    fora = sum(1 for cam in caminhoes for p in cam["paradas"] if p.get("dentro_da_rota") is False)
    total_paradas = sum(len(cam["paradas"]) for cam in caminhoes)
    tempo = sum(cam["tempo_paradas"] for cam in caminhoes)
    campeao = ""
    if caminhoes:
        maior = max(caminhoes, key=lambda cam: (cam["tempo_paradas"], cam["placa"]))
        if maior["tempo_paradas"] > 0:
            campeao = f"{maior['placa']} ({_duracao(maior['tempo_paradas'])})"
    hora_ini, hora_fim = horarios_do_turno(dia, turno)
    return {
        "nome": _nome_arquivo(placa, nome_op, dia.isoformat(), turno),
        "operacao": nome_op,
        "data": dia.strftime("%d/%m/%Y"),
        "turno": f"{'Diurno' if turno == 'diurno' else 'Noturno'} {hora_ini}–{hora_fim}",
        "filtro": f"{_inteiro(minimo)} a {_inteiro(maximo)} min",
        "gerado": _agora_brt().strftime("%d/%m/%Y %H:%M"),
        "minimo": _inteiro(minimo),
        "maximo": _inteiro(maximo),
        "caminhoes": caminhoes,
        "total_paradas": total_paradas,
        "tempo_total": tempo,
        "fora": fora,
        "campeao": campeao or "—",
        "inicio_iso": inicio,
    }


def _periodo(qs: dict[str, list[str]]):
    from .frota import resolver_periodo

    return resolver_periodo(_param(qs, "data"), _param(qs, "turno"), _param(qs, "inicio"), _param(qs, "fim"))


def _numero(qs: dict[str, list[str]], nome: str, padrao: float) -> float:
    from .frota import _limite

    return _limite(_param(qs, nome), padrao, nome)


def _lista_placas(qs: dict[str, list[str]], op: dict[str, Any] | None) -> list[str]:
    if op is None:
        vivo = ao_vivo(qs)
        return [str(item.get("placa") or "") for item in vivo.get("veiculos") or [] if item.get("placa")]
    return sorted(_placas_operacao(op))


def _descricoes(qs: dict[str, list[str]]) -> dict[str, str]:
    try:
        vivo = ao_vivo(qs)
    except Exception:
        return {}
    saida = {}
    for item in vivo.get("veiculos") or []:
        placa = str(item.get("placa") or "")
        if placa:
            saida[placa] = str(item.get("descricao") or placa)
    return saida


def _telemetria(qs: dict[str, list[str]], placa: str) -> dict[str, Any]:
    pedido = {chave: list(valor) for chave, valor in qs.items()}
    pedido["placa"] = [placa]
    pedido["sem_geo_eventos"] = ["1"]
    try:
        return consultar_telemetria(pedido)
    except Exception:
        return {}


def _desenhar(pacote: dict[str, Any]) -> bytes:
    _registrar_fontes()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=28 * mm,
        bottomMargin=16 * mm,
        title="SEGTRACK — Relatório de paradas",
    )
    estilo, negrito, miudo, link = _estilos()
    story: list[Any] = [_resumo(pacote, estilo, negrito)]
    caminhoes = pacote["caminhoes"]
    for indice, cam in enumerate(caminhoes):
        if len(caminhoes) > 1 and indice:
            story.append(PageBreak())
        story.extend(_secao(cam, pacote, estilo, negrito, miudo, link))
    if not caminhoes:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Nenhum caminhão nesta consulta.", estilo))
    doc.build(story, canvasmaker=_numerador(pacote))
    return buf.getvalue()


def _resumo(pacote: dict[str, Any], estilo: ParagraphStyle, negrito: ParagraphStyle) -> Table:
    linhas = [
        [Paragraph("<b>Resumo geral</b>", negrito)],
        [Paragraph(f"Caminhões: {len(pacote['caminhoes'])}", estilo)],
        [Paragraph(f"Total de paradas: {pacote['total_paradas']}", estilo)],
        [Paragraph(f"Tempo total parado: {_duracao(pacote['tempo_total'])}", estilo)],
        [Paragraph(f"Paradas fora da rota: {pacote['fora']}", estilo)],
        [Paragraph(f"Caminhão com mais tempo parado: {escape(pacote['campeao'])}", estilo)],
    ]
    tabela = Table(linhas, colWidths=[186 * mm])
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _CINZA),
        ("BACKGROUND", (0, 1), (-1, -1), white),
        ("BOX", (0, 0), (-1, -1), 0.4, _LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return tabela


def _secao(cam: dict[str, Any], pacote: dict[str, Any], estilo, negrito, miudo, link) -> list[Any]:
    tele = cam["telemetria"] or {}
    blocos: list[Any] = [
        Spacer(1, 4 * mm),
        Paragraph(f"{escape(cam['placa'])} — {escape(cam['descricao'])}", negrito),
        Spacer(1, 1.5 * mm),
        Paragraph(_texto_telemetria(tele), miudo),
        Spacer(1, 2 * mm),
    ]
    blocos.extend(_bloco_conducao(tele, negrito, miudo))
    paradas = cam["paradas"]
    if not paradas:
        blocos.append(
            Paragraph(
                f"Nenhuma parada entre {pacote['minimo']} e {pacote['maximo']} min neste turno.",
                estilo,
            )
        )
        return blocos
    cab = ["Nº", "Início", "Fim", "Duração", "Endereço", "Rota", "Ignição", "Mapa"]
    dados: list[list[Any]] = [[Paragraph(f"<b>{c}</b>", miudo) for c in cab]]
    fora_linhas = []
    tem_lacuna = False
    for indice, parada in enumerate(paradas, start=1):
        if parada.get("com_lacuna"):
            tem_lacuna = True
        marca = f"{indice} ⚠ sinal" if parada.get("com_lacuna") else str(indice)
        href = f"https://www.google.com/maps?q={parada.get('lat')},{parada.get('lon')}"
        dados.append(
            [
                Paragraph(escape(marca), miudo),
                Paragraph(escape(_hora_curta(parada.get("inicio"))), miudo),
                Paragraph(escape(_hora_curta(parada.get("fim"))), miudo),
                Paragraph(escape(_duracao(parada.get("duracao_min"))), miudo),
                Paragraph(escape(str(parada.get("endereco") or "—")), miudo),
                Paragraph(escape(_rota(parada.get("dentro_da_rota"))), miudo),
                Paragraph(escape(_ignicao(parada.get("ignicao_desligada"))), miudo),
                Paragraph(f'<link href="{href}">Ver no mapa</link>', link),
            ]
        )
        if parada.get("dentro_da_rota") is False:
            fora_linhas.append(indice)
    larguras = [16 * mm, 22 * mm, 22 * mm, 16 * mm, 48 * mm, 16 * mm, 22 * mm, 24 * mm]
    tabela = Table(dados, colWidths=larguras, repeatRows=1)
    comandos = [
        ("BACKGROUND", (0, 0), (-1, 0), _CINZA),
        ("GRID", (0, 0), (-1, -1), 0.3, _LINHA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for linha in fora_linhas:
        comandos.append(("BACKGROUND", (0, linha), (-1, linha), _FORA))
    tabela.setStyle(TableStyle(comandos))
    blocos.append(tabela)
    if tem_lacuna:
        blocos.append(Spacer(1, 1.5 * mm))
        blocos.append(Paragraph("⚠ sinal: intervalo entre pulsos maior que 10 min.", miudo))
    return blocos


def _texto_telemetria(tele: dict[str, Any]) -> str:
    return (
        f"Km {_decimal(tele.get('km'))} · "
        f"em movimento {_duracao(tele.get('tempo_movimento_min'))} · "
        f"parado {_duracao(tele.get('tempo_parado_min'))} · "
        f"velocidade média {_kmh(tele.get('vel_media'))} · "
        f"máxima {_kmh(tele.get('vel_maxima'))}<br/>"
        f"Primeiro pulso {_hora_curta(tele.get('primeiro_pulso'))} · "
        f"último {_hora_curta(tele.get('ultimo_pulso'))} · "
        f"excessos &gt; 60 km/h: {tele.get('pulsos_acima_60') if tele.get('pulsos_acima_60') is not None else '—'}"
    )


def _bloco_conducao(tele: dict[str, Any], negrito, miudo) -> list[Any]:
    if not tele or tele.get("contagem") is None:
        return []
    contagem = tele.get("contagem") or {}
    ocioso = tele.get("ocioso") or {}
    linhas = [
        Spacer(1, 1.5 * mm),
        Paragraph("Condução", negrito),
        Paragraph(_texto_contagem(contagem), miudo),
    ]
    excessos = [item for item in tele.get("eventos") or [] if item.get("tipo") == "excesso_velocidade"]
    if excessos:
        linhas.append(Paragraph("Excessos: " + " · ".join(_texto_excesso(item) for item in excessos), miudo))
    else:
        linhas.append(Paragraph("Excessos: nenhum", miudo))
    linhas.append(Paragraph(_texto_ocioso(ocioso), miudo))
    if tele.get("alerta_ignicao"):
        linhas.append(Paragraph(escape(str(tele["alerta_ignicao"])), miudo))
    linhas.append(Paragraph(escape(AVISO_ESTIMADO), miudo))
    linhas.append(Spacer(1, 2 * mm))
    return linhas


def _texto_contagem(contagem: dict[str, Any]) -> str:
    pares = (
        ("frenagem_brusca", "Frenagem brusca"),
        ("aceleracao_brusca", "Aceleração brusca"),
        ("curva_brusca_esquerda", "Curva à esquerda"),
        ("curva_brusca_direita", "Curva à direita"),
        ("excesso_velocidade", "Excessos"),
        ("motor_ocioso", "Ocioso"),
    )
    return " · ".join(f"{rotulo}: {int(contagem.get(chave) or 0)}" for chave, rotulo in pares)


def _texto_excesso(item: dict[str, Any]) -> str:
    return (
        f"{escape(_hora_curta(item.get('data_hora')))}–{escape(_hora_curta(item.get('fim')))}, "
        f"máx. {escape(_kmh(item.get('velocidade')))} ({escape(_duracao(item.get('duracao_min')))})"
    )


def _texto_ocioso(ocioso: dict[str, Any]) -> str:
    if ocioso.get("origem") == "api":
        origem = "no dia"
    else:
        origem = "estimado no turno"
    percentual = ocioso.get("percentual")
    pct = "—" if percentual is None else f"{_decimal(percentual)}% do tempo ligado"
    return (
        f"Ocioso {origem}: {_duracao(ocioso.get('tempo_min'))} ({pct}) · "
        f"episódios estimados: {int(ocioso.get('episodios') or 0)}"
    )


def _estilos() -> tuple[ParagraphStyle, ParagraphStyle, ParagraphStyle, ParagraphStyle]:
    base = ParagraphStyle(
        "frota",
        fontName="DejaVu",
        fontSize=9,
        leading=12,
        textColor=HexColor("#222222"),
        alignment=TA_LEFT,
    )
    negrito = ParagraphStyle("frota-b", parent=base, fontName="DejaVuBold", fontSize=11, leading=14)
    miudo = ParagraphStyle("frota-m", parent=base, fontSize=7.5, leading=9)
    link = ParagraphStyle("frota-l", parent=miudo, textColor=_AZUL)
    return base, negrito, miudo, link


def _numerador(pacote: dict[str, Any]):
    class Numerado(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            canvas.Canvas.__init__(self, *args, **kwargs)
            self._paginas: list[dict[str, Any]] = []

        def showPage(self):
            self._paginas.append(dict(self.__dict__))
            self._startPage()
            self._pageNumber += 1

        def save(self):
            total = len(self._paginas)
            for estado in self._paginas:
                self.__dict__.update(estado)
                _margem(self, pacote, total)
                canvas.Canvas.showPage(self)
            canvas.Canvas.save(self)

    return Numerado


def _margem(pdf: canvas.Canvas, pacote: dict[str, Any], total: int) -> None:
    largura, altura = A4
    pdf.saveState()
    pdf.setFillColor(HexColor("#16324F"))
    pdf.rect(0, altura - 22 * mm, largura, 22 * mm, stroke=0, fill=1)
    pdf.setFillColor(white)
    pdf.setFont("DejaVuBold", 11)
    pdf.drawString(12 * mm, altura - 8 * mm, "SEGTRACK — Relatório de paradas")
    pdf.setFont("DejaVu", 8)
    pdf.drawString(12 * mm, altura - 13 * mm, pacote["operacao"])
    pdf.drawRightString(largura - 12 * mm, altura - 8 * mm, pacote["data"])
    pdf.drawRightString(largura - 12 * mm, altura - 13 * mm, f"{pacote['turno']} · {pacote['filtro']}")
    pdf.setFillColor(HexColor("#444444"))
    pdf.setFont("DejaVu", 8)
    pdf.drawString(12 * mm, 8 * mm, f"Gerado em {pacote['gerado']}")
    pdf.drawRightString(largura - 12 * mm, 8 * mm, f"Página {pdf._pageNumber} de {total}")
    pdf.restoreState()


def _registrar_fontes() -> None:
    global _REGISTRO
    if _REGISTRO:
        return
    if not _FONTE.is_file() or not _FONTE_NEGRITO.is_file():
        raise RuntimeError("Fonte DejaVuSans não encontrada em inlog_agent/fonts.")
    pdfmetrics.registerFont(TTFont("DejaVu", str(_FONTE)))
    pdfmetrics.registerFont(TTFont("DejaVuBold", str(_FONTE_NEGRITO)))
    _REGISTRO = True


def _nome_arquivo(placa: str, operacao: str, data: str, turno: str) -> str:
    bruto = unicodedata.normalize("NFKD", placa or operacao or "frota")
    bruto = "".join(ch for ch in bruto if not unicodedata.combining(ch))
    limpo = []
    anterior = False
    for ch in bruto:
        ok = ch.isalnum()
        if ok:
            limpo.append(ch)
            anterior = False
        elif not anterior:
            limpo.append("-")
            anterior = True
    base = "".join(limpo).strip("-") or "frota"
    return f"paradas-{base}-{data}-{turno}.pdf"


def _duracao(minutos: Any) -> str:
    if minutos is None or minutos == "":
        return "—"
    try:
        total = int(round(float(minutos)))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        total = 0
    if total < 60:
        return f"{total} min"
    horas, resto = divmod(total, 60)
    return f"{horas}h{resto:02d}"


def _hora_curta(valor: Any) -> str:
    texto = str(valor or "").replace("T", " ")
    if len(texto) >= 16 and texto[4] == "-" and texto[7] == "-":
        return f"{texto[8:10]}/{texto[5:7]} {texto[11:16]}"
    return texto or "—"


def _decimal(valor: Any) -> str:
    if valor is None or valor == "":
        return "—"
    try:
        return f"{float(valor):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def _kmh(valor: Any) -> str:
    texto = _decimal(valor)
    return "—" if texto == "—" else f"{texto} km/h"


def _inteiro(valor: float) -> str:
    if float(valor).is_integer():
        return str(int(valor))
    return str(valor).replace(".", ",")


def _rota(dentro: Any) -> str:
    if dentro is True:
        return "dentro"
    if dentro is False:
        return "fora"
    return "—"


def _ignicao(desligada: Any) -> str:
    if desligada is True:
        return "desligada"
    if desligada is False:
        return "ligada"
    return "—"
