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


SEM_CURSOR = ("SCW-4F36", "SDE-6B76", "SDO-5B17", "SDN-9G76")
SEM_DADOS = RuntimeError('Fulltrack 400 /events/interval: {"detail":"No data were found!"}')
GPS_NOVO = {"timestamp": "2026-10-05T22:03:00", "latitude": -16.80, "longitude": -49.24}


def _turno_com_ancora():
    from inlog_agent.live_db import gravar_turno, marcar_placa
    from inlog_agent.live_sync import _limpar_cache_frota

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
        marcar_placa(
            con,
            DIA.isoformat(),
            "noturno",
            "SCH-9B28",
            veiculo="SCH-9B28",
            vehicle_id="id-sch",
            last_gps_timestamp=CURSOR_24,
        )
        con.commit()
    finally:
        con.close()


def _ler_consulta():
    con = conectar()
    try:
        return {
            linha["placa"]: {
                "cursor": linha["last_gps_timestamp"],
                "ate": linha["consulta_ate"],
                "falha_em": linha["consulta_falha_em"],
                "falhas": int(linha["consulta_falhas"] or 0),
                "telemetria": linha["telemetria"],
            }
            for linha in con.execute(
                """
                SELECT placa, last_gps_timestamp, consulta_ate, consulta_falha_em,
                       consulta_falhas, telemetria
                FROM placa WHERE data = ? AND turno = ?
                """,
                (DIA.isoformat(), "noturno"),
            )
        }
    finally:
        con.close()


def test_sem_gps_nao_repete_historico_desde_as_19():
    from datetime import datetime, timedelta

    from inlog_agent.live_db import marcar_placa
    from inlog_agent.live_sync import ciclo_gps

    _turno_com_ancora()
    con = conectar()
    try:
        for placa in SEM_CURSOR:
            marcar_placa(
                con,
                DIA.isoformat(),
                "noturno",
                placa,
                veiculo=placa,
                vehicle_id="id-" + placa,
            )
        con.commit()
    finally:
        con.close()

    chamadas = []
    gps = {"ligado": False}

    def descobrir():
        return []

    def historico(placa, _vehicle_id, inicio, fim):
        chamadas.append((placa, inicio, fim))
        if placa == "SCH-9B28":
            return []
        if placa == "SCW-4F36" and gps["ligado"]:
            return [GPS_NOVO]
        raise SEM_DADOS

    def rodar(agora):
        return ciclo_gps(
            Path(tempfile.mkdtemp()),
            DIA.isoformat(),
            "noturno",
            agora=agora,
            limites=[],
            descobrir=descobrir,
            buscar_historico=historico,
            recalcular=lambda *_args: [],
        )

    primeiro = datetime(2026, 10, 5, 22, 0, 0)
    assert rodar(primeiro).get("falha", "") == ""
    das_quatro = [item for item in chamadas if item[0] in SEM_CURSOR]
    assert sorted(item[0] for item in das_quatro) == sorted(SEM_CURSOR)
    assert {item[1] for item in das_quatro} == {INICIO_NOITE}
    estado = _ler_consulta()
    for placa in SEM_CURSOR:
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["ate"] == "2026-10-05T22:00:00"
        assert estado[placa]["telemetria"] == "sem_dados"
        assert estado[placa]["falhas"] == 0
    assert estado["SCH-9B28"]["cursor"] == CURSOR_24

    chamadas.clear()
    segundo = datetime(2026, 10, 5, 22, 2, 0)
    assert rodar(segundo).get("falha", "") == ""
    das_quatro = [item for item in chamadas if item[0] in SEM_CURSOR]
    assert len(das_quatro) == 4
    assert {item[1] for item in das_quatro} == {datetime(2026, 10, 5, 21, 58, 30)}
    assert all(item[1] != INICIO_NOITE for item in das_quatro)
    estado = _ler_consulta()
    for placa in SEM_CURSOR:
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["ate"] == "2026-10-05T22:02:00"

    chamadas.clear()
    gps["ligado"] = True
    terceiro = datetime(2026, 10, 5, 22, 4, 0)
    assert rodar(terceiro).get("falha", "") == ""
    da_scw = [item for item in chamadas if item[0] == "SCW-4F36"]
    assert len(da_scw) == 1
    assert da_scw[0][1] == datetime(2026, 10, 5, 22, 0, 30)
    assert da_scw[0][1] == datetime(2026, 10, 5, 22, 2, 0) - timedelta(seconds=90)
    con = conectar()
    try:
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ?",
                ("SCW-4F36", "noturno"),
            )
        ]
    finally:
        con.close()
    assert pontos == ["2026-10-05T22:03:00"]
    estado = _ler_consulta()
    assert estado["SCW-4F36"]["cursor"] == "2026-10-05T22:03:00"
    for placa in SEM_CURSOR[1:]:
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["ate"] == "2026-10-05T22:04:00"

    chamadas.clear()
    assert rodar(datetime(2026, 10, 5, 22, 6, 0)).get("falha", "") == ""
    da_scw = [item for item in chamadas if item[0] == "SCW-4F36"]
    assert len(da_scw) == 1
    assert da_scw[0][1] == datetime(2026, 10, 5, 22, 1, 30)
    assert da_scw[0][1] != INICIO_NOITE
    outras = [item for item in chamadas if item[0] in SEM_CURSOR[1:]]
    assert len(outras) == 3
    assert all(item[1] != INICIO_NOITE for item in outras)
    con = conectar()
    try:
        pontos = [
            linha["ts"]
            for linha in con.execute(
                "SELECT ts FROM ponto WHERE placa = ? AND turno = ?",
                ("SCW-4F36", "noturno"),
            )
        ]
        rotas = [
            linha["status"]
            for linha in con.execute("SELECT status FROM rota WHERE data = ? AND turno = ?", (DIA.isoformat(), "noturno"))
        ]
    finally:
        con.close()
    assert pontos == ["2026-10-05T22:03:00"]
    assert estado["SCH-9B28"]["cursor"] == _ler_consulta()["SCH-9B28"]["cursor"]
    assert all(status != "sem_dados" for status in rotas)


