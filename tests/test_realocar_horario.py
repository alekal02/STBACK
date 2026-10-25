"""Realocação dos pulsos gravados no turno errado. Só usa banco temporário."""

from __future__ import annotations

import sqlite3

SABADO = "2026-10-24"
SEXTA = "2026-10-23"
PLACA = "SCN1A23"
VEICULO = "COMPACT 7"


def _banco(monkeypatch, tmp_path):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    from inlog_agent import live_sync

    live_sync._sair()

    def falso(con, output_dir, dia, turno, codigos, placas_executoras=None):
        total = con.execute(
            "SELECT COUNT(*) FROM ponto WHERE data = ? AND turno = ?",
            (dia.isoformat(), turno),
        ).fetchone()[0]
        return [
            {"codigo": codigo, "percentual": int(total) * 10, "rota": codigo}
            for codigo in codigos
        ]

    monkeypatch.setattr(live_sync, "_recalcular_oficial", falso)
    return live_sync


def _turno(con, data: str, turno: str, situacao: str) -> None:
    from inlog_agent.live_db import gravar_rotas, gravar_turno

    gravar_turno(
        con,
        {
            "data": data,
            "turno": turno,
            "aberto": 0 if situacao == "ENCERRADO" else 1,
        },
    )
    con.execute(
        """
        UPDATE turno
        SET situacao = ?, fechado_em = ?, resumo_json = ''
        WHERE data = ? AND turno = ?
        """,
        (
            situacao,
            "2026-10-24T04:59:59" if situacao == "ENCERRADO" else "",
            data,
            turno,
        ),
    )
    gravar_rotas(
        con,
        data,
        turno,
        [{"codigo": "16AD", "percentual": 0, "na_grade": True, "grupo": "nao", "rota": "16AD"}],
    )


def _pulso(quando: str) -> dict[str, float | str]:
    return {"timestamp": quando, "latitude": -16.82, "longitude": -49.24}


def _semear(monkeypatch, tmp_path):
    live_sync = _banco(monkeypatch, tmp_path)
    from inlog_agent.live_db import conectar, inserir_pontos, marcar_placa

    con = conectar()
    _turno(con, SEXTA, "noturno", "ENCERRADO")
    _turno(con, SABADO, "diurno", "ABERTO")
    _turno(con, SABADO, "noturno", "ABERTO")
    inserir_pontos(
        con,
        SEXTA,
        "noturno",
        PLACA,
        [
            _pulso("2026-10-24T04:30:00"),
            _pulso("2026-10-24T05:10:00"),
            _pulso("2026-10-24T05:50:00"),
        ],
    )
    inserir_pontos(
        con,
        SABADO,
        "diurno",
        PLACA,
        [
            _pulso("2026-10-24T07:00:00"),
            _pulso("2026-10-24T14:59:00"),
            _pulso("2026-10-24T15:00:00"),
            _pulso("2026-10-24T16:30:00"),
            _pulso("2026-10-24T18:59:00"),
        ],
    )
    marcar_placa(con, SEXTA, "noturno", PLACA, veiculo=VEICULO)
    marcar_placa(con, SABADO, "diurno", PLACA, veiculo=VEICULO)
    con.commit()
    con.close()
    return live_sync


def _ler():
    import os

    con = sqlite3.connect(os.environ["SAGTRACK_LIVE_DB"])
    con.row_factory = sqlite3.Row
    return con


def _horas(con, data: str, turno: str) -> list[str]:
    linhas = con.execute(
        "SELECT ts FROM ponto WHERE data = ? AND turno = ? ORDER BY ts",
        (data, turno),
    ).fetchall()
    return [str(linha["ts"])[11:16] for linha in linhas]


