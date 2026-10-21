"""Auditoria administrativa da conclusão de rotas.

Lê o SQLite já gravado. Não chama o motor de cobertura, não baixa GPS
e não grava turno, rota nem ponto.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from typing import Any

_TOLERANCIA = 0.05
_ATRASO_S = 15 * 60
_CHAVES_OCULTAS = {
    "senha",
    "senha_hash",
    "token",
    "csrf",
    "telemetria",
    "secret",
    "authorization",
    "cookie",
    "pontos",
    "trechos",
    "trechos_feitos",
    "trechos_faltando",
    "linhas",
    "latitude",
    "longitude",
    "vehicle_id",
    "ras_vei_id",
    "executores_json",
    "diagnostico",
}
_CAMPOS_COBERTURA = (
    "contribuicao_exclusiva",
    "contribuicao_compartilhada",
)


def consultar(data: str | None, turno: str | None, rota: str | None = None) -> dict[str, Any]:
    """Lista o turno e, se houver rota, detalha a comparação com o snapshot."""
    dia, pedido = _validar(data, turno)
    codigo = str(rota or "").strip().upper()
    con = _leitura()
    if con is None:
        return _sanitizar(_vazio(dia, pedido, "Ainda não há estado salvo deste turno."))
    try:
        cab = _turno(con, dia, pedido)
        if cab is None:
            return _sanitizar(_vazio(dia, pedido, "Ainda não há estado salvo deste turno."))
        linhas = _rotas(con, dia, pedido)
    finally:
        con.close()
    fichas = [_ficha(linha) for linha in linhas]
    fichas.sort(key=lambda item: str(item.get("codigo") or ""))
    escolhida = next((item for item in fichas if item["codigo"] == codigo), None) if codigo else None
    if codigo and escolhida is None:
        escolhida = {
            "codigo": codigo,
            "encontrada": False,
            "alertas": ["Rota não encontrada neste turno."],
        }
    return _sanitizar(_pacote(dia, pedido, cab, fichas, escolhida))


def gerar_pdf(data: str | None, turno: str | None, rota: str | None = None) -> tuple[bytes, str]:
    """Relatório de leitura. O arquivo não entra no banco."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    relatorio = consultar(data, turno, rota)
    buf_nome = _nome_arquivo(relatorio.get("data") or "sem-data", relatorio.get("turno") or "turno", relatorio.get("rota"))
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4, pageCompression=0)
    largura, altura = A4
    y = _cabecalho_pdf(pdf, relatorio, largura, altura, mm)
    rotas = relatorio.get("rotas") or []
    foco = relatorio.get("rota") if isinstance(relatorio.get("rota"), dict) and relatorio["rota"].get("encontrada", True) else None
    linhas = [foco] if foco else rotas
    if not linhas:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(14 * mm, y, "Nenhuma rota gravada neste turno.")
    for item in linhas:
        y = _bloco_rota(pdf, item, y, altura, mm)
    pdf.save()
    return buf.getvalue(), buf_nome


def _validar(data: str | None, turno: str | None) -> tuple[str, str]:
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
    return dia.isoformat(), pedido


def _leitura() -> sqlite3.Connection | None:
    from .live_db import caminho_banco

    caminho = caminho_banco()
    if not caminho.is_file():
        return None
    uri = caminho.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _turno(con: sqlite3.Connection, data: str, turno: str) -> sqlite3.Row | None:
    if not _tabela(con, "turno"):
        return None
    return con.execute(
        "SELECT * FROM turno WHERE data = ? AND turno = ?",
        (data, turno),
    ).fetchone()