def test_timeout_e_500_nao_avancam_a_marca():
    from datetime import datetime

    from inlog_agent.live_db import marcar_placa
    from inlog_agent.live_sync import ciclo_gps

    _turno_com_ancora()
    con = conectar()
    try:
        marcar_placa(con, DIA.isoformat(), "noturno", "SCW-4F36", veiculo="SCW-4F36", vehicle_id="id-scw")
        marcar_placa(con, DIA.isoformat(), "noturno", "SDE-6B76", veiculo="SDE-6B76", vehicle_id="id-sde")
        con.commit()
    finally:
        con.close()

    chamadas = []

    def historico(placa, _vehicle_id, inicio, fim):
        chamadas.append((placa, inicio, fim))
        if placa == "SCW-4F36":
            raise TimeoutError("timed out")
        if placa == "SDE-6B76":
            raise RuntimeError("Fulltrack 500 upstream")
        return []

    def rodar(agora):
        return ciclo_gps(
            Path(tempfile.mkdtemp()),
            DIA.isoformat(),
            "noturno",
            agora=agora,
            limites=[],
            descobrir=lambda: [],
            buscar_historico=historico,
            recalcular=lambda *_args: [],
        )

    assert rodar(datetime(2026, 10, 5, 22, 0, 0)).get("falha", "") == ""
    primeiras = [item for item in chamadas if item[0] in ("SCW-4F36", "SDE-6B76")]
    assert {item[1] for item in primeiras} == {INICIO_NOITE}
    estado = _ler_consulta()
    for placa in ("SCW-4F36", "SDE-6B76"):
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["ate"] == ""
        assert estado[placa]["falha_em"] == "2026-10-05T22:00:00"
        assert estado[placa]["falhas"] == 1
        assert estado[placa]["telemetria"] != "sem_dados"

    chamadas.clear()
    assert rodar(datetime(2026, 10, 5, 22, 2, 0)).get("falha", "") == ""
    assert [item for item in chamadas if item[0] in ("SCW-4F36", "SDE-6B76")] == []
    estado = _ler_consulta()
    for placa in ("SCW-4F36", "SDE-6B76"):
        assert estado[placa]["ate"] == ""
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["falhas"] == 1

    chamadas.clear()
    assert rodar(datetime(2026, 10, 5, 22, 10, 0)).get("falha", "") == ""
    de_novo = [item for item in chamadas if item[0] in ("SCW-4F36", "SDE-6B76")]
    assert len(de_novo) == 2
    assert {item[1] for item in de_novo} == {INICIO_NOITE}
    estado = _ler_consulta()
    for placa in ("SCW-4F36", "SDE-6B76"):
        assert estado[placa]["cursor"] == ""
        assert estado[placa]["ate"] == ""
        assert estado[placa]["telemetria"] != "sem_dados"