def _foto(con) -> dict:
    pontos = [
        tuple(linha)
        for linha in con.execute(
            "SELECT data, turno, placa, ts, lat, lon FROM ponto ORDER BY data, turno, ts"
        )
    ]
    rotas = [
        tuple(linha)
        for linha in con.execute(
            "SELECT data, turno, codigo, percentual FROM rota ORDER BY data, turno, codigo"
        )
    ]
    turnos = [
        tuple(linha)
        for linha in con.execute(
            "SELECT data, turno, situacao, resumo_json FROM turno ORDER BY data, turno"
        )
    ]
    eventos = con.execute("SELECT COUNT(*) FROM turno_evento").fetchone()[0]
    return {"pontos": pontos, "rotas": rotas, "turnos": turnos, "eventos": eventos}


def _qtd(movidos, de: dict, para: dict) -> int:
    for item in movidos:
        if item["de"] == de and item["para"] == para:
            return item["quantidade"]
    return 0


def _rota(resultado, data: str, turno: str, codigo: str = "16AD") -> dict:
    for item in resultado["turnos"]:
        if item["data"] == data and item["turno"] == turno:
            return item["rotas"][codigo]
    raise AssertionError(f"turno ausente: {data} {turno}")


def test_previa_nao_grava_e_aplicar_move_os_pulsos(monkeypatch, tmp_path):
    live_sync = _semear(monkeypatch, tmp_path)
    realocar = live_sync.realocar_pontos_pelo_horario

    assert live_sync._entrar()
    try:
        ocupado = realocar(tmp_path, SABADO, aplicar=True, usuario="auditor")
        assert ocupado["ok"] is False
        assert ocupado["erro"] == "Atualização ao vivo em andamento"
        assert live_sync._OCUPADO is True
    finally:
        live_sync._sair()

    con = _ler()
    foto = _foto(con)
    con.close()

    previa = realocar(tmp_path, SABADO, aplicar=False, usuario="auditor")
    assert previa["ok"] is True
    assert previa["aplicado"] is False
    assert previa["previa"] is True
    assert previa["por_placa"] == {PLACA: 5}
    assert previa["fora_de_turno"] == []
    assert _qtd(previa["movidos"], {"data": SEXTA, "turno": "noturno"}, {"data": SABADO, "turno": "diurno"}) == 2
    assert _qtd(previa["movidos"], {"data": SABADO, "turno": "diurno"}, {"data": SABADO, "turno": "noturno"}) == 3
    assert _rota(previa, SEXTA, "noturno") == {"antes": 0, "depois": 10}
    assert _rota(previa, SABADO, "diurno") == {"antes": 0, "depois": 40}
    assert _rota(previa, SABADO, "noturno") == {"antes": 0, "depois": 30}

    con = _ler()
    assert _foto(con) == foto
    con.close()

    aplicado = realocar(tmp_path, SABADO, aplicar=True, usuario="auditor")
    assert aplicado["aplicado"] is True
    assert aplicado["por_placa"] == {PLACA: 5}
    assert _rota(aplicado, SEXTA, "noturno")["depois"] == 10
    assert _rota(aplicado, SABADO, "diurno")["depois"] == 40
    assert _rota(aplicado, SABADO, "noturno")["depois"] == 30

    con = _ler()
    assert _horas(con, SEXTA, "noturno") == ["04:30"]
    assert _horas(con, SABADO, "diurno") == ["05:10", "05:50", "07:00", "14:59"]
    assert _horas(con, SABADO, "noturno") == ["15:00", "16:30", "18:59"]
    noite = con.execute(
        "SELECT veiculo FROM placa WHERE data = ? AND turno = 'noturno' AND placa = ?",
        (SABADO, PLACA),
    ).fetchone()
    assert noite["veiculo"] == VEICULO
    diurno = con.execute(
        "SELECT veiculo FROM placa WHERE data = ? AND turno = 'diurno' AND placa = ?",
        (SABADO, PLACA),
    ).fetchone()
    assert diurno["veiculo"] == VEICULO
    eventos = con.execute(
        "SELECT data, turno, acao, origem, usuario FROM turno_evento ORDER BY id"
    ).fetchall()
    assert len(eventos) == 3
    assert {(linha["data"], linha["turno"], linha["acao"], linha["origem"], linha["usuario"]) for linha in eventos} == {
        (SEXTA, "noturno", "REALOCACAO_HORARIO", "manual", "auditor"),
        (SABADO, "diurno", "REALOCACAO_HORARIO", "manual", "auditor"),
        (SABADO, "noturno", "REALOCACAO_HORARIO", "manual", "auditor"),
    }
    sexta = con.execute(
        "SELECT situacao, resumo_json FROM turno WHERE data = ? AND turno = 'noturno'",
        (SEXTA,),
    ).fetchone()
    assert sexta["situacao"] == "ENCERRADO"
    assert "percentual_geral" in sexta["resumo_json"]
    for turno in ("diurno", "noturno"):
        linha = con.execute(
            "SELECT situacao, resumo_json FROM turno WHERE data = ? AND turno = ?",
            (SABADO, turno),
        ).fetchone()
        assert linha["situacao"] == "ABERTO"
        assert linha["resumo_json"] == ""
    percentuais = {
        (linha["data"], linha["turno"]): linha["percentual"]
        for linha in con.execute("SELECT data, turno, percentual FROM rota")
    }
    assert percentuais[(SEXTA, "noturno")] == 10
    assert percentuais[(SABADO, "diurno")] == 40
    assert percentuais[(SABADO, "noturno")] == 30
    con.close()

    de_novo = realocar(tmp_path, SABADO, aplicar=True, usuario="auditor")
    assert de_novo["movidos"] == []
    assert de_novo["por_placa"] == {}
    assert de_novo["fora_de_turno"] == []
    assert de_novo["turnos"] == []
    con = _ler()
    assert _horas(con, SEXTA, "noturno") == ["04:30"]
    assert _horas(con, SABADO, "diurno") == ["05:10", "05:50", "07:00", "14:59"]
    assert _horas(con, SABADO, "noturno") == ["15:00", "16:30", "18:59"]
    assert con.execute("SELECT COUNT(*) FROM turno_evento").fetchone()[0] == 3
    con.close()


