"""86BN opera só no domingo noturno; fora disso não entra na grade nem nos cards."""

from datetime import date, datetime

from inlog_agent.live_report import _resumo
from inlog_agent.live_sync import turno_operacional
from inlog_agent.live_turno import classificar_instante
from inlog_agent.mapa_oficial import carregar_setores
from inlog_agent.unificado import obter_rotas_previstas

DOMINGO = date(2026, 10, 25)
SEGUNDA = date(2026, 10, 19)
TERCA = date(2026, 10, 20)
SABADO = date(2026, 10, 24)
OUTRAS_BN = [f"{n}BN" for n in range(76, 86)]


def test_frequencia_no_gpkg_e_so_domingo_noturno():
    setor = next(s for s in carregar_setores(None) if str(s.get("codigo") or "").upper() == "86BN")
    assert setor["frequencia"] == "DOM - NOTURNO"

    assert [r["codigo"] for r in obter_rotas_previstas(DOMINGO, "noturno") if r["codigo"] == "86BN"] == ["86BN"]
    assert all(r["codigo"] != "86BN" for r in obter_rotas_previstas(DOMINGO, "diurno"))

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


def test_cards_do_painel_contam_86bn_so_no_domingo():
    sabado = obter_rotas_previstas(SABADO, "noturno")
    domingo = obter_rotas_previstas(DOMINGO, "noturno")

    def como_painel(previstas):
        return [
            {
                "codigo": r["codigo"],
                "na_grade": True,
                "grupo": "nao",
                "percentual": 0,
                "status": "Não iniciada",
            }
            for r in previstas
        ]

    resumo_sab = _resumo(como_painel(sabado))
    resumo_dom = _resumo(como_painel(domingo))
    assert "86BN" not in {r["codigo"] for r in sabado}
    assert "86BN" in {r["codigo"] for r in domingo}
    assert resumo_sab["previstas"] == len(sabado)
    assert resumo_dom["previstas"] == len(domingo)
    assert resumo_dom["nao_iniciadas"] == resumo_dom["previstas"]
    assert resumo_sab["nao_iniciadas"] == resumo_sab["previstas"]
