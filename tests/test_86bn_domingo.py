"""86BN opera só no domingo, em diurno ou noturno (varia por semana)."""

import json
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
from inlog_agent.mapa_oficial import _freq_do_dia, carregar_setores
from inlog_agent.unificado import (
    _casa_turno,
    _na_programacao,
    _turno_flexivel,
    ajustar_rotas_turno_flexivel,
    filtrar_rotas_frequencia_atual,
    obter_rotas_previstas,
)

DOMINGO = date(2026, 10, 25)
SEGUNDA = date(2026, 10, 19)
TERCA = date(2026, 10, 20)
TERCA_PROD = date(2026, 11, 3)
DOMINGO_PROD = date(2026, 11, 8)
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
    # DIURNO/NOTURNO não é diário: o dia da semana continua valendo.
    assert _freq_do_dia(FREQ, DOMINGO)
    assert not _freq_do_dia(FREQ, TERCA)
    assert not _freq_do_dia(FREQ, TERCA_PROD)
    assert _na_programacao(FREQ, DOMINGO, "diurno")
    assert _na_programacao(FREQ, DOMINGO, "noturno")
    assert not _na_programacao(FREQ, TERCA_PROD, "diurno")
    assert not _na_programacao(FREQ, TERCA_PROD, "noturno")
    assert not _freq_do_dia("DIURNO/NOTURNO", TERCA_PROD)
    assert not _na_programacao("DIURNO/NOTURNO", TERCA_PROD, "noturno")


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


def _seed_turno(con, dia: date, turno: str, rotas, *, situacao="ABERTO", resumo_json=""):
    aberto = 0 if situacao == "ENCERRADO" else 1
    fim = dia.isoformat() if turno == "diurno" else date.fromordinal(dia.toordinal() + 1).isoformat()
    gravar_turno(
        con,
        {
            "data": dia.isoformat(),
            "turno": turno,
            "aberto": aberto,
            "periodo": f"{dia.isoformat()} {turno}",
            "hora_inicio": "06:00" if turno == "diurno" else "19:00",
            "hora_fim": "18:59" if turno == "diurno" else "05:59",
            "data_fim": fim,
            "atualizado_em": f"{dia.isoformat()}T12:00:00",
        },
    )
    con.execute(
        "UPDATE turno SET situacao = ?, aberto = ?, resumo_json = ? WHERE data = ? AND turno = ?",
        (situacao, aberto, resumo_json, dia.isoformat(), turno),
    )
    gravar_rotas(con, dia.isoformat(), turno, rotas)


def _rotas_seed(previstas):
    return [
        {
            "codigo": r["codigo"],
            "rota": f"{r['codigo']} - X",
            "frequencia": r.get("frequencia") or "",
            "veiculo": "",
            "placa": "",
            "status": "Não iniciada",
            "grupo": "nao",
            "percentual": 0,
            "inicio": "",
            "ultimo_gps": "",
            "tempo_em_rota": "",
            "km_coleta": "",
            "km_oficial": "",
            "na_grade": True,
        }
        for r in previstas
    ]


def test_snapshot_terca_remove_86bn_mesmo_com_estado_antigo():
    """Estado salvo antes da mudança de frequência: 86BN some na terça e volta no domingo."""
    previstas_ter = obter_rotas_previstas(TERCA_PROD, "noturno")
    assert all(r["codigo"] != "86BN" for r in previstas_ter)
    assert any(r["codigo"] == "85BN" for r in previstas_ter)

    stale = _rota(codigo="86BN", pct=0, turno_freq="TER/QUI/SAB - NOTURNO", na_grade=True)
    con = conectar()
    for turno in ("diurno", "noturno"):
        base = _rotas_seed(obter_rotas_previstas(TERCA_PROD, turno))
        _seed_turno(
            con,
            TERCA_PROD,
            turno,
            base + [stale],
            situacao="ENCERRADO",
            resumo_json=json.dumps(
                {
                    "previstas": len(base) + 1,
                    "concluidas": 0,
                    "parciais": 0,
                    "nao_iniciadas": len(base) + 1,
                    "apoios": 0,
                    "percentual_geral": 0,
                    "fechado_em": f"{TERCA_PROD.isoformat()}T06:00:00",
                }
            ),
        )
    con.commit()
    con.close()

    for turno in ("diurno", "noturno"):
        painel = ler_painel(SAIDA, TERCA_PROD.isoformat(), turno)
        codigos = {r["codigo"] for r in painel["rotas"]}
        assert "86BN" not in codigos
        esperadas = {r["codigo"] for r in obter_rotas_previstas(TERCA_PROD, turno)}
        assert codigos == esperadas
        assert painel["resumo"]["previstas"] == len(esperadas)
        assert painel["resumo"]["nao_iniciadas"] == len(esperadas)
        assert painel["resumo"].get("fechado_em") == f"{TERCA_PROD.isoformat()}T06:00:00"

    # Domingo seguinte: 86BN volta nos dois turnos.
    con = conectar()
    for turno in ("diurno", "noturno"):
        base = _rotas_seed(obter_rotas_previstas(DOMINGO_PROD, turno))
        assert any(r["codigo"] == "86BN" for r in base)
        _seed_turno(con, DOMINGO_PROD, turno, base)
    con.commit()
    con.close()
    for turno in ("diurno", "noturno"):
        painel = ler_painel(SAIDA, DOMINGO_PROD.isoformat(), turno)
        assert "86BN" in {r["codigo"] for r in painel["rotas"]}
        assert painel["resumo"]["previstas"] == len(obter_rotas_previstas(DOMINGO_PROD, turno))


def test_filtrar_rotas_frequencia_atual_puro():
    terca = filtrar_rotas_frequencia_atual(
        [_rota(pct=0), _rota(codigo="85BN", pct=0, turno_freq="TER/QUI/SAB - NOTURNO")],
        TERCA_PROD,
        "noturno",
    )
    assert [r["codigo"] for r in terca] == ["85BN"]
    assert terca[0]["frequencia"]  # veio do GPKG
    domingo = filtrar_rotas_frequencia_atual([_rota(pct=0)], DOMINGO_PROD, "noturno")
    assert [r["codigo"] for r in domingo] == ["86BN"]
    assert domingo[0]["frequencia"] == FREQ
