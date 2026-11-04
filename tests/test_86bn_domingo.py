"""86BN opera só no domingo, em diurno ou noturno (varia por semana)."""

import os
import tempfile
from datetime import date, datetime
from pathlib import Path

os.environ["SAGTRACK_LIVE_DB"] = str(Path(tempfile.mkdtemp()) / "live.db")
os.environ["SAGTRACK_LIVE_SYNC"] = "0"

from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno
from inlog_agent.live_report import _exibir_classificacao, _resumo
from inlog_agent.live_sync import ler_painel, turno_operacional
from inlog_agent.live_turno import classificar_instante
from inlog_agent.mapa_oficial import carregar_setores
from inlog_agent.unificado import (
    _casa_turno,
    _turno_flexivel,
    ajustar_rotas_turno_flexivel,
    obter_rotas_previstas,
)

DOMINGO = date(2026, 10, 25)
SEGUNDA = date(2026, 10, 19)
TERCA = date(2026, 10, 20)
SABADO = date(2026, 10, 24)
OUTRAS_BN = [f"{n}BN" for n in range(76, 86)]
FREQ = "DOM - DIURNO/NOTURNO"
SAIDA = Path(tempfile.mkdtemp())


def _rota(codigo="86BN", pct=0, turno_freq=FREQ, na_grade=True, grupo=None):
    if grupo is None:
        grupo = "nao" if pct <= 0 else ("concluida" if pct >= 100 else "parcial")
    status = {
        "nao": "Não iniciada",
        "parcial": "Parcial",
        "concluida": "Concluída",
    }.get(grupo, "Não iniciada")
    return {
        "codigo": codigo,
        "rota": f"{codigo} - TESTE",
        "frequencia": turno_freq,
        "veiculo": "CAM" if pct else "",
        "placa": "ABC-1D23" if pct else "",
        "status": status,
        "grupo": grupo,
        "percentual": pct,
        "inicio": "",
        "ultimo_gps": "",
        "tempo_em_rota": "",
        "km_coleta": "",
        "km_oficial": "",
        "na_grade": na_grade,
    }


def test_frequencia_gpkg_aceita_domingo_qualquer_turno():
    setor = next(s for s in carregar_setores(None) if str(s.get("codigo") or "").upper() == "86BN")
    assert setor["frequencia"] == FREQ
    assert _turno_flexivel(FREQ)
    assert _casa_turno(FREQ, "diurno")
    assert _casa_turno(FREQ, "noturno")
    assert not _turno_flexivel("DOM - NOTURNO")
    assert not _casa_turno("DOM - NOTURNO", "diurno")
    assert _casa_turno("DOM - NOTURNO", "noturno")


def test_86bn_aparece_nos_dois_turnos_do_domingo():
    assert [r["codigo"] for r in obter_rotas_previstas(DOMINGO, "diurno") if r["codigo"] == "86BN"] == ["86BN"]
    assert [r["codigo"] for r in obter_rotas_previstas(DOMINGO, "noturno") if r["codigo"] == "86BN"] == ["86BN"]

    for dia in (SEGUNDA, TERCA, SABADO):
        assert all(r["codigo"] != "86BN" for r in obter_rotas_previstas(dia, "diurno"))
        assert all(r["codigo"] != "86BN" for r in obter_rotas_previstas(dia, "noturno"))


def test_outras_bn_seguem_ter_qui_sab():
    terca = {r["codigo"] for r in obter_rotas_previstas(TERCA, "noturno")}
    sabado = {r["codigo"] for r in obter_rotas_previstas(SABADO, "noturno")}
    for codigo in OUTRAS_BN:
        assert codigo in terca
        assert codigo in sabado
    assert "86BN" not in terca
    assert "86BN" not in sabado


def test_madrugada_de_domingo_ainda_e_sabado_operacional_sem_86bn():
    """Noturno de sábado vai até 03:00 de domingo; 86BN não pode entrar nessa janela."""
    madrugada = datetime(2026, 10, 25, 2, 30, 0)
    assert classificar_instante(madrugada) == (SABADO, "noturno")
    assert turno_operacional(madrugada) == (SABADO, "noturno")
    assert all(r["codigo"] != "86BN" for r in obter_rotas_previstas(SABADO, "noturno"))


def test_sem_execucao_mostra_nao_iniciada_no_turno_atual():
    diurno = ajustar_rotas_turno_flexivel([_rota(pct=0)], "diurno", {})
    noturno = ajustar_rotas_turno_flexivel([_rota(pct=0)], "noturno", {})
    assert [r["codigo"] for r in diurno] == ["86BN"]
    assert [r["codigo"] for r in noturno] == ["86BN"]
    assert _resumo(diurno)["nao_iniciadas"] == 1
    assert _resumo(noturno)["nao_iniciadas"] == 1