def _rotas(con: sqlite3.Connection, data: str, turno: str) -> list[sqlite3.Row]:
    if not _tabela(con, "rota"):
        return []
    colunas = _colunas(con, "rota")
    desejados = (
        "codigo",
        "rota",
        "frequencia",
        "veiculo",
        "placa",
        "status",
        "grupo",
        "percentual",
        "inicio",
        "ultimo_gps",
        "na_grade",
        "atualizado_em",
        "executores_json",
    )
    campos = [nome for nome in desejados if nome in colunas]
    sql = f"SELECT {', '.join(campos)} FROM rota WHERE data = ? AND turno = ? ORDER BY codigo"
    return [_Linha(row) for row in con.execute(sql, (data, turno))]


def _tabela(con: sqlite3.Connection, nome: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (nome,),
    ).fetchone()
    return row is not None


def _colunas(con: sqlite3.Connection, tabela: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({tabela})")}


class _Linha:
    """Completa colunas antigas para os mesmos leitores da tabela, do detalhe e do PDF."""

    def __init__(self, row: sqlite3.Row) -> None:
        self._dados = {str(chave): row[chave] for chave in row.keys()}

    def __getitem__(self, chave: str) -> Any:
        if chave not in self._dados:
            return 0 if chave == "na_grade" else ""
        return self._dados[chave]

    def keys(self) -> set[str]:
        return set(self._dados)


def _ficha(row: sqlite3.Row) -> dict[str, Any]:
    from .cobertura import ler_executores_json
    from .live_pdf import _pct, _rota_lida

    chaves = set(row.keys())
    bruto = row["executores_json"] if "executores_json" in chaves else ""
    info = ler_executores_json(bruto)
    pdf = _rota_lida(row)
    tabela = _num(row["percentual"])
    detalhe = tabela
    pdf_pct = _num(pdf.get("percentual"))
    snapshot = _num(info.get("percentual")) if _tem_snapshot(bruto, info) else None
    presented_placa = str(info.get("placa_principal") or row["placa"] or "")
    pdf_placa = str(pdf.get("placa_principal") or pdf.get("placa") or "")
    placas_conferem = presented_placa == pdf_placa
    fontes_ok = _iguais(tabela, detalhe) and _iguais(detalhe, pdf_pct)
    bate_snapshot = snapshot is not None and _iguais(tabela, snapshot)
    consistente = fontes_ok and bate_snapshot and placas_conferem
    divergente = (not fontes_ok) or (snapshot is not None and not bate_snapshot) or not placas_conferem
    legado = snapshot is not None and not info.get("calculado")
    ausentes = _campos_ausentes(info) if snapshot is not None else []
    caminhoes = _caminhoes(info, pdf)
    if any(not cam["confere_pdf"] for cam in caminhoes):
        divergente = True
        consistente = False
    passagens = _observados(info, "passagens", "passagem")
    nao_confirmados = _observados(info, "nao_confirmados", "nao_confirmado")
    alertas = _alertas_rota(
        divergente=divergente,
        snapshot=snapshot,
        fontes_ok=fontes_ok,
        placas_conferem=placas_conferem,
        legado=legado,
        ausentes=ausentes,
        caminhoes_ok=all(cam["confere_pdf"] for cam in caminhoes),
    )
    if presented_placa and str(row["placa"] or "") and presented_placa != str(row["placa"] or ""):
        alertas.append("A placa gravada na rota difere da placa principal apresentada.")
    return {
        "codigo": str(row["codigo"] or ""),
        "encontrada": True,
        "rota": str(row["rota"] or ""),
        "status": str(row["status"] or ""),
        "grupo": str(row["grupo"] or ""),
        "percentual": snapshot if snapshot is not None else tabela,
        "percentual_texto": _pct(snapshot if snapshot is not None else tabela),
        "placa_principal": presented_placa,
        "placa_gravada": str(row["placa"] or ""),
        "veiculo_principal": str(info.get("veiculo_principal") or row["veiculo"] or ""),
        "apoio": str(info.get("apoio") or ""),
        "placas_apoio": list(info.get("placas_apoio") or []),
        "cobertura_sobreposta": str(info.get("cobertura_sobreposta") or ""),
        "resumo_veiculos": str(info.get("resumo_veiculos") or pdf.get("resumo_veiculos") or ""),
        "comparacao": [
            {"fonte": "Tabela", "percentual": tabela, "texto": _pct(tabela), "confere": _confere(tabela, snapshot, fontes_ok)},
            {"fonte": "Detalhe", "percentual": detalhe, "texto": _pct(detalhe), "confere": _confere(detalhe, snapshot, fontes_ok)},
            {"fonte": "PDF", "percentual": pdf_pct, "texto": _pct(pdf_pct), "confere": _confere(pdf_pct, snapshot, fontes_ok)},
            {
                "fonte": "Snapshot oficial",
                "percentual": snapshot,
                "texto": "ausente" if snapshot is None else _pct(snapshot),
                "confere": snapshot is not None and consistente,
            },
        ],
        "consistente": consistente,
        "divergente": divergente,
        "compativel_historico": fontes_ok and (snapshot is None or _iguais(tabela, snapshot)),
        "incompleto": snapshot is None,
        "legado": legado,
        "campos_ausentes": ausentes,
        "placas_conferem": placas_conferem,
        "caminhoes": caminhoes,
        "passagens": passagens,
        "nao_confirmados": nao_confirmados,
        "alertas": alertas,
        "atualizado_em": str(row["atualizado_em"] or "") if "atualizado_em" in chaves else "",
    }


def _tem_snapshot(bruto: str, info: dict[str, Any]) -> bool:
    if not str(bruto or "").strip():
        return False
    if info.get("calculado"):
        return True
    if info.get("percentual") is not None:
        return True
    return bool(info.get("executores"))


def _campos_ausentes(info: dict[str, Any]) -> list[str]:
    executores = [item for item in info.get("executores") or [] if isinstance(item, dict)]
    if not executores:
        return []
    ausentes = []
    for campo in _CAMPOS_COBERTURA:
        if any(campo not in item for item in executores):
            ausentes.append(campo)
    observados = bool(info.get("passagens") or info.get("nao_confirmados"))
    if observados and not info.get("auditoria"):
        ausentes.append("justificativa")
    return ausentes


def _caminhoes(info: dict[str, Any], pdf: dict[str, Any]) -> list[dict[str, Any]]:
    motivos = {
        str(item.get("placa") or "").upper(): str(item.get("motivo") or "")
        for item in info.get("auditoria") or []
        if isinstance(item, dict)
    }
    pdf_por_placa = {
        str(item.get("placa") or "").upper(): item
        for item in pdf.get("executores") or []
        if isinstance(item, dict)
    }
    saida = []
    for item in info.get("executores") or []:
        if not isinstance(item, dict):
            continue
        placa = str(item.get("placa") or "")
        chave = placa.upper()
        outro = pdf_por_placa.get(chave) or {}
        individual = _num(item.get("percentual"))
        exclusiva = _num(item.get("contribuicao_exclusiva")) if "contribuicao_exclusiva" in item else None
        sobreposta = _num(item.get("contribuicao_compartilhada")) if "contribuicao_compartilhada" in item else None
        pdf_individual = _num(outro.get("percentual")) if outro else None
        saida.append(
            {
                "placa": placa,
                "veiculo": str(item.get("veiculo") or ""),
                "papel": str(item.get("papel") or ""),
                "classificacao": str(item.get("classificacao") or ""),
                "percentual_individual": individual,
                "cobertura_exclusiva": exclusiva,
                "cobertura_sobreposta": sobreposta,
                "horario": str(item.get("horario") or ""),
                "justificativa": motivos.get(chave) or "",
                "confere_pdf": bool(outro) and _iguais(individual, pdf_individual),
            }
        )
    return saida


def _observados(info: dict[str, Any], campo: str, classe: str) -> list[dict[str, str]]:
    motivos = {}
    for item in info.get("auditoria") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("classe") or "") != classe:
            continue
        motivos[str(item.get("placa") or "").upper()] = str(item.get("motivo") or "")
    saida = []
    for placa in info.get(campo) or []:
        texto = str(placa or "").strip()
        if not texto:
            continue
        motivo = motivos.get(texto.upper()) or ""
        saida.append(
            {
                "placa": texto,
                "justificativa": motivo or "Sem justificativa detalhada neste snapshot.",
            }
        )
    return saida


