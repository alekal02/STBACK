"""Operações da frota por região. Não chama a Fulltrack nem o Nominatim."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.frota import (
    ao_vivo,
    apagar_operacao,
    atualizar_operacao,
    catalogo_veiculos,
    consultar_paradas,
    consultar_telemetria,
    consultar_trajeto,
    criar_operacao,
    fixar_placa,
    listar_operacoes,
    paradas_csv,
)
from inlog_agent.fulltrack_api import APARECIDA_LAT, APARECIDA_LON
from inlog_agent.server import Handler, SalaHTTPServer


SENHA = "Admin-Rota-Vivo-1"
PERIODO = {"data": ["2026-10-21"], "turno": ["diurno"]}
VEICULOS = [
    {"ras_vei_placa": "ABC1D23", "ras_vei_id": "1", "ras_vei_veiculo": "Dentro"},
    {"ras_vei_placa": "SAO1A11", "ras_vei_id": "2", "ras_vei_veiculo": "Fora"},
    {"ras_vei_placa": "SEM1B22", "ras_vei_id": "3", "ras_vei_veiculo": "Sem pulso"},
]
EVENTOS = [
    {
        "ras_vei_id": "1",
        "ras_vei_placa": "ABC1D23",
        "ras_eve_latitude": "-16.82",
        "ras_eve_longitude": "-49.25",
        "ras_eve_data_gps": "2026-10-28 15:00:00",
        "ras_eve_velocidade": "10",
        "ras_eve_ignicao": "1",
        "ras_eve_cidade": "Aparecida de Goiania",
    },
    {
        "ras_vei_id": "2",
        "ras_vei_placa": "SAO1A11",
        "ras_eve_latitude": "-23.55",
        "ras_eve_longitude": "-46.63",
        "ras_eve_data_gps": "2026-10-28 15:00:00",
        "ras_eve_velocidade": "0",
        "ras_eve_ignicao": "0",
        "ras_eve_cidade": "Sao Paulo",
    },
]


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "acesso.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import frota

    frota._CACHE.update(chave="", corpo=None, expira=0.0)
    frota._GEO_ULTIMO = 0.0
    yield tmp_path
    frota._CACHE.update(chave="", corpo=None, expira=0.0)
    frota._GEO_ULTIMO = 0.0


def _fulltrack(monkeypatch, veiculos=None, eventos=None):
    chamadas = {"login": 0, "eventos": 0, "historico": []}

    def login(settings):
        chamadas["login"] += 1
        return "tok"

    def eventos_fn(settings, token):
        chamadas["eventos"] += 1
        return list(eventos or [])

    def historico(placa, inicio, fim, **kwargs):
        chamadas["historico"].append(placa)
        return []

    monkeypatch.setattr("inlog_agent.frota.load_settings", lambda: object())
    monkeypatch.setattr("inlog_agent.frota.login", login)
    monkeypatch.setattr("inlog_agent.frota.listar_veiculos", lambda settings, token=None: list(veiculos or []))
    monkeypatch.setattr("inlog_agent.frota._eventos_atuais", eventos_fn)
    monkeypatch.setattr("inlog_agent.frota.historico", historico)
    return chamadas


def _placas(corpo):
    return [item["placa"] for item in corpo["veiculos"]]


def test_pertencimento_dentro_fora_fixado_e_sem_posicao(ambiente, monkeypatch):
    chamadas = _fulltrack(monkeypatch, VEICULOS, EVENTOS)
    vivo = ao_vivo({})
    por_placa = {item["placa"]: item for item in vivo["veiculos"]}
    assert list(por_placa) == ["ABC-1D23"]
    assert por_placa["ABC-1D23"]["fixado"] is False
    assert por_placa["ABC-1D23"]["fora_da_area"] is False
    assert chamadas["eventos"] == 1

    fixar_placa(1, "SAO1A11")
    vivo = ao_vivo({})
    por_placa = {item["placa"]: item for item in vivo["veiculos"]}
    assert set(por_placa) == {"ABC-1D23", "SAO-1A11"}
    assert por_placa["SAO-1A11"]["fixado"] is True
    assert por_placa["SAO-1A11"]["fora_da_area"] is True
    assert chamadas["eventos"] == 1

    assert "SEM-1B22" not in por_placa
    fixar_placa(1, "sem1b22")
    vivo = ao_vivo({})
    sem = {item["placa"]: item for item in vivo["veiculos"]}["SEM-1B22"]
    assert sem["fixado"] is True
    assert sem["fora_da_area"] is True
    assert sem["lat"] is None
    assert chamadas["eventos"] == 1


def test_seed_da_operacao_padrao(ambiente, monkeypatch):
    _fulltrack(monkeypatch)
    primeira = listar_operacoes()["operacoes"]
    assert len(primeira) == 1
    op = primeira[0]
    assert op["nome"] == "Aparecida de Goiânia"
    assert op["padrao"] is True
    assert op["ativa"] is True
    assert op["lat_min"] == APARECIDA_LAT[0]
    assert op["lat_max"] == APARECIDA_LAT[1]
    assert op["lon_min"] == APARECIDA_LON[0]
    assert op["lon_max"] == APARECIDA_LON[1]
    assert op["placas"] == []
    assert op["qtd"] == 0
    de_novo = listar_operacoes()["operacoes"]
    assert [item["id"] for item in de_novo] == [op["id"]]


def test_nao_apaga_a_padrao_e_so_uma_fica_padrao(ambiente, monkeypatch):
    _fulltrack(monkeypatch)
    with pytest.raises(ValueError, match="padrão"):
        apagar_operacao(1)
    criada = criar_operacao(
        {"nome": "Senador Canedo", "lat_min": -16.75, "lat_max": -16.60, "lon_min": -49.15, "lon_max": -49.05}
    )
    nova = criada["operacao"]["id"]
    assert criada["operacao"]["padrao"] is False
    atualizar_operacao(nova, {"padrao": True})
    por_id = {op["id"]: op for op in listar_operacoes()["operacoes"]}
    assert por_id[1]["padrao"] is False
    assert por_id[nova]["padrao"] is True
    assert apagar_operacao(1)["ok"] is True
    with pytest.raises(ValueError, match="Não é possível apagar a operação padrão."):
        apagar_operacao(nova)
    with pytest.raises(ValueError, match="não pode deixar de ser padrão"):
        atualizar_operacao(nova, {"padrao": False})


def test_validacao_de_limites(ambiente, monkeypatch):
    _fulltrack(monkeypatch)
    with pytest.raises(ValueError, match="mínimo precisa ser menor"):
        criar_operacao({"nome": "Invertida", "lat_min": -16.6, "lat_max": -16.7, "lon_min": -49.1, "lon_max": -49.0})
    with pytest.raises(ValueError, match="Latitude"):
        criar_operacao({"nome": "Sul demais", "lat_min": -40, "lat_max": -39, "lon_min": -50, "lon_max": -48})
    with pytest.raises(ValueError, match="Longitude"):
        criar_operacao({"nome": "Leste demais", "lat_min": -16, "lat_max": -15, "lon_min": -33, "lon_max": -32})
    with pytest.raises(ValueError, match="cidade ou lat_min"):
        criar_operacao({"nome": "Sem caixa"})
    extrema = criar_operacao({"nome": "Extremo", "lat_min": -34, "lat_max": 6, "lon_min": -74, "lon_max": -34})
    assert extrema["operacao"]["lat_min"] == -34
    assert extrema["operacao"]["lon_max"] == -34


def test_operacao_todas_e_filtro_depois_do_cache(ambiente, monkeypatch):
    chamadas = _fulltrack(monkeypatch, VEICULOS, EVENTOS)
    dentro = ao_vivo({})
    todas = ao_vivo({"operacao": ["todas"]})
    assert _placas(dentro) == ["ABC-1D23"]
    assert _placas(todas) == ["ABC-1D23", "SAO-1A11", "SEM-1B22"]
    assert "fixado" not in todas["veiculos"][0]
    assert chamadas["eventos"] == 1
    caixa = criar_operacao(
        {"nome": "Sao Paulo", "lat_min": -23.8, "lat_max": -23.3, "lon_min": -46.9, "lon_max": -46.3}
    )
    sp = ao_vivo({"operacao": [str(caixa["operacao"]["id"])]})
    assert _placas(sp) == ["SAO-1A11"]
    assert sp["veiculos"][0]["fixado"] is False
    assert sp["veiculos"][0]["fora_da_area"] is False
    assert chamadas["eventos"] == 1
    catalogo = catalogo_veiculos({"busca": ["sao"]})
    assert catalogo["qtd"] == 1
    assert catalogo["veiculos"][0]["cidade"] == "Sao Paulo"
    assert catalogo["veiculos"][0]["posicao"]["lat"] == pytest.approx(-23.55)


def test_placa_fora_da_operacao_responde_400(ambiente, monkeypatch):
    chamadas = _fulltrack(monkeypatch, VEICULOS, EVENTOS)
    for consulta in (consultar_telemetria, consultar_trajeto, consultar_paradas):
        with pytest.raises(ValueError, match="Veículo fora desta operação."):
            consulta({**PERIODO, "placa": ["SAO1A11"]})
    with pytest.raises(ValueError, match="Veículo fora desta operação."):
        paradas_csv({**PERIODO, "placa": ["SAO-1A11"]})
    assert chamadas["historico"] == []
    todas = consultar_telemetria({**PERIODO, "placa": ["SAO1A11"], "operacao": ["todas"]})
    assert todas["ok"] is True
    assert todas["placa"] == "SAO-1A11"
    assert chamadas["historico"] == ["SAO1A11"]


def test_paradas_da_frota_percorre_so_a_operacao(ambiente, monkeypatch):
    chamadas = _fulltrack(monkeypatch, VEICULOS, EVENTOS)
    corpo = consultar_paradas(PERIODO)
    assert corpo["ok"] is True
    assert chamadas["historico"] == ["ABC-1D23"]
    assert chamadas["login"] == 2
    vazio = criar_operacao(
        {"nome": "Acre", "lat_min": -9.2, "lat_max": -9.0, "lon_min": -68.2, "lon_max": -67.8}
    )
    antes = chamadas["login"]
    consultar_paradas({**PERIODO, "operacao": [str(vazio["operacao"]["id"])]})
    assert chamadas["historico"] == ["ABC-1D23"]
    assert chamadas["login"] == antes


def test_cidade_no_nominatim_respeita_um_por_segundo(ambiente, monkeypatch):
    from inlog_agent import frota

    pedidos: list[str] = []
    dormiu: list[float] = []

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([{"boundingbox": ["-16.72", "-16.62", "-49.15", "-49.05"]}]).encode()

    def urlopen(pedido, timeout=12):
        pedidos.append(pedido.full_url)
        return Resposta()

    monkeypatch.setattr(frota.time, "sleep", lambda segundos: dormiu.append(segundos))
    monkeypatch.setattr("inlog_agent.frota.urlopen", urlopen)
    frota._GEO_ULTIMO = time.monotonic()
    op = criar_operacao({"nome": "Senador Canedo", "cidade": "Senador Canedo, GO"})
    assert op["operacao"]["lat_min"] == pytest.approx(-16.72)
    assert op["operacao"]["lat_max"] == pytest.approx(-16.62)
    assert op["operacao"]["lon_min"] == pytest.approx(-49.15)
    assert op["operacao"]["lon_max"] == pytest.approx(-49.05)
    assert op["operacao"]["padrao"] is False
    assert pedidos and "/search?" in pedidos[0]
    assert "format=json" in pedidos[0]
    assert "countrycodes=br" in pedidos[0]
    assert dormiu and dormiu[0] > 0.5


def _chamar(opener, url, dados=None, headers=None, method=None):
    corpo = None if dados is None else json.dumps(dados).encode("utf-8")
    pedido = urllib.request.Request(url, data=corpo, method=method or ("POST" if corpo is not None else "GET"))
    pedido.add_header("Content-Type", "application/json")
    for chave, valor in (headers or {}).items():
        pedido.add_header(chave, valor)
    try:
        with opener.open(pedido, timeout=30) as resp:
            bruto = resp.read()
            payload = json.loads(bruto.decode("utf-8") or "{}") if bruto else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        bruto = exc.read()
        try:
            payload = json.loads(bruto.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload


def _cliente():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def test_rotas_de_operacao_exigem_admin_e_csrf(ambiente, monkeypatch):
    _fulltrack(monkeypatch, VEICULOS, EVENTOS)
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon = _cliente()
        status, payload = _chamar(anon, base + "/api/frota/operacoes")
        assert status == 401
        criar_primeiro_admin("admin@op", SENHA, "Administrador")
        admin = _cliente()
        status, sessao = _chamar(admin, base + "/api/auth/login", {"login": "admin@op", "senha": SENHA}, method="POST")
        assert status == 200
        csrf = sessao["csrf"]
        status, payload = _chamar(admin, base + "/api/frota/operacoes", {}, method="POST")
        assert status == 403
        status, payload = _chamar(
            admin,
            base + "/api/frota/operacoes",
            {"nome": "Senador Canedo", "lat_min": -16.75, "lat_max": -16.6, "lon_min": -49.15, "lon_max": -49.05},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        nova = payload["operacao"]["id"]
        status, payload = _chamar(
            admin,
            base + "/api/frota/operacoes/1",
            method="DELETE",
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 400
        assert "padrão" in payload["erro"]
        status, payload = _chamar(
            admin,
            base + f"/api/frota/operacoes/{nova}/placas",
            {"placa": "sao1a11"},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        assert payload["placa"] == "SAO-1A11"
        status, payload = _chamar(
            admin,
            base + "/api/frota/telemetria?placa=SAO1A11&data=2026-10-21&turno=diurno",
        )
        assert status == 400
        assert payload["erro"] == "Veículo fora desta operação."
        status, payload = _chamar(
            admin,
            base + "/api/frota/ao-vivo",
            {},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 405
        status, payload = _chamar(
            admin,
            base + "/api/frota/ao-vivo",
            {},
            headers={"X-CSRF-Token": csrf},
            method="PUT",
        )
        assert status == 405
        status, payload = _chamar(admin, base + "/api/frota/operacoes")
        assert status == 200
        assert payload["operacoes"][0]["nome"] == "Aparecida de Goiânia"
    finally:
        httpd.shutdown()
