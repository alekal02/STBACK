"""Cobertura viaria: a mesma razao do verde e do vermelho."""

from inlog_agent.cobertura import percentual_das_vias
from inlog_agent.live_report import _faixa_percentual


def test_29bd_permanece_parcial():
    pct = percentual_das_vias(37193.2, 116.6)
    assert pct == 99.7
    assert _faixa_percentual(pct) == ("parcial", "Parcial")


def test_cem_so_sem_via_faltante():
    assert percentual_das_vias(12000, 0) == 100.0
    assert _faixa_percentual(100) == ("concluida", "Concluída")
    assert percentual_das_vias(9999.6, 0.4) == 99.9
    assert _faixa_percentual(99.9)[0] == "parcial"


def test_zero_e_resto_pequeno():
    assert percentual_das_vias(0, 40000) == 0.0
    assert _faixa_percentual(0) == ("nao", "Não iniciada")
    assert _faixa_percentual(0.4) == ("parcial", "Parcial")
    assert percentual_das_vias(1, 40000) == 0.1
