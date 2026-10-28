"""A janela fixa 06:00→05:59 da tela segue o ciclo do dia."""

from __future__ import annotations

from datetime import date

from inlog_agent.server import _janela_do_ciclo

SABADO = date(2026, 10, 24)
SEXTA = date(2026, 10, 23)
QUARTA = date(2026, 10, 21)


def test_sabado_com_06_00_05_59_vai_ate_03_00_do_dia_25():
    inicio, fim, data_fim = _janela_do_ciclo(SABADO, "06:00", "05:59", "2026-10-26")
    assert inicio == "05:00"
    assert fim == "03:00"
    assert data_fim == date(2026, 10, 25)


def test_sexta_fecha_o_ciclo_as_04_59():
    inicio, fim, data_fim = _janela_do_ciclo("2026-10-23", "06:00", "05:59", None)
    assert (inicio, fim) == ("06:00", "04:59")
    assert data_fim == date(2026, 10, 24)
    vazio_ini, vazio_fim, vazio_data = _janela_do_ciclo(SEXTA, "", "", "")
    assert (vazio_ini, vazio_fim, vazio_data) == ("06:00", "04:59", date(2026, 10, 24))


def test_dia_comum_sem_janela_continua_06_00_ate_05_59():
    inicio, fim, data_fim = _janela_do_ciclo(QUARTA, None, None, None)
    assert (inicio, fim) == ("06:00", "05:59")
    assert data_fim == date(2026, 10, 22)


def test_janela_escolhida_a_mao_e_respeitada():
    inicio, fim, data_fim = _janela_do_ciclo(SABADO, "08:00", "12:00", None)
    assert (inicio, fim, data_fim) == ("08:00", "12:00", None)
    inicio, fim, data_fim = _janela_do_ciclo("2026-10-21", "08:00", "12:00", "2026-10-21")
    assert (inicio, fim, data_fim) == ("08:00", "12:00", date(2026, 10, 21))
