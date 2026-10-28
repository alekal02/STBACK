"""Telemetria sem Nominatim síncrono. Não chama a Fulltrack."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from inlog_agent.frota import consultar_enderecos, consultar_paradas, consultar_telemetria, esperar_enderecos


PERIODO = {
    "data": ["2026-10-21"],
    "turno": ["diurno"],
    "operacao": ["todas"],
    "placa": ["ABC1D23"],
}


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import frota

    frota._TEL.clear()
    frota._FILA.clear()
    frota._FILA_SET.clear()
    frota._GEO_ULTIMO = 0.0
    yield frota
    frota._TEL.clear()
    frota._FILA.clear()
    frota._FILA_SET.clear()


def _preparar(frota, monkeypatch, historico):
    monkeypatch.setattr(frota, "load_settings", lambda: object())
    monkeypatch.setattr(frota, "login", lambda settings: "tok")
    monkeypatch.setattr(
        frota,
        "listar_veiculos",
        lambda settings, token=None: [
            {"ras_vei_placa": "ABC1D23", "ras_vei_id": "9", "ras_vei_veiculo": "TESTE", "ras_vei_velocidade_limite": 60}
        ],
    )
    monkeypatch.setattr(frota, "historico", historico)
    monkeypatch.setattr(frota, "alertas_periodo", lambda *args, **kwargs: [])
    monkeypatch.setattr(frota, "consolidado_veiculo", lambda *args, **kwargs: None)


def _parada(segundos):
    return {
        "placa": "ABC-1D23",
        "ts": (datetime(2026, 10, 21, 8, 0, 0) + timedelta(seconds=segundos)).strftime("%Y-%m-%dT%H:%M:%S"),
        "lat": -16.82,
        "lon": -49.25,
        "vel": 0,
        "ignicao": True,
    }


def test_telemetria_nao_chama_o_nominatim(ambiente, monkeypatch):
    frota = ambiente
    pedidos = []
    monkeypatch.setattr(frota, "urlopen", lambda *args, **kwargs: pedidos.append("nominatim"))
    _preparar(frota, monkeypatch, lambda *args, **kwargs: [])
    corpo = consultar_telemetria(PERIODO)
    assert corpo["ok"] is True
    assert corpo["consolidado_indisponivel"] is False
    assert pedidos == []


def test_timeout_de_consolidado_e_alertas_segue_sem_eles(ambiente, monkeypatch):
    frota = ambiente

    def estoura(*args, **kwargs):
        raise TimeoutError("estourou")

    _preparar(frota, monkeypatch, lambda *args, **kwargs: [])
    monkeypatch.setattr(frota, "alertas_periodo", estoura)
    monkeypatch.setattr(frota, "consolidado_veiculo", estoura)
    corpo = consultar_telemetria(PERIODO)
    assert corpo["ok"] is True
    assert corpo["consolidado_indisponivel"] is True
    assert corpo["eventos"] == []


def test_paradas_devolvem_null_e_enfileiram(ambiente, monkeypatch):
    frota = ambiente
    monkeypatch.setattr(frota, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nominatim")))
    monkeypatch.setattr(frota, "historico", lambda *args, **kwargs: [_parada(0), _parada(10 * 60)])
    corpo = consultar_paradas({**PERIODO, "min": ["5"], "max": ["60"]})
    assert len(corpo["paradas"]) == 1
    parada = corpo["paradas"][0]
    assert parada["endereco"] is None
    assert parada["lat"] == pytest.approx(-16.82)
    assert parada["lon"] == pytest.approx(-49.25)
    assert list(frota._FILA) == [("-16.8200", "-49.2500")]


def test_enderecos_le_o_cache_e_enfileira_o_resto(ambiente):
    frota = ambiente
    frota._com_banco(lambda con: frota._geo_gravar(con, "-16.8200", "-49.2500", "Rua A"))
    corpo = consultar_enderecos({"pontos": ["-16.82,-49.25;-16.70,-49.10"]})
    assert corpo["enderecos"][0] == {"lat": -16.82, "lon": -49.25, "endereco": "Rua A"}
    assert corpo["enderecos"][1]["endereco"] is None
    assert ("-16.7000", "-49.1000") in frota._FILA
    muitos = ";".join(f"{-16.5 - i * 0.01:.4f},-49.25" for i in range(201))
    with pytest.raises(ValueError, match="200"):
        consultar_enderecos({"pontos": [muitos]})
    with pytest.raises(ValueError, match="Ponto inválido"):
        consultar_enderecos({"pontos": ["abc"]})


def test_fila_nao_duplica_e_respeita_o_tamanho(ambiente):
    frota = ambiente
    chaves = [(f"{-20 - i * 0.0001:.4f}", "-49.2500") for i in range(450)]
    frota._enfileirar_chaves(chaves)
    assert len(frota._FILA) == 400
    frota._enfileirar_chaves([chaves[0], chaves[0]])
    assert len(frota._FILA) == 400
    assert list(frota._FILA).count(chaves[0]) == 1


def test_worker_respeita_um_por_segundo(ambiente, monkeypatch):
    frota = ambiente
    dormiu = []

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"display_name":"Rua B"}'

    monkeypatch.setattr(frota.time, "sleep", lambda segundos: dormiu.append(segundos))
    monkeypatch.setattr(frota, "urlopen", lambda *args, **kwargs: Resposta())
    frota._GEO_ULTIMO = time.monotonic()
    frota._enfileirar_chaves([("-16.8100", "-49.2500")])
    assert frota._processar_um_geo() is True
    assert dormiu and dormiu[0] > 0.5
    assert frota._FILA_SET == set()


def test_pdf_desiste_do_endereco_se_o_tempo_acaba(ambiente):
    itens = [{"lat": -16.81, "lon": -49.26, "endereco": None}]
    inicio = time.perf_counter()
    esperar_enderecos(itens, 0.35)
    assert time.perf_counter() - inicio < 2
    assert itens[0]["endereco"] is None


def test_cache_de_30s_no_turno_aberto_e_60s_depois(ambiente, monkeypatch):
    frota = ambiente
    relogio = {"t": 1000.0}
    agora = {"quando": datetime(2026, 10, 21, 12, 0, 0)}
    chamadas = {"n": 0}

    def historico(*args, **kwargs):
        chamadas["n"] += 1
        return []

    monkeypatch.setattr(frota.time, "monotonic", lambda: relogio["t"])
    monkeypatch.setattr(frota, "_agora_brt", lambda: agora["quando"])
    _preparar(frota, monkeypatch, historico)
    consultar_telemetria(PERIODO)
    consultar_telemetria(PERIODO)
    assert chamadas["n"] == 1
    relogio["t"] += 31
    consultar_telemetria(PERIODO)
    assert chamadas["n"] == 2

    agora["quando"] = datetime(2026, 10, 28, 12, 0, 0)
    chamadas["n"] = 0
    frota._TEL.clear()
    consultar_telemetria(PERIODO)
    relogio["t"] += 31
    consultar_telemetria(PERIODO)
    assert chamadas["n"] == 1
    relogio["t"] += 30
    consultar_telemetria(PERIODO)
    assert chamadas["n"] == 2
