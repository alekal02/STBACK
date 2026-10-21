"""Programação das áreas de difícil acesso: dias do GPKG e exceção por data."""

from datetime import date, datetime

from inlog_agent.dificil_acesso import anotar_programacao, programada_na_data
from inlog_agent.mapa_oficial import carregar_areas_dificil


def _por_nome(areas):
    grupos: dict[str, list[bool]] = {}
    for area in areas:
        grupos.setdefault(area["nome"], []).append(bool(area["programada"]))
    return grupos


def test_gpkg_guarda_os_dias_e_filtra_sem_mexer_no_anel():
    areas = carregar_areas_dificil()
    assert len(areas) == 11
    frequencias = {area["nome"]: area["frequencia"] for area in areas}
    assert frequencias["ALTO DA BOA VISTA"] == "TER/SAB - DIURNO"
    assert frequencias["GIRASSOIS"] == "SEG/SEX - DIURNO"
    assert frequencias["TERRA DO SOL"] == "QUI"
    assert all(area["frequencia"] for area in areas)

    segunda = anotar_programacao(areas, date(2026, 10, 12), "diurno", [])
    nomes = _por_nome(segunda)
    assert all(nomes["GIRASSOIS"])
    assert all(nomes["CHACARA SAO PEDRO"])
    assert all(nomes["SERRA DOURADA"])
    assert all(nomes["SANTA LUZIA/ROSA DO SUL"])
    assert not any(nomes["ALTO DA BOA VISTA"])
    assert not any(nomes["TERRA DO SOL"])
    assert sum(1 for area in segunda if area["programada"]) == 5

    terca = _por_nome(anotar_programacao(areas, date(2026, 10, 13), "diurno", []))
    assert all(terca["ALTO DA BOA VISTA"])
    assert all(terca["BURITI SERENO"])
    assert not any(terca["GIRASSOIS"])
    assert not any(terca["TERRA DO SOL"])

    quarta = anotar_programacao(areas, date(2026, 10, 14), "diurno", [])
    assert not any(area["programada"] for area in quarta)

    quinta = _por_nome(anotar_programacao(areas, date(2026, 10, 15), "diurno", []))
    assert all(quinta["TERRA DO SOL"])
    assert not any(quinta["GIRASSOIS"])
    assert not any(quinta["ALTO DA BOA VISTA"])

    domingo = anotar_programacao(areas, date(2026, 10, 18), "diurno", [])
    assert not any(area["programada"] for area in domingo)
    noturno = anotar_programacao(areas, date(2026, 10, 12), "noturno", [])
    assert not any(area["programada"] for area in noturno)

    def nomes_programados(dia, turno):
        marcadas = anotar_programacao(areas, dia, turno, [])
        return sorted(area["nome"] for area in marcadas if area["programada"])

    assert nomes_programados(date(2026, 10, 21), "diurno") == []
    assert nomes_programados(date(2026, 10, 22), "diurno") == ["TERRA DO SOL"]
    assert nomes_programados(date(2026, 10, 23), "diurno") == sorted([
        "CHACARA SAO PEDRO",
        "CHACARA SAO PEDRO",
        "SERRA DOURADA",
        "SANTA LUZIA/ROSA DO SUL",
        "GIRASSOIS",
    ])
    assert nomes_programados(date(2026, 10, 24), "diurno") == sorted([
        "ALTO DA BOA VISTA",
        "MADRE GERMANA",
        "QUINTA DA BOA VISTA",
        "JARDIM DOS IPES",
        "BURITI SERENO",
    ])
    for dia in (21, 22, 23, 24):
        assert nomes_programados(date(2026, 10, dia), "noturno") == []

    for original, marcado in zip(areas, segunda):
        assert marcado["poligonos"] == original["poligonos"]
        assert marcado["id"] == original["id"]
        assert "programada" not in original


def test_excecao_por_data_inclui_ou_cancela_um_dia():
    area = {
        "id": "GIRASSOIS|teste",
        "nome": "GIRASSOIS",
        "frequencia": "SEG/SEX - DIURNO",
        "poligonos": [[[0, 0], [0, 1], [1, 1], [0, 0]]],
    }
    segunda = date(2026, 10, 12)
    domingo = date(2026, 10, 18)
    cancela = [{"nome": "Girassóis", "data": "2026-10-12", "turno": "diurno", "executar": False}]
    inclui = [{"id": "GIRASSOIS|teste", "data": "2026-10-18", "executar": True}]
    assert programada_na_data(area, segunda, "diurno", cancela) is False
    assert programada_na_data(area, domingo, "diurno", inclui) is True
    assert programada_na_data(area, segunda, "diurno", inclui) is True
    outro_dia = [{"nome": "GIRASSOIS", "data": "2026-10-13", "executar": False}]
    assert programada_na_data(area, segunda, "diurno", outro_dia) is True


def test_api_marca_o_dia_e_mantem_trajetoria_fora_da_programacao(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    monkeypatch.setattr(
        "inlog_agent.live_sync.agora_brt",
        lambda: datetime(2026, 10, 14, 10, 0, 0),
    )
    import inlog_agent.live_mapa as mapa
    from inlog_agent.live_db import conectar, inserir_pontos

    area = {
        "id": "ALTO|teste",
        "nome": "ALTO DA BOA VISTA",
        "codigo": "DIFÍCIL ACESSO",
        "frequencia": "TER/SAB - DIURNO",
        "completo": "DIFÍCIL ACESSO - ALTO DA BOA VISTA",
        "poligonos": [[
            [-16.80, -49.30],
            [-16.80, -49.20],
            [-16.90, -49.20],
            [-16.90, -49.30],
            [-16.80, -49.30],
        ]],
    }
    mapa._AREAS = None
    monkeypatch.setattr(mapa, "areas_dificil", lambda: [area])
    con = conectar()
    inserir_pontos(
        con,
        "2026-10-14",
        "diurno",
        "SCN-4B49",
        [
            {"timestamp": "2026-10-14T09:00:00", "latitude": -16.85, "longitude": -49.25},
            {"timestamp": "2026-10-14T09:00:20", "latitude": -16.8504, "longitude": -49.2501},
        ],
    )
    con.commit()
    con.close()

    pacote = mapa.dificil_acesso("2026-10-14", "diurno")
    assert len(pacote["areas"]) == 1
    assert pacote["areas"][0]["programada"] is False
    assert pacote["areas"][0]["poligonos"] == area["poligonos"]
    cbt018 = next(item for item in pacote["veiculos"] if item["codigo"] == "CBT018")
    assert cbt018["trajeto"]
    assert cbt018["eventos"]
    assert cbt018["trechos_internos"] == []
    assert area["id"] not in cbt018["trechos_por_area"]
    assert not pacote["areas"][0].get("fora_programacao")
    assert pacote["resumo"]["areas"] == 1
    con = conectar()
    gravados = con.execute(
        "SELECT COUNT(*) FROM ponto WHERE data = '2026-10-14' AND turno = 'diurno'"
    ).fetchone()[0]
    con.close()
    assert gravados == 2
