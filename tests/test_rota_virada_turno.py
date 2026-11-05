"""Rotas que terminam depois da virada permanecem no turno de origem."""

from __future__ import annotations

from datetime import date, datetime

from inlog_agent.live_db import conectar, gravar_rotas, gravar_turno, ler_view
from inlog_agent.live_turno import campos_de_horario
from inlog_agent.live_virada import (
    carimbar_origem,
    filtrar_rotas_virada,
    ler_estado_virada,
    ler_rotas_turno_anterior,
    origem_do_inicio,
    turno_anterior,
)


def _turno(con, data: str, turno: str, situacao: str = "ABERTO") -> None:
    campos = campos_de_horario(date.fromisoformat(data), turno)
    gravar_turno(
        con,
        {
            "data": data,
            "turno": turno,
            "aberto": 0 if situacao == "ENCERRADO" else 1,
            "periodo": data,
            "hora_inicio": campos["hora_inicio"],
            "hora_fim": campos["hora_fim"],
            "data_fim": campos["data_fim"],
            "atualizado_em": data + "T12:00:00",
        },
    )
    con.execute(
        "UPDATE turno SET situacao = ?, aberto = ? WHERE data = ? AND turno = ?",
        (situacao, 0 if situacao == "ENCERRADO" else 1, data, turno),
    )


def test_origem_respeita_sabado_e_turno_anterior():
    sabado = date(2026, 10, 24)
    assert origem_do_inicio("2026-10-24T14:50:00", sabado.isoformat(), "diurno") == (
        sabado.isoformat(),
        "diurno",
    )
    assert origem_do_inicio("2026-10-24T15:10:00", sabado.isoformat(), "noturno") == (
        sabado.isoformat(),
        "noturno",
    )
    assert origem_do_inicio("2026-10-25T02:40:00", "2026-10-25", "diurno") == (
        sabado.isoformat(),
        "noturno",
    )
    assert turno_anterior(sabado, "noturno") == (sabado, "diurno")
    assert turno_anterior(date(2026, 10, 25), "diurno") == (sabado, "noturno")