def _alertas_rota(
    *,
    divergente: bool,
    snapshot: float | None,
    fontes_ok: bool,
    placas_conferem: bool,
    legado: bool,
    ausentes: list[str],
    caminhoes_ok: bool,
) -> list[str]:
    alertas = []
    if not fontes_ok:
        alertas.append("Tabela, detalhe e PDF não apresentam o mesmo percentual.")
    elif snapshot is not None and divergente and caminhoes_ok and placas_conferem:
        alertas.append("O percentual apresentado difere do snapshot oficial.")
    if not caminhoes_ok:
        alertas.append("A cobertura individual do PDF difere do snapshot oficial.")
    if snapshot is None:
        alertas.append("Snapshot oficial ausente. O histórico permanece sem recálculo.")
    if not placas_conferem:
        alertas.append("A placa principal não é a mesma na tabela, no detalhe e no PDF.")
    if legado:
        alertas.append("Snapshot anterior à marcação de cálculo oficial.")
    if "contribuicao_exclusiva" in ausentes or "contribuicao_compartilhada" in ausentes:
        alertas.append("Snapshot sem cobertura exclusiva ou sobreposta de cada caminhão.")
    if "justificativa" in ausentes:
        alertas.append("Há passagem ou veículo não confirmado sem justificativa gravada.")
    return alertas


