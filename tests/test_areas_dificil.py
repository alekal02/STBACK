"""Polígonos oficiais de difícil acesso ficam fora da grade e da cobertura."""

import json

from inlog_agent.mapa_oficial import (
    carregar_areas_dificil,
    eh_area_dificil,
    listar_rotas_oficiais,
)


def test_codigo_oficial_e_area_e_rota_comum_nao_e():
    assert eh_area_dificil("DIFÍCIL ACESSO")
    assert eh_area_dificil("DIFICIL ACESSO")
    assert not eh_area_dificil("01AD")
    assert not eh_area_dificil("66DN")


def test_gpkg_entrega_onze_poligonos_com_bairro():
    areas = carregar_areas_dificil()
    assert len(areas) == 11
    nomes = [area["nome"] for area in areas]
    assert nomes.count("CHACARA SAO PEDRO") == 2
    assert "ALTO DA BOA VISTA" in nomes
    assert "GIRASSOIS" in nomes
    assert "TERRA DO SOL" in nomes
    for area in areas:
        assert eh_area_dificil(area["codigo"])
        assert area["nome"]
        assert len(area["poligonos"]) == 1
        anel = area["poligonos"][0]
        assert area["id"]
        assert len(anel) >= 4
        lat, lon = anel[0]
        assert -17.2 < lat < -16.4
        assert -49.6 < lon < -48.9
    assert len({area["id"] for area in areas}) == 11
    rotas = listar_rotas_oficiais(None)
    assert rotas
    assert not any(eh_area_dificil(rota["codigo"]) for rota in rotas)


def test_api_separa_areas_das_rotas_e_do_gps(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    import inlog_agent.live_mapa as mapa
    from inlog_agent.live_db import conectar

    mapa._AREAS = None
    mapa._POLIGONOS = None
    mapa._CORPOS.clear()
    pacote = mapa.dificil_acesso("2026-10-12", "diurno")
    assert pacote["camada"] == "Áreas de difícil acesso"
    assert len(pacote["areas"]) == 11
    assert pacote["resumo"]["areas"] == 11
    assert all(item["poligonos"] == [] for item in pacote["veiculos"])
    body, _etag = mapa.geometria_bytes("2026-10-12", "diurno")
    colecao = json.loads(body)
    codigos = {
        str((feat.get("properties") or {}).get("codigo") or feat.get("id") or "")
        for feat in colecao["features"]
    }
    assert codigos
    assert not any(eh_area_dificil(codigo) for codigo in codigos)
    con = conectar()
    tabelas = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "area_dificil" not in tabelas