def test_pulso_fora_de_turno_permanece(monkeypatch, tmp_path):
    live_sync = _banco(monkeypatch, tmp_path)
    from inlog_agent.live_db import conectar, inserir_pontos, marcar_placa

    con = conectar()
    _turno(con, SABADO, "diurno", "ABERTO")
    _turno(con, SABADO, "noturno", "ABERTO")
    inserir_pontos(
        con,
        SABADO,
        "diurno",
        PLACA,
        [_pulso("2026-10-24T15:00:00"), _pulso("2026-10-25T03:00:00")],
    )
    marcar_placa(con, SABADO, "diurno", PLACA, veiculo=VEICULO)
    con.commit()
    con.close()

    resultado = live_sync.realocar_pontos_pelo_horario(tmp_path, SABADO, aplicar=True)
    assert [item["ts"] for item in resultado["fora_de_turno"]] == ["2026-10-25T03:00:00"]
    con = _ler()
    assert _horas(con, SABADO, "diurno") == ["03:00"]
    assert _horas(con, SABADO, "noturno") == ["15:00"]
    con.close()


def test_cli_expoe_realocar_horario_sem_aplicar():
    from inlog_agent.cli import build_parser

    previa = build_parser().parse_args(["realocar-horario", "--data", SABADO])
    assert previa.comando == "realocar-horario"
    assert previa.data == SABADO
    assert previa.aplicar is False
    gravar = build_parser().parse_args(["realocar-horario", "--data", SABADO, "--aplicar"])
    assert gravar.aplicar is True


def test_confirmacao_para_gravar_e_realocar_data():
    from inlog_agent.server import _confirmacao_de_realocacao

    assert _confirmacao_de_realocacao(SABADO, "REALOCAR 2026-10-24") is True
    assert _confirmacao_de_realocacao(SABADO, "realocar 2026-10-24") is False
    assert _confirmacao_de_realocacao(SABADO, "REALOCAR 2026-10-23") is False