def _confere(valor: float | None, snapshot: float | None, fontes_ok: bool) -> bool:
    if snapshot is None:
        return fontes_ok and valor is not None
    return _iguais(valor, snapshot)


def _pacote(
    data: str,
    turno: str,
    cab: sqlite3.Row,
    fichas: list[dict[str, Any]],
    rota: dict[str, Any] | None,
) -> dict[str, Any]:
    chaves = set(cab.keys())
    situacao = str(cab["situacao"] or "") if "situacao" in chaves else ""
    aberto = bool(cab["aberto"]) if "aberto" in chaves else False
    if not situacao:
        situacao = "ABERTO" if aberto else "ENCERRADO"
    sync = str(cab["ultima_sincronizacao"] or "") if "ultima_sincronizacao" in chaves else ""
    atualizado = str(cab["atualizado_em"] or "") if "atualizado_em" in chaves else ""
    falha = bool(str(cab["falha"] or "").strip()) if "falha" in chaves else False
    antiga = _sincronizacao_antiga(sync or atualizado, aberto)
    incompleto = (not fichas) or any(item.get("incompleto") for item in fichas) or not (sync or atualizado)
    alertas = []
    if not (sync or atualizado):
        alertas.append("Turno sem data de sincronização.")
    elif antiga:
        alertas.append("Sincronização do turno aberto com mais de 15 minutos.")
    if falha:
        alertas.append("A última sincronização registrou falha.")
    if situacao == "ENCERRADO":
        alertas.append("Turno encerrado: a auditoria só lê o que já foi gravado.")
    if any(item.get("divergente") for item in fichas):
        alertas.append("Há rota com percentual divergente do snapshot oficial.")
    if incompleto:
        alertas.append("Há informação antiga ou incompleta neste turno.")
    return {
        "ok": True,
        "data": data,
        "turno": turno,
        "situacao": situacao,
        "historico_encerrado": situacao == "ENCERRADO",
        "ultima_sincronizacao": sync,
        "atualizado_em": atualizado,
        "sincronizacao_antiga": antiga,
        "falha_sincronizacao": falha,
        "incompleto": incompleto,
        "leitura": "somente leitura do snapshot gravado",
        "aviso": "",
        "alertas": alertas,
        "rotas": fichas,
        "rota": rota,
    }


