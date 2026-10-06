"""Descoberta da frota ao vivo. Nao fala com a Fulltrack nem grava producao."""

import json
import os
import tempfile
from datetime import date
from pathlib import Path

os.environ["SAGTRACK_LIVE_DB"] = str(Path(tempfile.mkdtemp()) / "live.db")
os.environ["SAGTRACK_LIVE_SYNC"] = "0"

from inlog_agent.fulltrack_api import _escolher_veiculos, _fmt_placa, _norm_placa
from inlog_agent.live_db import conectar
from inlog_agent.live_sync import _importar_pontos_json

DIA = date(2026, 10, 5)
DENTRO = {"ras_eve_latitude": -16.82, "ras_eve_longitude": -49.25, "ras_ras_id": "eq-1"}


def _vw(placa, modelo, equipamento="eq-1"):
    return {
        "ras_vei_placa": placa,
        "ras_vei_modelo": modelo,
        "ras_vei_veiculo": modelo,
        "ras_vei_tipo": 3,
        "ras_vei_id": "id-" + placa,
        "ras_vei_equipamento": equipamento,
    }


def _placas(escolhidos):
    return {_norm_placa(str(item.get("ras_vei_placa") or "")) for item in escolhidos}


def test_rbm_8i85_entra_com_a_mesma_regra_das_vw():
    veiculos = [
        _vw("RBM8I85", "VW18.260 CRM 4X2 4P"),
        _vw("RBK7H47", "VW/17.260 CRM 4X2 4P"),
        _vw("RBM2G21", "VW/17.260 CRM 4X2 4P"),
        _vw("RBL0C01", "VW/17.260 CRM 4X2 4P"),
        _vw("SDI4I97", "VW/18.260 CRM 4X2 4P"),
        {
            "ras_vei_placa": "RBX1A23",
            "ras_vei_modelo": "CARRO",
            "ras_vei_veiculo": "LEVE",
            "ras_vei_tipo": 1,
            "ras_vei_id": "id-carro",
            "ras_vei_equipamento": "eq-1",
        },
    ]
    eventos = [DENTRO]
    achados = _placas(_escolher_veiculos(veiculos, eventos, (), None))
    assert "RBM8I85" in achados
    assert {"RBK7H47", "RBM2G21", "RBL0C01", "SDI4I97"} <= achados
    assert "RBX1A23" not in achados
    assert _fmt_placa("RBM8I85") == "RBM-8I85"


def test_8i85_nao_depende_de_lista_manual():
    so_ela = [_vw("RBM8I85", "VW18.260")]
    sem_lista = _placas(_escolher_veiculos(so_ela, [DENTRO], (), None))
    lista_argos = _placas(_escolher_veiculos(so_ela, [DENTRO], (), {"RBK7H47", "RBM2G21", "RBL0C01"}))
    assert sem_lista == {"RBM8I85"}
    assert lista_argos == {"RBM8I85"}


