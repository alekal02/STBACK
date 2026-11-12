"""A leitura do mapa não espera a gravação da sincronização."""

import time
from datetime import datetime

from inlog_agent.live_db import conectar


def test_leitura_segue_enquanto_a_sincronizacao_grava(tmp_path):
    banco = tmp_path / "sagtrack.db"
    escritor = conectar(banco)
    modo = str(escritor.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    espera = int(escritor.execute("PRAGMA busy_timeout").fetchone()[0])
    assert modo == "wal"
    assert espera == 30000
    escritor.execute(
        "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-11-11", "diurno", "SDM-4D87", "2026-11-11T07:00:00", -16.77, -49.26),
    )
    escritor.commit()
    escritor.execute("BEGIN IMMEDIATE")
    escritor.execute(
        "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-11-11", "diurno", "SDM-4D87", "2026-11-11T07:00:10", -16.77, -49.26),
    )
    leitor = conectar(banco, leitura=True)
    try:
        total = int(
            leitor.execute(
                "SELECT COUNT(*) FROM ponto WHERE data = ? AND turno = ?",
                ("2026-11-11", "diurno"),
            ).fetchone()[0]
        )
    finally:
        leitor.close()
    assert total == 1
    escritor.commit()
    escritor.close()


def test_tracks_le_com_a_gravacao_aberta(tmp_path, monkeypatch):
    banco = tmp_path / "sagtrack.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import trajetoria
    from inlog_agent.live_mapa import trilhas

    monkeypatch.setattr(trajetoria, "trajetos_do_turno", lambda *_args, **_kwargs: {})
    escritor = conectar(banco)
    escritor.execute(
        "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-11-11", "diurno", "SCH-9B28", "2026-11-11T08:00:00", -16.82, -49.25),
    )
    escritor.commit()
    escritor.execute("BEGIN IMMEDIATE")
    escritor.execute(
        "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-11-11", "diurno", "SCH-9B28", "2026-11-11T08:00:10", -16.82, -49.25),
    )
    inicio = time.perf_counter()
    pacote = trilhas("2026-11-11", "diurno")
    gasto = time.perf_counter() - inicio
    escritor.commit()
    escritor.close()
    assert gasto < 5
    assert pacote["pontos_originais"] == 1


def test_sincronizacao_solta_o_banco_antes_de_casar_a_linha(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "sagtrack.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import live_sync, trajetoria
    from inlog_agent.live_mapa import trilhas

    monkeypatch.setattr(trajetoria, "trajetos_do_turno", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(live_sync, "_limites", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_sync, "_recalcular_oficial", lambda *_args, **_kwargs: [])
    vistos: list[str] = []

    def historico(_placa, _vid, _inicio, _fim):
        return [
            {"timestamp": "2026-11-11T08:00:00", "latitude": -16.82, "longitude": -49.25},
            {"timestamp": "2026-11-11T08:00:30", "latitude": -16.821, "longitude": -49.251},
        ]

    def desenho(_con, data, turno, placa, completo=False):
        inicio = time.perf_counter()
        pacote = trilhas(data, turno)
        assert time.perf_counter() - inicio < 5
        assert pacote["pontos_originais"] >= 2
        vistos.append(placa)

    monkeypatch.setattr(live_sync, "_desenho_da_placa", desenho)
    monkeypatch.setattr(
        live_sync,
        "_descobrir_frota",
        lambda: [{"placa": "SCH-9B28", "veiculo": "VW/18.260", "vehicle_id": "1"}],
    )
    live_sync.ciclo_gps(
        tmp_path,
        "2026-11-11",
        "diurno",
        agora=datetime(2026, 11, 11, 10, 0, 0),
        buscar_historico=historico,
    )
    assert vistos == ["SCH-9B28"]


def test_linha_casada_grava_em_transacao_curta(tmp_path, monkeypatch):
    banco = tmp_path / "sagtrack.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    from inlog_agent.live_mapa import atualizar_trilha, casar_trilha

    escritor = conectar(banco)
    for indice, ts in enumerate(("2026-11-11T08:00:00", "2026-11-11T08:00:30")):
        escritor.execute(
            "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-11-11", "diurno", "SCH-9B28", ts, -16.82, -49.25 - indice * 0.001),
        )
    escritor.commit()
    escritor.execute("BEGIN IMMEDIATE")
    durante: list[bool] = []

    def casar(brutos, ruas_de=None):
        durante.append(escritor.in_transaction)
        return casar_trilha(brutos, lambda _fatia: [])

    monkeypatch.setattr("inlog_agent.live_mapa.casar_trilha", casar)
    atualizar_trilha(escritor, "2026-11-11", "diurno", "SCH-9B28", completo=True)
    assert durante == [False]
    assert not escritor.in_transaction
    leitor = conectar(banco, leitura=True)
    try:
        salva = leitor.execute(
            "SELECT placa FROM trilha WHERE data = ? AND turno = ? AND placa = ?",
            ("2026-11-11", "diurno", "SCH-9B28"),
        ).fetchone()
    finally:
        leitor.close()
    escritor.close()
    assert salva is not None


def test_api_nao_casa_no_proprio_processo():
    from inlog_agent.live_sync import _desenho_da_placa

    assert _desenho_da_placa.__doc__
    _desenho_da_placa(None, "2026-11-11", "noturno", "SCH-9B28", completo=True)


def test_so_o_pulso_novo_entra_no_casamento(tmp_path, monkeypatch):
    banco = tmp_path / "sagtrack.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    from inlog_agent.live_db import conectar, gravar_trilha_visual
    from inlog_agent.live_mapa import atualizar_trilha

    escritor = conectar(banco)
    marcas = [
        ("2026-11-11T20:00:00", -49.250),
        ("2026-11-11T20:00:10", -49.251),
        ("2026-11-11T20:00:20", -49.252),
        ("2026-11-11T20:00:30", -49.253),
        ("2026-11-11T20:00:40", -49.254),
        ("2026-11-11T20:00:50", -49.255),
    ]
    for ts, lon in marcas:
        escritor.execute(
            "INSERT INTO ponto (data, turno, placa, ts, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-11-11", "noturno", "SCH-9B28", ts, -16.82, lon),
        )
    gravar_trilha_visual(
        escritor,
        "2026-11-11",
        "noturno",
        "SCH-9B28",
        [[[-16.82, -49.25], [-16.82, -49.254]]],
        [],
        [],
        "2026-11-11T20:00:40",
    )
    escritor.commit()
    vistos: list[list[str]] = []

    def casar(brutos, ruas_de=None, **_kwargs):
        vistos.append([item[0] for item in brutos])
        return [[[-16.82, -49.254], [-16.82, -49.255]]]

    monkeypatch.setattr("inlog_agent.live_mapa.casar_trilha", casar)
    atualizar_trilha(escritor, "2026-11-11", "noturno", "SCH-9B28", completo=False)
    escritor.close()
    assert vistos == [["2026-11-11T20:00:20", "2026-11-11T20:00:30", "2026-11-11T20:00:40", "2026-11-11T20:00:50"]]