def test_executada_no_diurno_nao_aparece_como_nao_iniciada_no_noturno():
    feita = _rota(pct=92)
    diurno = ajustar_rotas_turno_flexivel([feita], "diurno", {"86BN": 0})
    noturno = ajustar_rotas_turno_flexivel([_rota(pct=0)], "noturno", {"86BN": 92})
    assert [r["codigo"] for r in diurno] == ["86BN"]
    assert _resumo(diurno)["parciais"] == 1
    assert _resumo(diurno)["nao_iniciadas"] == 0
    assert noturno == []
    assert _resumo(noturno)["previstas"] == 0
    assert _resumo(noturno)["nao_iniciadas"] == 0


def test_executada_no_noturno_nao_aparece_como_nao_iniciada_no_diurno():
    diurno = ajustar_rotas_turno_flexivel([_rota(pct=0)], "diurno", {"86BN": 100})
    noturno = ajustar_rotas_turno_flexivel([_rota(pct=100)], "noturno", {"86BN": 0})
    assert diurno == []
    assert [r["codigo"] for r in noturno] == ["86BN"]
    assert _resumo(noturno)["concluidas"] == 1
    assert _resumo(diurno)["nao_iniciadas"] == 0


def test_nunca_conta_em_dobro_nos_cards():
    diurno = ajustar_rotas_turno_flexivel([_rota(pct=88)], "diurno", {"86BN": 88})
    noturno = ajustar_rotas_turno_flexivel([_rota(pct=88)], "noturno", {"86BN": 88})
    assert _resumo(diurno)["previstas"] == 1
    assert _resumo(noturno)["previstas"] == 0
    assert diurno[0]["na_grade"] is True
    assert noturno[0]["na_grade"] is False


def test_painel_sqlite_tres_cenarios():
    dia = DOMINGO.isoformat()
    con = conectar()
    for turno in ("diurno", "noturno"):
        gravar_turno(
            con,
            {
                "data": dia,
                "turno": turno,
                "aberto": 1,
                "situacao": "ABERTO",
                "periodo": f"{dia} {turno}",
                "hora_inicio": "06:00" if turno == "diurno" else "19:00",
                "hora_fim": "18:59" if turno == "diurno" else "05:59",
                "data_fim": dia if turno == "diurno" else "2026-10-26",
                "atualizado_em": f"{dia}T12:00:00",
            },
        )

    # Sem execução: aparece como Não iniciada no turno atual.
    gravar_rotas(con, dia, "diurno", [_rota(pct=0)])
    gravar_rotas(con, dia, "noturno", [_rota(pct=0)])
    con.commit()
    painel_d = _exibir_classificacao(ler_painel(SAIDA, dia, "diurno"))
    painel_n = _exibir_classificacao(ler_painel(SAIDA, dia, "noturno"))
    assert "86BN" in {r["codigo"] for r in painel_d["rotas"]}
    assert "86BN" in {r["codigo"] for r in painel_n["rotas"]}
    assert painel_d["resumo"]["nao_iniciadas"] == 1
    assert painel_n["resumo"]["nao_iniciadas"] == 1

    # Executada no diurno: some do noturno e não vira "Não iniciada" lá.
    gravar_rotas(con, dia, "diurno", [_rota(pct=85)])
    con.commit()
    painel_d = _exibir_classificacao(ler_painel(SAIDA, dia, "diurno"))
    painel_n = _exibir_classificacao(ler_painel(SAIDA, dia, "noturno"))
    assert painel_d["resumo"]["parciais"] == 1
    assert "86BN" not in {r["codigo"] for r in painel_n["rotas"]}
    assert painel_n["resumo"]["nao_iniciadas"] == 0

    # Executada só no noturno: some do diurno.
    gravar_rotas(con, dia, "diurno", [_rota(pct=0)])
    gravar_rotas(con, dia, "noturno", [_rota(pct=100)])
    con.commit()
    con.close()
    painel_d = _exibir_classificacao(ler_painel(SAIDA, dia, "diurno"))
    painel_n = _exibir_classificacao(ler_painel(SAIDA, dia, "noturno"))
    assert "86BN" not in {r["codigo"] for r in painel_d["rotas"]}
    assert painel_d["resumo"]["nao_iniciadas"] == 0
    assert painel_n["resumo"]["concluidas"] == 1