def test_ponto_da_8i85_fica_so_no_turno_do_horario():
    pasta = Path(tempfile.mkdtemp())
    destino = pasta / "segtrack" / DIA.isoformat()
    destino.mkdir(parents=True)
    (destino / "frota.json").write_text(
        json.dumps(
            [
                {
                    "placa": "RBM-8I85",
                    "veiculo": "VW18.260",
                    "codigo_veiculo": "1553679",
                    "pontos": [
                        {"timestamp": "2026-10-05T08:07:00", "latitude": -16.74, "longitude": -49.22},
                        {"timestamp": "2026-10-05T20:40:00", "latitude": -16.80, "longitude": -49.24},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    con = conectar()
    try:
        _importar_pontos_json(con, pasta, DIA, "diurno")
        _importar_pontos_json(con, pasta, DIA, "noturno")
        con.commit()
        dia = con.execute(
            "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
            ("RBM-8I85", "diurno"),
        ).fetchall()
        noite = con.execute(
            "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
            ("RBM-8I85", "noturno"),
        ).fetchall()
    finally:
        con.close()
    assert [linha["ts"] for linha in dia] == ["2026-10-05T08:07:00"]
    assert [linha["ts"] for linha in noite] == ["2026-10-05T20:40:00"]


FROTA_24 = (
    "SCW-4F36",
    "SDE-6B76",
    "SDE-5I26",
    "RBL-0C01",
    "SCH-9B28",
    "RBK-7H47",
    "SDI-1H97",
    "SDF-5G66",
    "SDG-1J76",
    "RBM-2G21",
    "SDG-6D46",
    "SDI-2B97",
    "SDI-4I97",
    "SDI-4H17",
    "SDN-9G76",
    "SDK-8H37",
    "SDJ-6C47",
    "SDO-5B17",
    "SDJ-5A17",
    "SDM-4D87",
    "SDN-8F66",
    "SDN-6J87",
    "SDN-8J06",
    "SDO-2J06",
)
CURSOR_24 = "2026-10-05T21:00:00"
AGORA = __import__("datetime").datetime(2026, 10, 5, 22, 0, 0)
INICIO_NOITE = __import__("datetime").datetime(2026, 10, 5, 19, 0, 0)
NOITE = {"timestamp": "2026-10-05T20:40:00", "latitude": -16.80, "longitude": -49.24}
DIA_FORA = {"timestamp": "2026-10-05T08:07:00", "latitude": -16.74, "longitude": -49.22}
DEPOIS = {"timestamp": "2026-10-05T21:10:00", "latitude": -16.80, "longitude": -49.24}


def test_rbm_8i85_entra_no_noturno_ja_aberto():
    from datetime import datetime, timedelta

    from inlog_agent.cobertura import _xy
    from inlog_agent.live_db import gravar_turno, marcar_placa
    from inlog_agent.live_sync import _limpar_cache_frota, ciclo_gps

    _limpar_cache_frota()
    con = conectar()
    try:
        con.execute("DELETE FROM ponto")
        con.execute("DELETE FROM placa")
        con.execute("DELETE FROM rota")
        con.execute("DELETE FROM turno")
        gravar_turno(con, {"data": DIA.isoformat(), "turno": "noturno"})
        con.execute(
            "UPDATE turno SET situacao = 'ABERTO', aberto = 1 WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        )
        for indice, placa in enumerate(FROTA_24, start=1):
            marcar_placa(
                con,
                DIA.isoformat(),
                "noturno",
                placa,
                veiculo=placa,
                vehicle_id="id-%s" % indice,
                last_gps_timestamp=CURSOR_24,
            )
        con.commit()
    finally:
        con.close()

    xy = _xy(NOITE)
    assert xy is not None
    limites = [
        {
            "codigo": "75AN",
            "minx": xy[0] - 50,
            "miny": xy[1] - 50,
            "maxx": xy[0] + 50,
            "maxy": xy[1] + 50,
        },
        {"codigo": "60DN", "minx": 0, "miny": 0, "maxx": 1, "maxy": 1},
    ]
    chamadas = []
    descobertas = []
    recalculos = []

    def descobrir():
        descobertas.append(1)
        itens = [
            {"placa": placa, "veiculo": placa, "vehicle_id": "id-%s" % indice}
            for indice, placa in enumerate(FROTA_24, start=1)
        ]
        itens.append({"placa": "RBM-8I85", "veiculo": "VW18.260", "vehicle_id": "1553679"})
        return itens

    def historico(placa, vehicle_id, inicio, fim):
        chamadas.append((placa, vehicle_id, inicio, fim))
        if placa != "RBM-8I85":
            return []
        if inicio == INICIO_NOITE:
            return [DIA_FORA, NOITE]
        return [NOITE, DEPOIS]

    def recalcular(_con, _pasta, _dia, _turno, codigos):
        recalculos.append(list(codigos))
        return [{"codigo": codigo} for codigo in codigos]

    pasta = Path(tempfile.mkdtemp())
    primeiro = ciclo_gps(
        pasta,
        DIA.isoformat(),
        "noturno",
        agora=AGORA,
        limites=limites,
        recalcular=recalcular,
        descobrir=descobrir,
        buscar_historico=historico,
    )
    assert primeiro.get("falha", "") == ""
    assert len(descobertas) == 1

    con = conectar()
    try:
        placas = {
            linha["placa"]: linha["last_gps_timestamp"]
            for linha in con.execute(
                "SELECT placa, last_gps_timestamp FROM placa WHERE data = ? AND turno = ?",
                (DIA.isoformat(), "noturno"),
            )
        }
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
                ("RBM-8I85", "noturno"),
            )
        ]
        turno = con.execute(
            "SELECT reconstrucao_pontos, reconstrucao_tentativas FROM turno WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        ).fetchone()
    finally:
        con.close()

    assert set(placas) == set(FROTA_24) | {"RBM-8I85"}
    for placa in FROTA_24:
        assert placas[placa] == CURSOR_24
    assert placas["RBM-8I85"] == "2026-10-05T20:40:00"
    assert pontos == ["2026-10-05T20:40:00"]
    assert int(turno["reconstrucao_pontos"] or 0) == 0
    assert int(turno["reconstrucao_tentativas"] or 0) == 0
    assert recalculos == [["75AN"]]

    da_nova = [item for item in chamadas if item[0] == "RBM-8I85"]
    assert len(da_nova) == 1
    assert da_nova[0][1] == "1553679"
    assert da_nova[0][2] == INICIO_NOITE
    assert da_nova[0][3] == AGORA
    assert all(item[2] != INICIO_NOITE for item in chamadas if item[0] != "RBM-8I85")

    segundo = ciclo_gps(
        pasta,
        DIA.isoformat(),
        "noturno",
        agora=AGORA,
        limites=limites,
        recalcular=recalcular,
        descobrir=descobrir,
        buscar_historico=historico,
    )
    assert segundo.get("falha", "") == ""
    assert len(descobertas) == 1

    con = conectar()
    try:
        placas = {
            linha["placa"]: linha["last_gps_timestamp"]
            for linha in con.execute(
                "SELECT placa, last_gps_timestamp FROM placa WHERE data = ? AND turno = ?",
                (DIA.isoformat(), "noturno"),
            )
        }
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
                ("RBM-8I85", "noturno"),
            )
        ]
        turno = con.execute(
            "SELECT reconstrucao_pontos FROM turno WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        ).fetchone()
    finally:
        con.close()

    assert placas["RBM-8I85"] == "2026-10-05T21:10:00"
    for placa in FROTA_24:
        assert placas[placa] == CURSOR_24
    assert pontos == ["2026-10-05T20:40:00", "2026-10-05T21:10:00"]
    assert int(turno["reconstrucao_pontos"] or 0) == 0
    da_nova = [item for item in chamadas if item[0] == "RBM-8I85"]
    assert len(da_nova) == 2
    assert da_nova[1][2] == datetime(2026, 10, 5, 20, 38, 30)
    assert da_nova[1][2] != INICIO_NOITE
    assert da_nova[1][2] == datetime(2026, 10, 5, 20, 40, 0) - timedelta(seconds=90)
    assert recalculos[1] == ["75AN"]


def test_placa_cadastrada_sem_cursor_recebe_bootstrap():
    from datetime import datetime, timedelta

    from inlog_agent.cobertura import _xy
    from inlog_agent.live_db import gravar_turno, marcar_placa
    from inlog_agent.live_sync import _limpar_cache_frota, ciclo_gps

    _limpar_cache_frota()
    con = conectar()
    try:
        con.execute("DELETE FROM ponto")
        con.execute("DELETE FROM placa")
        con.execute("DELETE FROM rota")
        con.execute("DELETE FROM turno")
        gravar_turno(con, {"data": DIA.isoformat(), "turno": "noturno"})
        con.execute(
            "UPDATE turno SET situacao = 'ABERTO', aberto = 1 WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        )
        for indice, placa in enumerate(FROTA_24, start=1):
            marcar_placa(
                con,
                DIA.isoformat(),
                "noturno",
                placa,
                veiculo=placa,
                vehicle_id="id-%s" % indice,
                last_gps_timestamp=CURSOR_24,
            )
        marcar_placa(
            con,
            DIA.isoformat(),
            "noturno",
            "RBM-8I85",
            veiculo="VW18.260",
            vehicle_id="1553679",
        )
        con.commit()
    finally:
        con.close()

    xy = _xy(NOITE)
    assert xy is not None
    limites = [
        {
            "codigo": "75AN",
            "minx": xy[0] - 50,
            "miny": xy[1] - 50,
            "maxx": xy[0] + 50,
            "maxy": xy[1] + 50,
        },
        {"codigo": "60DN", "minx": 0, "miny": 0, "maxx": 1, "maxy": 1},
    ]
    chamadas = []
    descobertas = []
    recalculos = []

    def descobrir():
        descobertas.append(1)
        itens = [
            {"placa": placa, "veiculo": placa, "vehicle_id": "id-%s" % indice}
            for indice, placa in enumerate(FROTA_24, start=1)
        ]
        itens.append({"placa": "RBM-8I85", "veiculo": "VW18.260", "vehicle_id": "1553679"})
        return itens

    def historico(placa, vehicle_id, inicio, fim):
        chamadas.append((placa, vehicle_id, inicio, fim))
        if placa != "RBM-8I85":
            return []
        if inicio == INICIO_NOITE:
            return [DIA_FORA, NOITE]
        return [NOITE, DEPOIS]

    def recalcular(_con, _pasta, _dia, _turno, codigos):
        recalculos.append(list(codigos))
        return [{"codigo": codigo} for codigo in codigos]

    pasta = Path(tempfile.mkdtemp())
    primeiro = ciclo_gps(
        pasta,
        DIA.isoformat(),
        "noturno",
        agora=AGORA,
        limites=limites,
        recalcular=recalcular,
        descobrir=descobrir,
        buscar_historico=historico,
    )
    assert primeiro.get("falha", "") == ""

    desde_inicio = [item for item in chamadas if item[2] == INICIO_NOITE]
    assert [item[0] for item in desde_inicio] == ["RBM-8I85"]
    assert desde_inicio[0][1] == "1553679"
    assert desde_inicio[0][3] == AGORA

    con = conectar()
    try:
        placas = {
            linha["placa"]: linha["last_gps_timestamp"]
            for linha in con.execute(
                "SELECT placa, last_gps_timestamp FROM placa WHERE data = ? AND turno = ?",
                (DIA.isoformat(), "noturno"),
            )
        }
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
                ("RBM-8I85", "noturno"),
            )
        ]
        turno = con.execute(
            "SELECT reconstrucao_pontos, reconstrucao_tentativas FROM turno WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        ).fetchone()
    finally:
        con.close()

    assert set(placas) == set(FROTA_24) | {"RBM-8I85"}
    for placa in FROTA_24:
        assert placas[placa] == CURSOR_24
    assert placas["RBM-8I85"] == "2026-10-05T20:40:00"
    assert pontos == ["2026-10-05T20:40:00"]
    assert int(turno["reconstrucao_pontos"] or 0) == 0
    assert int(turno["reconstrucao_tentativas"] or 0) == 0
    assert recalculos == [["75AN"]]

    segundo = ciclo_gps(
        pasta,
        DIA.isoformat(),
        "noturno",
        agora=AGORA,
        limites=limites,
        recalcular=recalcular,
        descobrir=descobrir,
        buscar_historico=historico,
    )
    assert segundo.get("falha", "") == ""
    assert len(descobertas) == 1

    con = conectar()
    try:
        placas = {
            linha["placa"]: linha["last_gps_timestamp"]
            for linha in con.execute(
                "SELECT placa, last_gps_timestamp FROM placa WHERE data = ? AND turno = ?",
                (DIA.isoformat(), "noturno"),
            )
        }
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ? ORDER BY ts",
                ("RBM-8I85", "noturno"),
            )
        ]
        turno = con.execute(
            "SELECT reconstrucao_pontos, reconstrucao_tentativas FROM turno WHERE data = ? AND turno = ?",
            (DIA.isoformat(), "noturno"),
        ).fetchone()
    finally:
        con.close()

    da_nova = [item for item in chamadas if item[0] == "RBM-8I85"]
    assert len(da_nova) == 2
    assert da_nova[1][2] == datetime(2026, 10, 5, 20, 40, 0) - timedelta(seconds=90)
    assert da_nova[1][2] != INICIO_NOITE
    for placa in FROTA_24:
        assert placas[placa] == CURSOR_24
    assert placas["RBM-8I85"] == "2026-10-05T21:10:00"
    assert pontos == ["2026-10-05T20:40:00", "2026-10-05T21:10:00"]
    assert int(turno["reconstrucao_pontos"] or 0) == 0
    assert int(turno["reconstrucao_tentativas"] or 0) == 0
    assert recalculos[1] == ["75AN"]
