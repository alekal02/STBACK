"""O percentual das vias só entra com compactador ou basculante do cadastro."""

import pytest

from inlog_agent.dificil_acesso import eh_coleta


@pytest.fixture(autouse=True)
def banco_limpo(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    from inlog_agent.cadastro_frota import invalidar

    invalidar()
    yield
    invalidar()


def test_pm_trator_e_daily_ficam_de_fora():
    assert eh_coleta("PM-001", "PM-001") is False
    assert eh_coleta("PM-34", "PM-34") is False
    assert eh_coleta("PM-003", "PM-003") is False
    assert eh_coleta("TAR-6461", "Trator EX854 - 32316461") is False
    assert eh_coleta("TAR-2294", "Trator EX854 - 32312294") is False
    assert eh_coleta("CGZ-0015", "TRATOR CORTADOR DE GRAMA GIRO ZERO HUSQVARNA Z560X") is False
    assert eh_coleta("SCZ-4A29", "IVECO/DAILY 65CD") is False


def test_edicao_do_admin_vence_o_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    from inlog_agent.cadastro_frota import definir, invalidar, tipo_de

    invalidar()
    assert eh_coleta("SCN-4E19", "IVECO/TECTOR 15-210") is True
    definir("SCN-4E19", "apoio")
    assert tipo_de("SCN-4E19") == "apoio"
    assert eh_coleta("SCN-4E19", "IVECO/TECTOR 15-210") is False
    invalidar()


def test_compactador_e_basculante_entram():
    assert eh_coleta("SDM-4D87", "VW/18.260 CRM 4X2 4P") is True
    assert eh_coleta("VCK-0D86", "VW/26.260 CRM 6X2 4P") is True
    assert eh_coleta("SCN-2B79", "IVECO/TECTOR 24-280") is True
    assert eh_coleta("SCN-4E19", "IVECO/TECTOR 15-210") is True
    assert eh_coleta("SCN-4B49", "IVECO/TECTOR 24-280") is True
    assert eh_coleta("SCN-1G89", "CBT012") is True
    assert eh_coleta("SCH-9B28", "") is True


def test_placa_nova_do_rastreador_fica_sem_tipo(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    from inlog_agent.cadastro_frota import definir, invalidar, listar, tipo_de
    from inlog_agent.live_db import conectar

    invalidar()
    con = conectar()
    con.execute(
        "INSERT INTO placa (data, turno, placa, veiculo) VALUES (?, ?, ?, ?)",
        ("2026-11-11", "diurno", "XYZ-1A23", "CAMINHAO NOVO"),
    )
    con.commit()
    con.close()
    assert tipo_de("XYZ-1A23") == "sem_tipo"
    assert eh_coleta("XYZ-1A23", "CAMINHAO NOVO") is False
    item = next(linha for linha in listar() if linha["placa"] == "XYZ-1A23")
    assert item["tipo"] == "sem_tipo"
    assert item["veiculo"] == "CAMINHAO NOVO"
    definir("XYZ-1A23", "compactador")
    assert tipo_de("XYZ-1A23") == "compactador"
    assert eh_coleta("XYZ-1A23", "") is True
    assert next(linha for linha in listar() if linha["placa"] == "XYZ-1A23")["tipo"] == "compactador"
    invalidar()


def test_modelo_no_codigo_nao_classifica(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    from inlog_agent.cadastro_frota import definir, invalidar

    invalidar()
    assert eh_coleta("AAA-0A00", "VW/18.260 CRM 4X2") is False
    assert eh_coleta("SCN-2B79", "") is True
    definir("SCN-2B79", "outro")
    assert eh_coleta("SCN-2B79", "IVECO/TECTOR 24-280") is False
    definir("PM-001", "compactador")
    assert eh_coleta("PM-001", "PM-001") is True
    invalidar()


def test_edicao_sobrevive_e_o_json_nao_semeia_de_novo():
    from inlog_agent.cadastro_frota import definir, invalidar, tipo_de
    from inlog_agent.live_db import conectar

    assert tipo_de("SCN-4E19") == "compactador"
    definir("SCN-4E19", "trator")
    invalidar()
    assert tipo_de("SCN-4E19") == "trator"
    con = conectar()
    con.execute("DELETE FROM frota_tipo WHERE placa = 'SCN-4E19'")
    con.commit()
    con.close()
    invalidar()
    assert tipo_de("SCN-4E19") == "sem_tipo"
