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

    # Origem carimbada de turno mais antigo não entra, mesmo parcial/aberta.
    filtradas_origem = filtrar_rotas_virada(
        [
            {
                "codigo": "OK",
                "grupo": "parcial",
                "inicio": "2026-10-23T22:00:00",
                "percentual": 40,
                "data_turno": "2026-10-23",
                "turno_origem": "noturno",
            },
            {
                "codigo": "VELHA",
                "grupo": "parcial",
                "inicio": "2026-10-22T22:00:00",
                "percentual": 55,
                "data_turno": "2026-10-22",
                "turno_origem": "noturno",
            },
            {
                "codigo": "OUTRO",
                "grupo": "parcial",
                "inicio": "2026-10-23T10:00:00",
                "percentual": 30,
                "data_turno": "2026-10-23",
                "turno_origem": "diurno",
            },
        ],
        virada,
        ant_dia=date(2026, 10, 23),
        ant_turno="noturno",
    )
    assert {rota["codigo"] for rota in filtradas_origem} == {"OK"}

    # Encerrada com concluido_em >= virada permanece; antes da virada só no histórico.
    filtradas_encerrada = filtrar_rotas_virada(
        [
            {
                "codigo": "ENC",
                "grupo": "parcial",
                "inicio": "2026-10-23T22:00:00",
                "percentual": 72,
                "concluido_em": "2026-10-24T07:10:00",
                "data_turno": "2026-10-23",
                "turno_origem": "noturno",
            },
            {
                "codigo": "CONG",
                "grupo": "parcial",
                "inicio": "2026-10-23T20:00:00",
                "percentual": 50,
                "concluido_em": "2026-10-23T23:40:00",
                "data_turno": "2026-10-23",
                "turno_origem": "noturno",
            },
            {
                "codigo": "FEITA",
                "grupo": "concluida",
                "inicio": "2026-10-23T20:00:00",
                "percentual": 100,
                "concluido_em": "2026-10-23T23:40:00",
                "data_turno": "2026-10-23",
                "turno_origem": "noturno",
            },
        ],
        virada,
        ant_dia=date(2026, 10, 23),
        ant_turno="noturno",
    )
    assert {rota["codigo"] for rota in filtradas_encerrada} == {"ENC"}


def test_encerrar_rota_virada_regras_e_placas():
    from datetime import date

    from inlog_agent.live_virada import (
        corte_gps_turno_origem,
        encerrar_rota_virada,
        filtrar_pontos_virada,
        inicio_placa_turno_atual,
        placas_da_rota,
    )

    rota = {
        "codigo": "71AN",
        "grupo": "parcial",
        "percentual": 80.1,
        "placa": "SDJ-6C47",
        "placa_principal": "SDJ-6C47",
        "placas_apoio": ["SDI-4I97"],
        "ultimo_gps": "2026-11-05T05:03:00",
    }
    assert placas_da_rota(rota) == {"SDJ6C47", "SDI4I97"}
    assert "SEMAPOIOIDENTIFICADO" not in placas_da_rota(
        {"placa": "SDJ-6C47", "apoio": "Sem apoio identificado", "placas_apoio": []}
    )
    virada = datetime(2026, 11, 5, 6, 0, 0)
    assert corte_gps_turno_origem(date(2026, 11, 4), "noturno") == datetime(2026, 11, 5, 12, 0, 0)
    assert corte_gps_turno_origem(date(2026, 11, 5), "diurno") == datetime(2026, 11, 5, 23, 0, 0)
    # Corte fixo do noturno (12:00): congela % e concluido_em = último pulso até o corte.
    fechada = encerrar_rota_virada(
        rota,
        virada=virada,
        agora=datetime(2026, 11, 5, 12, 5, 0),
        ultimo_pulso=datetime(2026, 11, 5, 11, 50, 0),
        corte_gps=datetime(2026, 11, 5, 12, 0, 0),
    )
    assert fechada is not None
    assert fechada["percentual"] == 80.1
    assert fechada["concluido_em"] == "2026-11-05T11:50:00"
    assert fechada["status"] == "Encerrada"
    assert fechada["motivo_encerramento"] == "limite_corte"

    # Placa já escalada no turno atual: corte no início da rota nova.
    assert inicio_placa_turno_atual(
        [
            {"codigo": "27BD", "placa": "SDJ-6C47", "inicio": "2026-11-05T06:39:00"},
            {"codigo": "28BD", "placa": "RBM-2G21", "placas_apoio": ["SDJ-6C47"], "inicio": "2026-11-05T07:39:00"},
        ],
        "SDJ-6C47",
    ) == datetime(2026, 11, 5, 6, 39, 0)
    cortados = filtrar_pontos_virada(
        [
            {"timestamp": "2026-11-05T06:30:00"},
            {"timestamp": "2026-11-05T06:39:00"},
            {"timestamp": "2026-11-05T08:00:00"},
            {"timestamp": "2026-11-05T12:01:00"},
        ],
        limite_apos_virada=datetime(2026, 11, 5, 12, 0, 0),
        inicio_rota_nova=datetime(2026, 11, 5, 6, 39, 0),
    )
    assert [p["timestamp"] for p in cortados] == ["2026-11-05T06:30:00"]

    # 30 min sem pulso da placa fecha antes do corte das 12:00.
    cedo = encerrar_rota_virada(
        {
            "codigo": "75AN",
            "grupo": "parcial",
            "percentual": 74.7,
            "ultimo_gps": "2026-11-05T06:10:00",
        },
        virada=virada,
        agora=datetime(2026, 11, 5, 6, 45, 0),
        ultimo_pulso=datetime(2026, 11, 5, 6, 10, 0),
        corte_gps=datetime(2026, 11, 5, 12, 0, 0),
    )
    assert cedo is not None
    assert cedo["motivo_encerramento"] == "sem_pulso"
    assert cedo["concluido_em"] == "2026-11-05T06:10:00"

    # 100% fecha na hora.
    feita = encerrar_rota_virada(
        {"codigo": "X", "grupo": "parcial", "percentual": 100, "ultimo_gps": "2026-11-05T06:20:00"},
        virada=virada,
        agora=datetime(2026, 11, 5, 6, 21, 0),
        ultimo_pulso=datetime(2026, 11, 5, 6, 20, 0),
        corte_gps=datetime(2026, 11, 5, 12, 0, 0),
    )
    assert feita is not None
    assert feita["grupo"] == "concluida"
    assert feita["concluido_em"] == "2026-11-05T06:20:00"


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
