"""Ciclo de vida do turno. Não recalcula cobertura e não chama a Fulltrack.

Horários em Brasília. A data do noturno é o dia em que ele começa.
ABERTO e FINALIZANDO mantêm aberto = 1. ENCERRADO grava aberto = 0.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as hora, timedelta
from typing import Any

JANELA = timedelta(minutes=20)
_SITUACOES = {"ABERTO", "FINALIZANDO", "ENCERRADO"}


def agora_operacional(agora: datetime | None = None) -> datetime:
    if agora is not None:
        return agora.replace(tzinfo=None)
    from .live_sync import agora_brt

    return agora_brt()


def inicio_operacional(dia: date, turno: str) -> datetime:
    if turno == "noturno":
        return datetime.combine(dia, hora(19, 0, 0))
    return datetime.combine(dia, hora(6, 0, 0))


def fim_operacional(dia: date, turno: str) -> datetime:
    if turno == "noturno":
        return datetime.combine(dia + timedelta(days=1), hora(5, 59, 59))
    return datetime.combine(dia, hora(18, 59, 59))


def consolidar_ate(dia: date, turno: str) -> datetime:
    """19:20:00 e 06:20:00. Nesse instante o turno já está encerrado."""
    return fim_operacional(dia, turno) + JANELA + timedelta(seconds=1)


def situacao_pelo_relogio(dia: date, turno: str, agora: datetime | None = None) -> str:
    momento = agora_operacional(agora)
    inicio = inicio_operacional(dia, turno)
    if momento < inicio:
        return ""
    if momento <= fim_operacional(dia, turno):
        return "ABERTO"
    if momento < consolidar_ate(dia, turno):
        return "FINALIZANDO"
    return "ENCERRADO"


def fim_da_consulta(dia: date, turno: str, agora: datetime | None = None) -> datetime:
    """Teto dos GPS. Finalizando às 19:10 ainda corta em 18:59:59."""
    momento = agora_operacional(agora)
    return min(momento, fim_operacional(dia, turno))


def instante_do_ponto(texto: str) -> datetime | None:
    from .live_sync import _parse_ts

    return _parse_ts(str(texto or ""))


def ponto_pertence(texto: str, dia: date, turno: str) -> bool:
    lido = instante_do_ponto(texto)
    if lido is None:
        return False
    return inicio_operacional(dia, turno) <= lido <= fim_operacional(dia, turno)


def confirmacao_esperada(dia: date, turno: str) -> str:
    return f"{dia.day:02d}/{dia.month:02d}/{dia.year} {turno.upper()}"


def confirmacao_confere(texto: str, dia: date, turno: str) -> bool:
    return " ".join(str(texto or "").strip().upper().split()) == confirmacao_esperada(dia, turno)


def candidatos(agora: datetime | None = None) -> list[tuple[str, date, str]]:
    """Turno ABERTO primeiro, FINALIZANDO depois. No máximo um de cada."""
    from .live_sync import turno_operacional

    momento = agora_operacional(agora)
    dia, turno = turno_operacional(momento)
    fila = [("ABERTO", dia, turno)]
    if turno == "noturno":
        anterior = (dia, "diurno")
    else:
        anterior = (dia - timedelta(days=1), "noturno")
    if situacao_pelo_relogio(anterior[0], anterior[1], momento) == "FINALIZANDO":
        fila.append(("FINALIZANDO", anterior[0], anterior[1]))
    return fila


def _duracao(abertura: str, fechamento: str) -> str:
    ini = instante_do_ponto(abertura)
    fim = instante_do_ponto(fechamento)
    if ini is None or fim is None or fim < ini:
        return ""
    minutos = int((fim - ini).total_seconds() // 60)
    horas, resto = divmod(minutos, 60)
    if horas:
        return f"{horas} h {resto} min"
    return f"{resto} min"


def montar_resumo(con, dia: date, turno: str, fechado_em: str) -> dict[str, Any]:
    """A mesma conta de _resumo. Não grava nas rotas."""
    from .live_db import ler_view
    from .live_report import _resumo

    view = ler_view(con, dia.isoformat(), turno)
    rotas = (view or {}).get("rotas") or []
    resumo = _resumo(rotas)
    extremo = con.execute(
        "SELECT MIN(ts), MAX(ts) FROM ponto WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    primeiro = str(extremo[0] or "") if extremo else ""
    ultimo = str(extremo[1] or "") if extremo else ""
    cab = con.execute(
        "SELECT aberto_em FROM turno WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    abertura = str((cab["aberto_em"] if cab else "") or "") or inicio_operacional(dia, turno).isoformat(timespec="seconds")
    resumo.update(
        {
            "primeiro_gps": primeiro,
            "ultimo_gps": ultimo,
            "aberto_em": abertura,
            "fechado_em": fechado_em,
            "duracao_operacional": _duracao(abertura, fechado_em),
        }
    )
    return resumo


def _auditar(
    con,
    dia: date,
    turno: str,
    acao: str,
    origem: str,
    antes: str,
    depois: str,
    quando: str,
    usuario: str | None,
) -> None:
    con.execute(
        """
        INSERT INTO turno_evento (data, turno, acao, quando, origem, usuario, antes, depois)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (dia.isoformat(), turno, acao, quando, origem, usuario, antes, depois),
    )