def _vazio(data: str, turno: str, aviso: str) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "turno": turno,
        "situacao": "",
        "historico_encerrado": False,
        "ultima_sincronizacao": "",
        "atualizado_em": "",
        "sincronizacao_antiga": False,
        "falha_sincronizacao": False,
        "incompleto": True,
        "leitura": "somente leitura do snapshot gravado",
        "aviso": aviso,
        "alertas": [aviso],
        "rotas": [],
        "rota": None,
    }


def _sincronizacao_antiga(iso: str, aberto: bool) -> bool:
    if not aberto:
        return False
    lido = _instante(iso)
    if lido is None:
        return True
    from .live_sync import agora_brt

    return (agora_brt() - lido).total_seconds() > _ATRASO_S


def _instante(texto: str) -> datetime | None:
    bruto = (texto or "").strip()
    if not bruto:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(bruto[:19], fmt)
        except ValueError:
            continue
    return None


def _num(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _iguais(esquerda: float | None, direita: float | None) -> bool:
    if esquerda is None or direita is None:
        return False
    return abs(esquerda - direita) < _TOLERANCIA


def _sanitizar(valor: Any) -> Any:
    if isinstance(valor, dict):
        limpo = {}
        for chave, item in valor.items():
            nome = str(chave)
            baixo = nome.lower()
            if baixo in _CHAVES_OCULTAS or "senha" in baixo or "token" in baixo or "secret" in baixo:
                continue
            limpo[nome] = _sanitizar(item)
        return limpo
    if isinstance(valor, list):
        return [_sanitizar(item) for item in valor]
    if isinstance(valor, float):
        return round(valor, 1)
    return valor


def _nome_arquivo(data: str, turno: str, rota: dict[str, Any] | None) -> str:
    codigo = ""
    if isinstance(rota, dict) and rota.get("codigo"):
        codigo = "_" + "".join(ch for ch in str(rota["codigo"]).upper() if ch.isalnum())
    dia = "".join(ch for ch in str(data) if ch.isdigit() or ch == "-") or "sem-data"
    nome_turno = "Diurno" if turno == "diurno" else "Noturno" if turno == "noturno" else "Turno"
    return f"SEGTRACK_Auditoria_{dia}_{nome_turno}{codigo}.pdf"


def _cabecalho_pdf(pdf, relatorio: dict[str, Any], largura: float, altura: float, mm) -> float:
    pdf.setFillColorRGB(0.07, 0.16, 0.22)
    pdf.rect(0, altura - 28 * mm, largura, 28 * mm, fill=1, stroke=0)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(14 * mm, altura - 12 * mm, "SEGTRACK  ·  AUDITORIA DE ROTAS")
    pdf.setFont("Helvetica", 9)
    sync = relatorio.get("ultima_sincronizacao") or relatorio.get("atualizado_em") or "não informada"
    pdf.drawString(
        14 * mm,
        altura - 20 * mm,
        f"{relatorio.get('data')}  ·  {str(relatorio.get('turno') or '').capitalize()}  ·  {relatorio.get('situacao') or 'sem turno'}",
    )
    y = altura - 38 * mm
    pdf.setFillColorRGB(0.12, 0.16, 0.18)
    pdf.setFont("Helvetica", 9)
    linhas = [
        f"Última sincronização: {sync}",
        "Leitura do snapshot gravado. Nenhum histórico encerrado é recalculado.",
    ]
    if relatorio.get("sincronizacao_antiga"):
        linhas.append("Sincronização antiga no turno aberto.")
    if relatorio.get("falha_sincronizacao"):
        linhas.append("A última sincronização registrou falha.")
    if relatorio.get("incompleto"):
        linhas.append("Há informação incompleta neste turno.")
    for linha in linhas:
        pdf.drawString(14 * mm, y, linha[:110])
        y -= 5 * mm
    return y - 2 * mm


def _bloco_rota(pdf, rota: dict[str, Any], y: float, altura: float, mm) -> float:
    def nova() -> float:
        pdf.showPage()
        return altura - 16 * mm

    if y < 70 * mm:
        y = nova()
    pdf.setFont("Helvetica-Bold", 12)
    pdf.setFillColorRGB(0.07, 0.16, 0.22)
    pdf.drawString(14 * mm, y, f"Rota {rota.get('codigo') or '—'}")
    y -= 6 * mm
    pdf.setFont("Helvetica", 9)
    pdf.setFillColorRGB(0.12, 0.16, 0.18)
    apoios = ", ".join(rota.get("placas_apoio") or []) or rota.get("apoio") or "—"
    fatos = [
        f"Percentual consolidado: {rota.get('percentual_texto') or '—'}",
        f"Placa principal: {rota.get('placa_principal') or '—'}",
        f"Apoios efetivos: {apoios}",
    ]
    for linha in fatos:
        pdf.drawString(14 * mm, y, linha[:110])
        y -= 4.6 * mm
    y -= 1 * mm
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(14 * mm, y, "FONTE")
    pdf.drawString(70 * mm, y, "PERCENTUAL")
    pdf.drawString(110 * mm, y, "CONFERE")
    y -= 4.5 * mm
    pdf.setFont("Helvetica", 8)
    for item in rota.get("comparacao") or []:
        if y < 20 * mm:
            y = nova()
        pdf.drawString(14 * mm, y, str(item.get("fonte") or "")[:28])
        pdf.drawString(70 * mm, y, str(item.get("texto") or "—"))
        pdf.drawString(110 * mm, y, "sim" if item.get("confere") else "não")
        y -= 4.2 * mm
    y -= 1.5 * mm
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(14 * mm, y, "CAMINHÃO")
    pdf.drawString(48 * mm, y, "INDIVIDUAL")
    pdf.drawString(78 * mm, y, "EXCLUSIVA")
    pdf.drawString(108 * mm, y, "SOBREPOSTA")
    y -= 4.5 * mm
    pdf.setFont("Helvetica", 8)
    caminhoes = rota.get("caminhoes") or []
    if not caminhoes:
        pdf.drawString(14 * mm, y, "Nenhum caminhão no snapshot.")
        y -= 4.2 * mm
    for cam in caminhoes:
        if y < 20 * mm:
            y = nova()
        papel = cam.get("classificacao") or cam.get("papel") or ""
        pdf.drawString(14 * mm, y, f"{cam.get('placa') or '—'} {papel}"[:28])
        pdf.drawString(48 * mm, y, _texto_pct(cam.get("percentual_individual")))
        pdf.drawString(78 * mm, y, _texto_pct(cam.get("cobertura_exclusiva")))
        pdf.drawString(108 * mm, y, _texto_pct(cam.get("cobertura_sobreposta")))
        y -= 4.2 * mm
    for titulo, chave in (("Passagens", "passagens"), ("Não confirmados", "nao_confirmados")):
        itens = rota.get(chave) or []
        if not itens:
            continue
        if y < 24 * mm:
            y = nova()
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(14 * mm, y, titulo)
        y -= 4.2 * mm
        pdf.setFont("Helvetica", 8)
        for item in itens:
            if y < 18 * mm:
                y = nova()
            texto = f"{item.get('placa') or '—'}: {item.get('justificativa') or ''}"
            pdf.drawString(14 * mm, y, texto[:120])
            y -= 4.2 * mm
    for alerta in rota.get("alertas") or []:
        if y < 18 * mm:
            y = nova()
        pdf.setFillColorRGB(0.55, 0.25, 0.12)
        pdf.drawString(14 * mm, y, str(alerta)[:120])
        pdf.setFillColorRGB(0.12, 0.16, 0.18)
        y -= 4.2 * mm
    return y - 4 * mm


def _texto_pct(valor: Any) -> str:
    if valor is None:
        return "—"
    from .live_pdf import _pct

    return _pct(valor)