def test_gravar_inicio_carimba_origem_e_conclusao_fica_no_turno(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    con = conectar()
    _turno(con, "2026-10-24", "diurno", "ENCERRADO")
    _turno(con, "2026-10-24", "noturno", "ABERTO")
    gravar_rotas(
        con,
        "2026-10-24",
        "diurno",
        [
            {
                "codigo": "01AD",
                "rota": "01AD",
                "status": "Parcial",
                "grupo": "parcial",
                "percentual": 40,
                "inicio": "2026-10-24T14:20:00",
                "na_grade": True,
                "placa": "ABC1D23",
                "_geo": True,
            }
        ],
    )
    con.commit()
    row = con.execute(
        "SELECT data_turno, turno_origem, concluido_em, grupo FROM rota WHERE codigo = '01AD'"
    ).fetchone()
    assert row["data_turno"] == "2026-10-24"
    assert row["turno_origem"] == "diurno"
    assert row["concluido_em"] == ""

    gravar_rotas(
        con,
        "2026-10-24",
        "diurno",
        [
            {
                "codigo": "01AD",
                "rota": "01AD",
                "status": "Concluída",
                "grupo": "concluida",
                "percentual": 100,
                "inicio": "2026-10-24T14:20:00",
                "ultimo_gps": "2026-10-24T15:25:00",
                "saida_hora": "2026-10-24T15:25:00",
                "na_grade": True,
                "placa": "ABC1D23",
                "_geo": True,
            }
        ],
    )
    con.commit()
    row = con.execute(
        """
        SELECT data, turno, data_turno, turno_origem, grupo, percentual, concluido_em
        FROM rota WHERE codigo = '01AD'
        """
    ).fetchone()
    con.close()
    assert row["data"] == "2026-10-24"
    assert row["turno"] == "diurno"
    assert row["data_turno"] == "2026-10-24"
    assert row["turno_origem"] == "diurno"
    assert row["grupo"] == "concluida"
    assert float(row["percentual"]) == 100
    assert row["concluido_em"].startswith("2026-10-24T15:25")


def test_endpoint_virada_traz_abertas_e_concluidas_apos_virada(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    con = conectar()
    _turno(con, "2026-10-24", "diurno", "ENCERRADO")
    _turno(con, "2026-10-24", "noturno", "ABERTO")
    gravar_rotas(
        con,
        "2026-10-24",
        "diurno",
        [
            {
                "codigo": "ABERTA1",
                "rota": "ABERTA1",
                "status": "Parcial",
                "grupo": "parcial",
                "percentual": 55,
                "inicio": "2026-10-24T14:10:00",
                "na_grade": True,
                "placa": "AAA0A00",
            },
            {
                "codigo": "TARDE1",
                "rota": "TARDE1",
                "status": "Concluída",
                "grupo": "concluida",
                "percentual": 100,
                "inicio": "2026-10-24T13:00:00",
                "saida_hora": "2026-10-24T15:40:00",
                "concluido_em": "2026-10-24T15:40:00",
                "na_grade": True,
                "placa": "BBB1B11",
            },
            {
                "codigo": "CEDO1",
                "rota": "CEDO1",
                "status": "Concluída",
                "grupo": "concluida",
                "percentual": 100,
                "inicio": "2026-10-24T08:00:00",
                "saida_hora": "2026-10-24T11:00:00",
                "concluido_em": "2026-10-24T11:00:00",
                "na_grade": True,
                "placa": "CCC2C22",
            },
            {
                "codigo": "NULA1",
                "rota": "NULA1",
                "status": "Não iniciada",
                "grupo": "nao",
                "percentual": 0,
                "na_grade": True,
            },
        ],
    )
    gravar_rotas(
        con,
        "2026-10-24",
        "noturno",
        [
            {
                "codigo": "60DN",
                "rota": "60DN",
                "status": "Não iniciada",
                "grupo": "nao",
                "percentual": 0,
                "na_grade": True,
            }
        ],
    )
    con.commit()
    anterior = ler_rotas_turno_anterior(con, date(2026, 10, 24), "noturno")
    con.close()
    codigos = {rota["codigo"] for rota in anterior["rotas"]}
    assert codigos == {"ABERTA1", "TARDE1"}
    assert anterior["data"] == "2026-10-24"
    assert anterior["turno"] == "diurno"

    estado = ler_estado_virada(tmp_path, "2026-10-24", "noturno")
    assert estado["turno"] == "noturno"
    assert {rota["codigo"] for rota in estado["rotas_turno_anterior"]} == {"ABERTA1", "TARDE1"}
    assert estado["turno_anterior"]["turno"] == "diurno"
    tarde = next(rota for rota in estado["rotas_turno_anterior"] if rota["codigo"] == "TARDE1")
    assert tarde["concluido_em"].startswith("2026-10-24T15:40")
    assert tarde["status"]


def test_carimbar_origem_nao_sobrescreve_e_filtro_usa_virada():
    base = carimbar_origem(
        {"codigo": "01AD", "inicio": "2026-10-23T20:10:00", "grupo": "parcial", "percentual": 10},
        "2026-10-23",
        "noturno",
    )
    assert base["data_turno"] == "2026-10-23"
    assert base["turno_origem"] == "noturno"
    de_novo = carimbar_origem(
        {**base, "inicio": "2026-10-24T06:10:00", "data_turno": "2026-10-23", "turno_origem": "noturno"},
        "2026-10-24",
        "diurno",
    )
    assert de_novo["data_turno"] == "2026-10-23"
    assert de_novo["turno_origem"] == "noturno"

    virada = datetime(2026, 10, 24, 6, 0, 0)
    filtradas = filtrar_rotas_virada(
        [
            {"codigo": "A", "grupo": "parcial", "inicio": "2026-10-23T22:00:00", "percentual": 12},
            {
                "codigo": "B",
                "grupo": "concluida",
                "inicio": "2026-10-23T21:00:00",
                "concluido_em": "2026-10-24T06:20:00",
                "percentual": 100,
            },
            {
                "codigo": "C",
                "grupo": "concluida",
                "inicio": "2026-10-23T19:30:00",
                "concluido_em": "2026-10-23T23:00:00",
                "percentual": 100,
            },
        ],
        virada,
    )
    assert {rota["codigo"] for rota in filtradas} == {"A", "B"}


def test_migracao_preenche_origem_sem_alterar_status(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    con = conectar()
    _turno(con, "2026-10-21", "diurno")
    con.execute(
        """
        INSERT INTO rota (
            data, turno, codigo, rota, status, grupo, percentual, na_grade, atualizado_em
        ) VALUES ('2026-10-21', 'diurno', '02AD', '02AD', 'Parcial', 'parcial', 33, 1, '2026-10-21T10:00:00')
        """
    )
    con.commit()
    con.close()
    con = conectar()
    row = con.execute(
        "SELECT data_turno, turno_origem, percentual, status FROM rota WHERE codigo = '02AD'"
    ).fetchone()
    view = ler_view(con, "2026-10-21", "diurno")
    con.close()
    assert row["data_turno"] == "2026-10-21"
    assert row["turno_origem"] == "diurno"
    assert float(row["percentual"]) == 33
    assert row["status"] == "Parcial"
    assert view["rotas"][0]["data_turno"] == "2026-10-21"
