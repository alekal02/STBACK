"""Limites do sábado e a preservação dos outros dias.

Sábado diurno: 05:00 até antes das 15:00.
Sábado noturno: 15:00 até antes das 02:00 de domingo.
A data operacional do noturno continua sendo o sábado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from inlog_agent.config import janela_datetime
from inlog_agent.gps import filtrar_pontos_periodo
from inlog_agent.live_report import _periodo_padrao
from inlog_agent.live_sync import janela_ainda_aberta, turno_operacional
from inlog_agent.live_turno import (
    candidatos,
    ciclo_operacional,
    classificar_instante,
    consolidar_ate,
    fim_da_consulta,
    fim_operacional,
    horarios_do_turno,
    inicio_operacional,
    periodo_coleta,
    ponto_pertence,
    situacao_pelo_relogio,
)

SABADO = date(2026, 10, 24)
DOMINGO = date(2026, 10, 25)
SEXTA = date(2026, 10, 23)
QUARTA = date(2026, 10, 21)
QUINTA = date(2026, 10, 22)
SEGUNDA = date(2026, 10, 26)


def _quando(dia: date, texto: str) -> datetime:
    hora, minuto, segundo = (int(parte) for parte in texto.split(":"))
    return datetime(dia.year, dia.month, dia.day, hora, minuto, segundo)


def _iso(dia: date, texto: str) -> str:
    return _quando(dia, texto).strftime("%Y-%m-%dT%H:%M:%S")


def test_limites_do_sabado():
    casos = [
        (_quando(SABADO, "04:59:00"), SEXTA, "noturno"),
        (_quando(SABADO, "04:59:59"), SEXTA, "noturno"),
        (_quando(SABADO, "05:00:00"), SABADO, "diurno"),
        (_quando(SABADO, "14:59:00"), SABADO, "diurno"),
        (_quando(SABADO, "14:59:59"), SABADO, "diurno"),
        (_quando(SABADO, "15:00:00"), SABADO, "noturno"),
        (_quando(SABADO, "23:59:00"), SABADO, "noturno"),
        (_quando(DOMINGO, "00:00:00"), SABADO, "noturno"),
        (_quando(DOMINGO, "01:59:00"), SABADO, "noturno"),
        (_quando(DOMINGO, "01:59:59"), SABADO, "noturno"),
    ]
    for momento, dia, turno in casos:
        assert classificar_instante(momento) == (dia, turno)
        assert turno_operacional(momento) == (dia, turno)
        assert ponto_pertence(_iso(momento.date(), momento.strftime("%H:%M:%S")), dia, turno)
        assert situacao_pelo_relogio(dia, turno, momento) == "ABERTO"


def test_consulta_depois_da_meia_noite_corta_as_02():
    assert fim_da_consulta(SABADO, "noturno", _quando(DOMINGO, "00:00:00")) == _quando(DOMINGO, "00:00:00")
    assert fim_da_consulta(SABADO, "noturno", _quando(DOMINGO, "01:59:00")) == _quando(DOMINGO, "01:59:00")
    assert fim_da_consulta(SABADO, "noturno", _quando(DOMINGO, "02:00:00")) == _quando(DOMINGO, "01:59:59")
    assert ponto_pertence(_iso(DOMINGO, "01:59:59"), SABADO, "noturno")
    assert ponto_pertence(_iso(DOMINGO, "02:00:00"), SABADO, "noturno") is False


def test_ciclo_do_dia_acompanha_o_sabado_e_preserva_os_outros():
    assert ciclo_operacional(SABADO) == ("05:00", "01:59", DOMINGO)
    assert ciclo_operacional(SEXTA) == ("06:00", "04:59", SABADO)
    assert ciclo_operacional(QUARTA) == ("06:00", "05:59", QUINTA)
    assert ciclo_operacional(DOMINGO) == ("06:00", "05:59", SEGUNDA)


def test_domingo_as_02_nao_pertence_ao_noturno_de_sabado():
    momento = _quando(DOMINGO, "02:00:00")
    assert classificar_instante(momento) is None
    assert ponto_pertence(_iso(DOMINGO, "02:00:00"), SABADO, "noturno") is False
    assert ponto_pertence(_iso(DOMINGO, "02:00:00"), DOMINGO, "diurno") is False
    assert situacao_pelo_relogio(SABADO, "noturno", momento) != "ABERTO"
    assert turno_operacional(momento) == (DOMINGO, "diurno")
    assert situacao_pelo_relogio(DOMINGO, "diurno", momento) == ""


def test_noturno_de_sabado_finaliza_e_encerra_depois_das_02():
    assert situacao_pelo_relogio(SABADO, "noturno", _quando(DOMINGO, "02:00:00")) == "FINALIZANDO"
    assert consolidar_ate(SABADO, "noturno") == _quando(DOMINGO, "02:20:00")
    assert situacao_pelo_relogio(SABADO, "noturno", _quando(DOMINGO, "02:20:00")) == "ENCERRADO"
    fila = candidatos(_quando(DOMINGO, "02:00:00"))
    assert ("ABERTO", SABADO, "noturno") not in fila
    assert ("ABERTO", DOMINGO, "diurno") not in fila
    assert ("FINALIZANDO", SABADO, "noturno") in fila
    assert candidatos(_quando(DOMINGO, "03:00:00")) == []


def test_ponto_depois_da_meia_noite_fica_no_sabado():
    assert classificar_instante(_quando(DOMINGO, "00:30:00")) == (SABADO, "noturno")
    assert ponto_pertence("25/10/2026 00:30:00", SABADO, "noturno")
    assert ponto_pertence("25/10/2026 00:30", DOMINGO, "diurno") is False
    assert ponto_pertence("25/10/2026 01:59:59", SABADO, "noturno")
    assert ponto_pertence("25/10/2026 02:00:00", SABADO, "noturno") is False


def test_janelas_do_sabado_e_o_corte_da_sexta():
    assert horarios_do_turno(SABADO, "diurno") == ("05:00", "14:59")
    assert horarios_do_turno(SABADO, "noturno") == ("15:00", "01:59")
    assert inicio_operacional(SABADO, "diurno") == _quando(SABADO, "05:00:00")
    assert fim_operacional(SABADO, "diurno") == _quando(SABADO, "14:59:59")
    assert inicio_operacional(SABADO, "noturno") == _quando(SABADO, "15:00:00")
    assert fim_operacional(SABADO, "noturno") == _quando(DOMINGO, "01:59:59")
    assert horarios_do_turno(SEXTA, "noturno") == ("19:00", "04:59")
    assert fim_operacional(SEXTA, "noturno") == _quando(SABADO, "04:59:59")
    assert janela_ainda_aberta(SABADO, "diurno", _quando(SABADO, "14:59:59"))
    assert janela_ainda_aberta(SABADO, "diurno", _quando(SABADO, "15:00:00")) is False
    assert janela_ainda_aberta(SABADO, "noturno", _quando(DOMINGO, "01:59:59"))
    assert janela_ainda_aberta(SABADO, "noturno", _quando(DOMINGO, "02:00:00")) is False


def test_historico_gps_cobertura_e_relatorio_usam_a_mesma_janela():
    for turno in ("diurno", "noturno"):
        inicio, fim, dia_fim = periodo_coleta(SABADO, turno)
        assert janela_datetime(SABADO, inicio, fim, dia_fim) == (
            inicio_operacional(SABADO, turno),
            fim_operacional(SABADO, turno),
        )
    diurno = filtrar_pontos_periodo(
        [
            {"timestamp": _iso(SABADO, "04:59:00")},
            {"timestamp": _iso(SABADO, "05:00:00")},
            {"timestamp": _iso(SABADO, "14:59:00")},
            {"timestamp": _iso(SABADO, "15:00:00")},
        ],
        SABADO,
        *periodo_coleta(SABADO, "diurno")[:2],
        data_fim=periodo_coleta(SABADO, "diurno")[2],
    )
    assert [item["timestamp"] for item in diurno] == [
        _iso(SABADO, "05:00:00"),
        _iso(SABADO, "14:59:00"),
    ]
    noturno = filtrar_pontos_periodo(
        [
            {"timestamp": _iso(SABADO, "14:59:59")},
            {"timestamp": _iso(SABADO, "15:00:00")},
            {"timestamp": _iso(SABADO, "23:59:00")},
            {"timestamp": _iso(DOMINGO, "00:00:00")},
            {"timestamp": _iso(DOMINGO, "01:59:00")},
            {"timestamp": _iso(DOMINGO, "02:00:00")},
        ],
        SABADO,
        *periodo_coleta(SABADO, "noturno")[:2],
        data_fim=periodo_coleta(SABADO, "noturno")[2],
    )
    assert [item["timestamp"] for item in noturno] == [
        _iso(SABADO, "15:00:00"),
        _iso(SABADO, "23:59:00"),
        _iso(DOMINGO, "00:00:00"),
        _iso(DOMINGO, "01:59:00"),
    ]
    assert "05:00" in _periodo_padrao(SABADO, "diurno")
    assert "14:59" in _periodo_padrao(SABADO, "diurno")
    assert "15:00" in _periodo_padrao(SABADO, "noturno")
    assert "01:59" in _periodo_padrao(SABADO, "noturno")


def test_demais_dias_permanecem_iguais():
    assert horarios_do_turno(QUARTA, "diurno") == ("06:00", "18:59")
    assert horarios_do_turno(QUARTA, "noturno") == ("19:00", "05:59")
    assert horarios_do_turno(DOMINGO, "diurno") == ("06:00", "18:59")
    assert horarios_do_turno(DOMINGO, "noturno") == ("19:00", "05:59")
    assert horarios_do_turno(SEXTA, "diurno") == ("06:00", "18:59")
    preservados = [
        (_quando(QUARTA, "05:59:59"), QUARTA - timedelta(days=1), "noturno"),
        (_quando(QUARTA, "06:00:00"), QUARTA, "diurno"),
        (_quando(QUARTA, "18:59:59"), QUARTA, "diurno"),
        (_quando(QUARTA, "19:00:00"), QUARTA, "noturno"),
        (_quando(QUINTA, "05:59:59"), QUARTA, "noturno"),
        (_quando(SEXTA, "18:59:59"), SEXTA, "diurno"),
        (_quando(SEXTA, "19:00:00"), SEXTA, "noturno"),
        (_quando(DOMINGO, "06:00:00"), DOMINGO, "diurno"),
        (_quando(DOMINGO, "18:59:59"), DOMINGO, "diurno"),
        (_quando(DOMINGO, "19:00:00"), DOMINGO, "noturno"),
        (_quando(SEGUNDA, "05:59:59"), DOMINGO, "noturno"),
        (_quando(SEGUNDA, "06:00:00"), SEGUNDA, "diurno"),
    ]
    for momento, dia, turno in preservados:
        assert classificar_instante(momento) == (dia, turno)
        assert fim_operacional(QUARTA, "noturno") == _quando(QUINTA, "05:59:59")
        assert fim_operacional(DOMINGO, "noturno") == _quando(SEGUNDA, "05:59:59")
