"""Mapeamento da frota pela base de cada placa. Não chama a Fulltrack nem o Nominatim."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlparse

import pytest

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.frota import (
    aplicar_mapeamento,
    fixar_placa,
    listar_operacoes,
    mapear_operacoes,
    mapeamento_csv_bytes,
)
from inlog_agent.fulltrack_api import APARECIDA_LAT, APARECIDA_LON, _norm_placa
from inlog_agent.server import Handler, SalaHTTPServer


SENHA = "Admin-Rota-Vivo-1"
INICIO = datetime(2026, 10, 21, 0, 0, 0)
CAIXAS = {
    "Belo Horizonte": ("-19.97", "-19.85", "-43.95", "-43.85"),
    "Contagem": ("-19.93", "-19.89", "-44.06", "-44.02"),
    "Aparecida de Goiânia": ("-16.90", "-16.74", "-49.32", "-49.16"),
    "Londrina": ("-23.40", "-23.20", "-51.25", "-51.05"),
    "Unaí": ("-16.46", "-16.26", "-47.00", "-46.80"),
    "Ibiporã": ("-23.35", "-23.19", "-51.15", "-50.95"),
    "Pindamonhangaba": ("-23.02", "-22.82", "-45.56", "-45.36"),
    "Porto Velho": ("-8.86", "-8.66", "-64.00", "-63.80"),
    "Uberaba": ("-19.85", "-19.65", "-48.03", "-47.83"),
}
# (-19.910, -44.040) é o centro da caixa de Contagem, mas o Nominatim responde Belo Horizonte.
LUGARES = {
    ("-19.910", "-44.040"): ("Belo Horizonte", "Minas Gerais", "BR-MG"),
    ("-19.930", "-44.080"): ("Contagem", "Minas Gerais", "BR-MG"),
    ("-19.880", "-43.900"): ("Belo Horizonte", "Minas Gerais", "BR-MG"),
    ("-19.960", "-43.900"): ("Betim", "Minas Gerais", "BR-MG"),
    ("-23.550", "-46.630"): ("São Paulo", "São Paulo", "BR-SP"),
    ("-19.750", "-47.930"): ("Uberaba", "Minas Gerais", "BR-MG"),
    ("-19.920", "-43.940"): ("Belo Horizonte", "Minas Gerais", "BR-MG"),
}
VEICULOS = [
    {"ras_vei_placa": "GAR1A11", "ras_vei_id": "1", "ras_vei_veiculo": "Base BH"},
    {"ras_vei_placa": "GAR2B22", "ras_vei_id": "2", "ras_vei_veiculo": "Base Contagem"},
    {"ras_vei_placa": "BET3C33", "ras_vei_id": "3", "ras_vei_veiculo": "Betim"},
    {"ras_vei_placa": "SAO4D44", "ras_vei_id": "4", "ras_vei_veiculo": "Sao Paulo"},
    {"ras_vei_placa": "SEM5E55", "ras_vei_id": "5", "ras_vei_veiculo": "Sem pulso"},
    {"ras_vei_placa": "MAN6F66", "ras_vei_id": "6", "ras_vei_veiculo": "Manual"},
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
    frota._GEO_AVISO = False
    frota._PULSO_STATS["sem_coord"] = 0
    yield tmp_path
    frota._CACHE.update(chave="", corpo=None, expira=0.0)
    frota._GEO_ULTIMO = 0.0


def _serie(placa, lat, lon, vel, duracao_min, passo_min, deslocamento_min=0):
    pontos = []
    cursor = INICIO + timedelta(minutes=deslocamento_min)
    fim = cursor + timedelta(minutes=duracao_min)
    while cursor <= fim:
        pontos.append(
            {
                "placa": placa,
                "ts": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                "lat": lat,
                "lon": lon,
                "vel": vel,
            }
        )
        cursor += timedelta(minutes=passo_min)
    return pontos


def _pontos_da_frota():
    base_bh = _serie("GAR1A11", -19.910, -44.040, 0, 600, 10)
    anda_contagem = _serie("GAR1A11", -19.930, -44.080, 40, 119, 1, deslocamento_min=601)
    base_contagem = _serie("GAR2B22", -19.930, -44.080, 0, 600, 10)
    anda_bh = _serie("GAR2B22", -19.880, -43.900, 40, 119, 1, deslocamento_min=601)
    betim = _serie("BET3C33", -19.960, -43.900, 0, 480, 10)
    saopaulo = _serie("SAO4D44", -23.550, -46.630, 0, 480, 10)
    uberaba = _serie("MAN6F66", -19.750, -47.930, 0, 480, 10)
    return {
        "GAR1A11": base_bh + anda_contagem,
        "GAR2B22": base_contagem + anda_bh,
        "BET3C33": betim,
        "SAO4D44": saopaulo,
        "MAN6F66": uberaba,
    }


class _Resposta:
    def __init__(self, corpo: bytes):
        self._corpo = corpo

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._corpo


def _preparar(monkeypatch, pontos=None, eventos=None, falhas=None):
    from inlog_agent import frota

    tabela = pontos if pontos is not None else _pontos_da_frota()
    recusas = {_norm_placa(placa) for placa in (falhas or [])}
    janela = {}

    def historico(placa, inicio, fim, **kwargs):
        janela["inicio"] = inicio
        janela["fim"] = fim
        if _norm_placa(placa) in recusas:
            raise RuntimeError("histórico pesado")
        return list(tabela.get(_norm_placa(placa), []))

    def urlopen(pedido, timeout=12):
        url = pedido.full_url
        consulta = parse_qs(urlparse(url).query)
        if "/reverse" in url:
            chave = (f"{float(consulta['lat'][0]):.3f}", f"{float(consulta['lon'][0]):.3f}")
            cidade, estado, iso = LUGARES[chave]
            corpo = {
                "display_name": f"{cidade}, {estado}",
                "address": {"city": cidade, "state": estado, "ISO3166-2-lvl4": iso},
            }
            return _Resposta(json.dumps(corpo).encode())
        if "/search" in url:
            cidade = consulta["q"][0].split(",")[0].strip()
            corpo = [{"boundingbox": list(CAIXAS[cidade])}]
            return _Resposta(json.dumps(corpo).encode())
        raise AssertionError(url)

    monkeypatch.setattr(frota.time, "sleep", lambda segundos: None)
    monkeypatch.setattr(frota, "load_settings", lambda: object())
    monkeypatch.setattr(frota, "login", lambda settings: "tok")
    monkeypatch.setattr(frota, "listar_veiculos", lambda settings, token=None: list(VEICULOS))
    monkeypatch.setattr(frota, "_eventos_atuais", lambda settings, token: list(eventos or []))
    monkeypatch.setattr(frota, "historico", historico)
    monkeypatch.setattr(frota, "urlopen", urlopen)
    return janela


def _por_placa(resultado):
    return {item["placa"]: item for item in resultado["veiculos"]}


def _ops():
    return {op["nome"]: op for op in listar_operacoes()["operacoes"]}


def test_base_pela_celula_e_vizinha_decidida_pela_cidade(ambiente, monkeypatch):
    janela = _preparar(monkeypatch)
    fixar_placa(1, "MAN6F66")
    resultado = mapear_operacoes()
    assert (janela["fim"] - janela["inicio"]) == timedelta(days=7)
    placas = _por_placa(resultado)
    bh = placas["GAR-1A11"]
    assert bh["base_lat"] == pytest.approx(-19.91)
    assert bh["base_lon"] == pytest.approx(-44.04)
    assert bh["horas_na_base"] == 10.0
    assert len(_pontos_da_frota()["GAR1A11"]) > 61
    assert bh["cidade_da_base"] == "Belo Horizonte"
    assert bh["cidade_onde_mais_rodou"] == "Contagem"
    assert bh["operacao_sugerida"] == "Belo Horizonte - MG"
    assert bh["situacao"] == "associado"
    contagem = placas["GAR-2B22"]
    assert contagem["cidade_da_base"] == "Contagem"
    assert contagem["cidade_onde_mais_rodou"] == "Belo Horizonte"
    assert contagem["operacao_sugerida"] == "Contagem - MG"
    assert contagem["situacao"] == "associado"
    revisar = placas["BET-3C33"]
    assert revisar["situacao"] == "revisar"
    assert revisar["cidade_da_base"] == "Betim"
    assert revisar["operacao_sugerida"] == "Belo Horizonte - MG"
    fora = placas["SAO-4D44"]
    assert fora["situacao"] == "não associado"
    assert fora["operacao_sugerida"] == ""
    sem = placas["SEM-5E55"]
    assert sem["situacao"] == "sem dados"
    assert sem["base_lat"] is None
    assert resultado["verificacao"]["total_veiculos"] == 6
    assert resultado["verificacao"]["com_pulso"] == 5
    assert resultado["verificacao"]["sem_pulso"] == 1
    assert resultado["verificacao"]["sem_posicao_valida"] == 1
    assert _ops()["Aparecida de Goiânia"]["placas"] == ["MAN-6F66"]
    assert len(_ops()) == 1


def test_aplicar_duas_vezes_nao_duplica_e_preserva_vinculo_manual(ambiente, monkeypatch):
    _preparar(monkeypatch)
    fixar_placa(1, "man6f66")
    resultado = mapear_operacoes()
    primeira = aplicar_mapeamento(resultado)
    assert "Belo Horizonte - MG" in primeira["criadas"]
    assert "Aparecida de Goiânia" not in primeira["criadas"]
    assert set(primeira["fixadas"]) == {"GAR-1A11", "GAR-2B22"}
    assert primeira["preservadas"] == ["MAN-6F66"]
    ops = _ops()
    assert len(ops) == 9
    assert len({op["nome"] for op in ops.values()}) == 9
    assert ops["Aparecida de Goiânia"]["padrao"] is True
    assert ops["Aparecida de Goiânia"]["lat_min"] == APARECIDA_LAT[0]
    assert ops["Aparecida de Goiânia"]["lon_max"] == APARECIDA_LON[1]
    assert ops["Aparecida de Goiânia"]["placas"] == ["MAN-6F66"]
    assert ops["Belo Horizonte - MG"]["placas"] == ["GAR-1A11"]
    assert ops["Contagem - MG"]["placas"] == ["GAR-2B22"]
    assert ops["Uberaba - MG"]["placas"] == []
    assert "BET-3C33" not in [placa for op in ops.values() for placa in op["placas"]]
    segunda = aplicar_mapeamento(resultado)
    assert segunda["criadas"] == []
    assert segunda["fixadas"] == []
    assert segunda["preservadas"] == ["MAN-6F66"]
    de_novo = _ops()
    assert len(de_novo) == 9
    assert sum(len(op["placas"]) for op in de_novo.values()) == 3
    assert de_novo["Aparecida de Goiânia"]["placas"] == ["MAN-6F66"]
    assert de_novo["Belo Horizonte - MG"]["placas"] == ["GAR-1A11"]


def test_historico_pesado_marca_base_pela_posicao_atual(ambiente, monkeypatch):
    evento = {
        "ras_vei_id": "1",
        "ras_vei_placa": "GAR1A11",
        "ras_eve_latitude": "-19.92",
        "ras_eve_longitude": "-43.94",
        "ras_eve_data_gps": "2026-10-28 12:00:00",
    }
    _preparar(monkeypatch, pontos={}, eventos=[evento], falhas=["GAR1A11"])
    linha = _por_placa(mapear_operacoes())["GAR-1A11"]
    assert linha["origem"] == "base pela posição atual"
    assert linha["cidade_da_base"] == "Belo Horizonte"
    assert linha["situacao"] == "associado"
    assert linha["horas_na_base"] is None


def test_csv_usa_ponto_e_virgula_e_bom(ambiente, monkeypatch):
    _preparar(monkeypatch)
    bruto = mapeamento_csv_bytes(mapear_operacoes())
    assert bruto.startswith(b"\xef\xbb\xbf")
    texto = bruto.decode("utf-8-sig")
    assert texto.splitlines()[0] == (
        "placa;descricao;base_lat;base_lon;cidade_da_base;horas_na_base;"
        "cidade_onde_mais_rodou;operacao_sugerida;situacao;origem"
    )
    assert "GAR-1A11;Base BH;" in texto
    assert ";associado;historico" in texto
    assert "BET-3C33;Betim;" in texto
    assert ";revisar;historico" in texto
    assert ";não associado;historico" in texto
    assert "SEM-5E55;Sem pulso;;;;;;;sem dados;" in texto


def test_cli_simular_e_o_padrao(ambiente, monkeypatch, capsys):
    from inlog_agent.cli import main

    monkeypatch.setattr(
        "inlog_agent.frota.mapear_operacoes",
        lambda dias=7: {"ok": True, "verificacao": {}, "resumo": [], "revisar": [], "nao_associado": [], "sem_dados": [], "veiculos": []},
    )
    monkeypatch.setattr("inlog_agent.frota.salvar_mapeamento_csv", lambda resultado, destino=None: "saida.csv")
    chamado = {}
    monkeypatch.setattr(
        "inlog_agent.frota.aplicar_mapeamento",
        lambda resultado=None, ajustes=False: chamado.update(aplicou=True) or {"ok": True, "criadas": [], "fixadas": [], "preservadas": []},
    )
    main(["frota-mapear"])
    assert "aplicou" not in chamado
    assert "Simulação" in capsys.readouterr().out
    main(["frota-mapear", "--aplicar"])
    assert chamado["aplicou"] is True


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


def test_ajustes_aprovados_criam_cidades_e_fixam_so_as_placas_pedidas(ambiente, monkeypatch):
    from inlog_agent import frota

    caixas = dict(CAIXAS)
    caixas["Ariquemes"] = ("-10.20", "-9.20", "-64.20", "-63.20")
    caixas["Caldas Novas"] = ("-17.80", "-17.70", "-48.70", "-48.60")

    def urlopen(pedido, timeout=12):
        consulta = parse_qs(urlparse(pedido.full_url).query)
        assert "/search" in pedido.full_url
        cidade = consulta["q"][0].split(",")[0].strip()
        return _Resposta(json.dumps([{"boundingbox": list(caixas[cidade])}]).encode())

    monkeypatch.setattr(frota.time, "sleep", lambda segundos: None)
    monkeypatch.setattr(frota, "urlopen", urlopen)
    monkeypatch.setattr(frota, "load_settings", lambda: object())
    monkeypatch.setattr(frota, "login", lambda settings: "tok")
    monkeypatch.setattr(frota, "listar_veiculos", lambda settings, token=None: [])
    monkeypatch.setattr(frota, "_eventos_atuais", lambda settings, token: [])

    def linha(placa, situacao, operacao=""):
        return {
            "placa": placa,
            "situacao": situacao,
            "operacao_sugerida": operacao,
            "operacao_chave": frota._chave_sugerida(operacao),
        }

    veiculos = [linha("APA-0001", "associado", "Aparecida de Goiânia")]
    veiculos.extend(linha(placa, "não associado") for placa in frota._PLACAS_ARIQUEMES)
    veiculos.extend(linha(placa, "não associado") for placa in frota._PLACAS_CALDAS)
    veiculos.append(linha("TRZ-5F28", "revisar", "Contagem - MG"))
    veiculos.append(linha("SEM-5E55", "sem dados"))
    veiculos.append(linha("BET-3C33", "revisar", "Belo Horizonte - MG"))
    resultado = {"ok": True, "veiculos": veiculos}
    primeira = aplicar_mapeamento(resultado, ajustes=True)
    ops = _ops()
    assert len(ops) == 11
    assert set(ops["Ariquemes - RO"]["placas"]) == {
        "CAT226B3",
        "RVE-2H79",
        "RVU-8C66",
        "TTV-9G70",
        "TUF-3C31",
        "UAX-0F65",
        "YTO NX854 0",
    }
    assert ops["Ariquemes - RO"]["lat_min"] == pytest.approx(-10.2)
    assert ops["Ariquemes - RO"]["lon_max"] == pytest.approx(-63.2)
    assert set(ops["Caldas Novas - GO"]["placas"]) == {"SCO-6H09", "TBY-3G12"}
    assert ops["Contagem - MG"]["placas"] == ["TRZ-5F28"]
    assert ops["Belo Horizonte - MG"]["placas"] == []
    assert ops["Unaí - MG"]["placas"] == []
    assert ops["Ibiporã - PR"]["placas"] == []
    assert ops["Aparecida de Goiânia"]["placas"] == ["APA-0001"]
    assert ops["Aparecida de Goiânia"]["padrao"] is True
    todas = [placa for op in ops.values() for placa in op["placas"]]
    assert "SEM-5E55" not in todas
    assert "BET-3C33" not in todas
    assert len(primeira["fixadas"]) == 11
    segunda = aplicar_mapeamento(resultado, ajustes=True)
    assert segunda["criadas"] == []
    assert segunda["fixadas"] == []
    assert len(_ops()) == 11


def test_mapeamento_exige_admin_e_csrf(ambiente, monkeypatch):
    monkeypatch.setattr(
        "inlog_agent.frota.mapear_operacoes",
        lambda dias=7: {"ok": True, "veiculos": [], "resumo": []},
    )
    monkeypatch.setattr(
        "inlog_agent.frota.aplicar_mapeamento",
        lambda resultado=None, ajustes=False: {"ok": True, "criadas": [], "fixadas": []},
    )
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _payload = _chamar(anon, base + "/api/frota/mapeamento")
        assert status == 401
        criar_primeiro_admin("admin@mapa", SENHA, "Administrador")
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        status, sessao = _chamar(admin, base + "/api/auth/login", {"login": "admin@mapa", "senha": SENHA}, method="POST")
        assert status == 200
        status, payload = _chamar(admin, base + "/api/frota/mapeamento")
        assert status == 200
        assert payload["ok"] is True
        status, _payload = _chamar(admin, base + "/api/frota/mapeamento/aplicar", {}, method="POST")
        assert status == 403
        status, payload = _chamar(
            admin,
            base + "/api/frota/mapeamento/aplicar",
            {},
            headers={"X-CSRF-Token": sessao["csrf"]},
            method="POST",
        )
        assert status == 200
        assert payload["ok"] is True
    finally:
        httpd.shutdown()