def encerrar_turno(
    con,
    dia: date,
    turno: str,
    origem: str,
    agora: datetime | None = None,
    *,
    usuario: str | None = None,
    acao: str = "ENCERRAMENTO_AUTOMATICO",
) -> dict[str, Any]:
    momento = agora_operacional(agora)
    limite = consolidar_ate(dia, turno)
    if origem == "manual":
        carimbo = momento
    elif momento >= limite:
        carimbo = limite
    else:
        carimbo = momento
    quando = carimbo.isoformat(timespec="seconds")
    row = con.execute(
        "SELECT situacao, aberto_em FROM turno WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    if row is None:
        return {"ok": False, "erro": "Turno não encontrado."}
    antes = str(row["situacao"] or "")
    if antes == "ENCERRADO":
        return {"ok": True, "ja_encerrado": True, "situacao": "ENCERRADO"}
    if not str(row["aberto_em"] or ""):
        con.execute(
            "UPDATE turno SET aberto_em = ? WHERE data = ? AND turno = ?",
            (inicio_operacional(dia, turno).isoformat(timespec="seconds"), dia.isoformat(), turno),
        )
    resumo = montar_resumo(con, dia, turno, quando)
    con.execute(
        """
        UPDATE turno SET
            situacao = 'ENCERRADO',
            aberto = 0,
            fechado_em = ?,
            fechado_origem = ?,
            consolidar_ate = ?,
            primeiro_gps = ?,
            ultimo_gps_turno = ?,
            resumo_json = ?,
            atualizado_em = ?
        WHERE data = ? AND turno = ? AND situacao != 'ENCERRADO'
        """,
        (
            quando,
            origem,
            consolidar_ate(dia, turno).isoformat(timespec="seconds"),
            resumo.get("primeiro_gps") or "",
            resumo.get("ultimo_gps") or "",
            json.dumps(resumo, ensure_ascii=False, separators=(",", ":")),
            quando,
            dia.isoformat(),
            turno,
        ),
    )
    if con.execute("SELECT changes()").fetchone()[0] == 0:
        return {"ok": True, "ja_encerrado": True, "situacao": "ENCERRADO"}
    _auditar(con, dia, turno, acao, origem, antes or "ABERTO", "ENCERRADO", quando, usuario)
    return {"ok": True, "situacao": "ENCERRADO", "resumo": resumo, "fechado_em": quando}


def aplicar_relogio(con, dia: date, turno: str, agora: datetime | None = None) -> str:
    """Atualiza a situação pelo relógio. Encerrado não reabre. Não chama a Fulltrack."""
    momento = agora_operacional(agora)
    row = con.execute(
        "SELECT situacao, aberto_em FROM turno WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    if row is None:
        return situacao_pelo_relogio(dia, turno, momento)
    atual = str(row["situacao"] or "")
    if atual == "ENCERRADO":
        return "ENCERRADO"
    alvo = situacao_pelo_relogio(dia, turno, momento)
    if alvo == "":
        return atual
    if alvo == "ENCERRADO":
        encerrar_turno(con, dia, turno, "automatico", momento)
        return "ENCERRADO"
    abertura = str(row["aberto_em"] or "") or inicio_operacional(dia, turno).isoformat(timespec="seconds")
    con.execute(
        """
        UPDATE turno SET situacao = ?, aberto = 1, aberto_em = ?, consolidar_ate = ?
        WHERE data = ? AND turno = ?
        """,
        (
            alvo,
            abertura,
            consolidar_ate(dia, turno).isoformat(timespec="seconds"),
            dia.isoformat(),
            turno,
        ),
    )
    return alvo


def avaliar_persistidos(con, agora: datetime | None = None) -> list[str]:
    """Encerra quem passou da janela, mesmo que o ciclo anterior tenha atrasado."""
    momento = agora_operacional(agora)
    encerrados = []
    linhas = con.execute("SELECT data, turno, situacao FROM turno").fetchall()
    for linha in linhas:
        dia = date.fromisoformat(str(linha["data"]))
        turno = str(linha["turno"])
        antes = str(linha["situacao"] or "")
        if antes == "ENCERRADO":
            continue
        alvo = situacao_pelo_relogio(dia, turno, momento)
        if alvo == "":
            continue
        if alvo == "ENCERRADO":
            encerrar_turno(con, dia, turno, "automatico", momento)
            encerrados.append(f"{linha['data']} {turno}")
        else:
            aplicar_relogio(con, dia, turno, momento)
    return encerrados


def classificar_vazios(con, agora: datetime | None = None) -> None:
    """Migração. Não mexe em ponto, rota ou percentual. Não chama a Fulltrack."""
    momento = agora_operacional(agora)
    linhas = con.execute(
        "SELECT data, turno, situacao FROM turno WHERE situacao IS NULL OR situacao = ''"
    ).fetchall()
    for linha in linhas:
        try:
            dia = date.fromisoformat(str(linha["data"]))
            turno = str(linha["turno"])
            alvo = situacao_pelo_relogio(dia, turno, momento)
            if alvo == "":
                continue
            if alvo == "ENCERRADO":
                encerrar_turno(con, dia, turno, "migracao", momento, acao="ENCERRAMENTO_AUTOMATICO")
            else:
                aplicar_relogio(con, dia, turno, momento)
        except Exception as exc:
            print(f"Migração do turno {linha['data']} {linha['turno']} falhou: {exc}", flush=True)


def turno_aceita_sync(con, dia: date, turno: str, agora: datetime | None = None) -> bool:
    momento = agora_operacional(agora)
    row = con.execute(
        "SELECT situacao FROM turno WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    if row is not None and str(row["situacao"] or "") == "ENCERRADO":
        return False
    return situacao_pelo_relogio(dia, turno, momento) in {"ABERTO", "FINALIZANDO"}


def reabrir_turno(
    con,
    dia: date,
    turno: str,
    agora: datetime | None = None,
    *,
    usuario: str | None = None,
) -> dict[str, Any]:
    momento = agora_operacional(agora)
    row = con.execute(
        "SELECT situacao FROM turno WHERE data = ? AND turno = ?",
        (dia.isoformat(), turno),
    ).fetchone()
    if row is None:
        return {"ok": False, "erro": "Turno não encontrado."}
    alvo = situacao_pelo_relogio(dia, turno, momento)
    if alvo not in {"ABERTO", "FINALIZANDO"}:
        return {
            "ok": False,
            "aplicado": False,
            "erro": (
                "Este turno está fora da janela. Reabri-lo sem um estado próprio "
                "colocaria a Fulltrack para baixar o histórico. Nada foi alterado."
            ),
            "codigo": "historico_antigo",
        }
    antes = str(row["situacao"] or "")
    quando = momento.isoformat(timespec="seconds")
    con.execute(
        """
        UPDATE turno SET
            situacao = ?,
            aberto = 1,
            fechado_em = '',
            fechado_origem = '',
            consolidar_ate = ?,
            atualizado_em = ?
        WHERE data = ? AND turno = ?
        """,
        (
            alvo,
            consolidar_ate(dia, turno).isoformat(timespec="seconds"),
            quando,
            dia.isoformat(),
            turno,
        ),
    )
    _auditar(con, dia, turno, "REABERTURA", "manual", antes, alvo, quando, usuario)
    return {"ok": True, "situacao": alvo, "aplicado": True}


def resumo_guardado(bruto: str) -> dict[str, Any] | None:
    texto = str(bruto or "").strip()
    if not texto:
        return None
    try:
        dado = json.loads(texto)
    except json.JSONDecodeError:
        return None
    return dado if isinstance(dado, dict) else None

