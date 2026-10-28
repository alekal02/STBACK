"""Paradas, km e acesso da frota. Não chama a Fulltrack."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar

import pytest

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.frota import (
    detectar_paradas,
    endereco,
    historico,
    janela_do_turno,
    km_rodados,
    resolver_periodo,
)
from inlog_agent.gps import haversine_m
from inlog_agent.server import Handler, SalaHTTPServer


SENHA_ADMIN = "Admin-Rota-Vivo-1"
SENHA_OP = "Operador-Rota-2"
LAT0 = -16.82
LON0 = -49.25
BASE = datetime(2026, 10, 21, 8, 0, 0)
METRO_GRAU = 111_195.0


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "acesso.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    return tmp_path


def _ponto(minutos=0, *, segundos=0, norte_m=0.0, vel=0, ignicao=None, placa="ABC1D23"):
    quando = BASE + timedelta(minutes=minutos, seconds=segundos)
    return {
        "placa": placa,
        "ts": quando.strftime("%Y-%m-%dT%H:%M:%S"),
        "lat": LAT0 + norte_m / METRO_GRAU,
        "lon": LON0,
        "vel": vel,
        "ignicao": ignicao,
    }


def test_so_registra_parada_entre_5_e_60_minutos():
    pontos = [
        _ponto(0, vel=0),
        _ponto(4, vel=0),
        _ponto(5, vel=30, norte_m=30),
        _ponto(6, vel=0, norte_m=30),
        _ponto(11, vel=0, norte_m=30),
        _ponto(16, vel=0, norte_m=30),
        _ponto(21, vel=0, norte_m=30),
        _ponto(26, vel=0, norte_m=30),
        _ponto(27, vel=30, norte_m=80),
        _ponto(28, vel=0, norte_m=80),
        _ponto(98, vel=0, norte_m=80),
    ]
    paradas = detectar_paradas(pontos)
    assert len(paradas) == 1
    parada = paradas[0]
    assert parada["duracao_min"] == 20
    assert parada["inicio"].endswith("08:06:00")
    assert parada["fim"].endswith("08:26:00")
    assert parada["qtd_pulsos"] == 5
    assert parada["com_lacuna"] is False
    assert parada["ignicao_desligada"] is None
    assert parada["dentro_da_rota"] is None
    assert parada["rota"] is None
    assert parada["placa"] == "ABC-1D23"


def test_deriva_de_40_m_continua_na_mesma_parada():
    perto = _ponto(0, vel=0, norte_m=0)
    longe = _ponto(10, vel=0, norte_m=40)
    dist = haversine_m((perto["lat"], perto["lon"]), (longe["lat"], longe["lon"]))
    assert 30 < dist <= 60
    pontos = [_ponto(i, vel=0, norte_m=0) for i in range(0, 11)]
    pontos += [_ponto(i, vel=0, norte_m=40) for i in range(11, 21)]
    paradas = detectar_paradas(pontos)
    assert len(paradas) == 1
    assert paradas[0]["duracao_min"] == 20
    assert paradas[0]["qtd_pulsos"] == 21
    assert paradas[0]["com_lacuna"] is False


def test_lacuna_de_sinal_permanece_na_parada():
    pontos = [
        _ponto(0, vel=0),
        _ponto(4, vel=0),
        _ponto(16, vel=0),
        _ponto(20, vel=0),
    ]
    paradas = detectar_paradas(pontos)
    assert len(paradas) == 1
    assert paradas[0]["com_lacuna"] is True
    assert paradas[0]["duracao_min"] == 20
    assert paradas[0]["qtd_pulsos"] == 4


def test_ignicao_desligada_conta_como_parada():
    pontos = [
        _ponto(0, vel=40, ignicao=0),
        _ponto(20, vel=40, ignicao=0),
    ]
    paradas = detectar_paradas(pontos)
    assert len(paradas) == 1
    assert paradas[0]["duracao_min"] == 20
    assert paradas[0]["ignicao_desligada"] is True


def test_sem_velocidade_usa_haversine():
    parados = [_ponto(i, vel=None) for i in (0, 10, 20)]
    assert len(detectar_paradas(parados)) == 1
    andando = [_ponto(i, norte_m=i * 500, vel=None) for i in range(0, 21)]
    assert detectar_paradas(andando) == []


def test_salto_de_gps_no_km_e_ignorado():
    pontos = [
        _ponto(segundos=0, norte_m=0, vel=20),
        _ponto(segundos=10, norte_m=100, vel=20),
        _ponto(segundos=20, norte_m=3100, vel=20),
        _ponto(segundos=30, norte_m=3200, vel=20),
    ]
    pares = [(item["lat"], item["lon"]) for item in pontos]
    salto = haversine_m(pares[1], pares[2])
    assert salto > 2000
    esperado = (haversine_m(pares[0], pares[1]) + haversine_m(pares[2], pares[3])) / 1000
    com_salto = esperado + salto / 1000
    assert km_rodados(pontos) == pytest.approx(esperado, rel=1e-6)
    assert km_rodados(pontos) < com_salto - 1


def test_janela_acompanha_o_sabado():
    sabado = date(2026, 10, 24)
    quarta = date(2026, 10, 21)
    ini, fim = janela_do_turno(quarta, "diurno")
    assert (ini.hour, ini.minute, ini.second) == (6, 0, 0)
    assert (fim.hour, fim.minute, fim.second) == (18, 59, 59)
    ini, fim = janela_do_turno(sabado, "diurno")
    assert ini == datetime(2026, 10, 24, 5, 0, 0)
    assert fim == datetime(2026, 10, 24, 14, 59, 59)
    ini, fim = janela_do_turno(sabado, "noturno")
    assert ini == datetime(2026, 10, 24, 15, 0, 0)
    assert fim == datetime(2026, 10, 25, 3, 0, 59)
    ini, fim, dia, turno = resolver_periodo(data="2026-10-24", turno="noturno")
    assert (ini, fim, dia, turno) == (
        datetime(2026, 10, 24, 15, 0, 0),
        datetime(2026, 10, 25, 3, 0, 59),
        sabado,
        "noturno",
    )
    ini, fim, dia, turno = resolver_periodo(inicio="2026-10-25T01:00:00", fim="2026-10-25T01:30:00")
    assert dia == sabado
    assert turno == "noturno"
    assert ini == datetime(2026, 10, 25, 1, 0, 0)


def _fulltrack_falso(monkeypatch):
    chamadas: list[tuple[datetime, datetime]] = []
    acessos = {"login": 0}

    def login(settings):
        acessos["login"] += 1
        return "tok"

    def veiculos(settings, token=None):
        return [
            {"ras_vei_placa": "ABC1D23", "ras_vei_id": "9", "ras_vei_veiculo": "TESTE"},
            {"ras_vei_placa": "DEF2E34", "ras_vei_id": "10", "ras_vei_veiculo": "TESTE 2"},
        ]

    def eventos(settings, vehicle_id, inicio, fim, token=None):
        chamadas.append((inicio, fim))
        meio = inicio + (fim - inicio) / 2
        utc = meio + timedelta(hours=3)
        return [
            {
                "ras_eve_latitude": "-16.82",
                "ras_eve_longitude": "-49.25",
                "ras_eve_data_gps": utc.strftime("%Y-%m-%d %H:%M:%S"),
                "ras_eve_velocidade": "12",
                "ras_eve_ignicao": "1",
            }
        ]

    monkeypatch.setattr("inlog_agent.frota.load_settings", lambda: object())
    monkeypatch.setattr("inlog_agent.frota.login", login)
    monkeypatch.setattr("inlog_agent.frota.listar_veiculos", veiculos)
    monkeypatch.setattr("inlog_agent.frota.historico_veiculo", eventos)
    return chamadas, acessos


def test_historico_busca_so_o_trecho_que_falta(ambiente, monkeypatch):
    chamadas, acessos = _fulltrack_falso(monkeypatch)
    inicio = datetime(2026, 10, 21, 6, 0, 0)
    primeiro = historico("ABC1D23", inicio, datetime(2026, 10, 21, 10, 0, 0))
    assert len(chamadas) == 1
    assert acessos["login"] == 1
    assert len(primeiro) == 1
    assert primeiro[0]["ts"] == "2026-10-21T08:00:00"
    assert primeiro[0]["ignicao"] is True
    assert primeiro[0]["vel"] == 12
    segundo = historico("abc1d23", inicio, datetime(2026, 10, 21, 14, 0, 0))
    assert chamadas[-1] == (datetime(2026, 10, 21, 10, 0, 0), datetime(2026, 10, 21, 14, 0, 0))
    assert len(chamadas) == 2
    assert acessos["login"] == 2
    assert len(segundo) == 2
    historico("ABC-1D23", inicio, datetime(2026, 10, 21, 14, 0, 0))
    assert len(chamadas) == 2
    assert acessos["login"] == 2


def test_historico_divide_janela_acima_de_24h(ambiente, monkeypatch):
    chamadas, acessos = _fulltrack_falso(monkeypatch)
    inicio = datetime(2026, 10, 21, 6, 0, 0)
    fim = datetime(2026, 10, 22, 7, 0, 0)
    pontos = historico("DEF2E34", inicio, fim)
    assert chamadas == [
        (datetime(2026, 10, 21, 6, 0, 0), datetime(2026, 10, 22, 6, 0, 0)),
        (datetime(2026, 10, 22, 6, 0, 0), datetime(2026, 10, 22, 7, 0, 0)),
    ]
    assert len(pontos) == 2
    historico("DEF2E34", inicio, fim)
    assert len(chamadas) == 2
    assert acessos["login"] == 1
    historico("DEF2E34", datetime(2026, 10, 21, 8, 0, 0), datetime(2026, 10, 21, 9, 0, 0))
    assert len(chamadas) == 2


def test_geocode_falha_nao_quebra_e_nao_repete_o_mesmo_ponto(ambiente, monkeypatch):
    pedidos: list[str] = []

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"display_name":"Rua das Flores, Aparecida de Goiania"}'

    def urlopen(pedido, timeout=12):
        pedidos.append(pedido.get_header("User-agent") or "")
        if "lat=-16.7000" in pedido.full_url:
            raise urllib.error.URLError("sem rede")
        return Resposta()

    monkeypatch.setattr("inlog_agent.frota.urlopen", urlopen)
    assert endereco(-16.82111, -49.25111) == "Rua das Flores, Aparecida de Goiania"
    assert endereco(-16.82114, -49.25114) == "Rua das Flores, Aparecida de Goiania"
    assert pedidos == ["SEGTRACK-frota"]
    assert endereco(-16.7, -49.1) == ""
    assert endereco(None, None) == ""


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
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as exc:
        bruto = exc.read()
        try:
            payload = json.loads(bruto.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload, exc.headers


def _bruto(opener, url):
    pedido = urllib.request.Request(url)
    try:
        with opener.open(pedido, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def _cliente():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def _servidor():
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _entrar(base, login, senha):
    opener = _cliente()
    status, payload, _ = _chamar(opener, base + "/api/auth/login", {"login": login, "senha": senha}, method="POST")
    assert status == 200
    return opener, payload["csrf"]


def test_operador_recebe_403_na_frota(ambiente, monkeypatch):
    monkeypatch.setattr("inlog_agent.frota.ao_vivo", lambda qs=None: {"ok": True, "veiculos": []})
    monkeypatch.setattr("inlog_agent.frota.consultar_paradas", lambda qs: {"ok": True, "paradas": []})
    monkeypatch.setattr("inlog_agent.frota.consultar_telemetria", lambda qs: {"ok": True})
    monkeypatch.setattr("inlog_agent.frota.consultar_trajeto", lambda qs: {"ok": True, "pontos": []})
    monkeypatch.setattr(
        "inlog_agent.frota.paradas_csv",
        lambda qs: ("\ufeffplaca;inicio\r\n".encode("utf-8"), "paradas.csv"),
    )
    httpd = _servidor()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    rotas = [
        "/api/frota/ao-vivo",
        "/api/frota/paradas?data=2026-10-21&turno=diurno",
        "/api/frota/telemetria?placa=ABC1D23&data=2026-10-21&turno=diurno",
        "/api/frota/trajeto?placa=ABC1D23&data=2026-10-21&turno=diurno",
        "/api/frota/paradas.csv?data=2026-10-21&turno=diurno",
    ]
    try:
        anon = _cliente()
        status, payload, _ = _chamar(anon, base + rotas[0])
        assert status == 401
        criar_primeiro_admin("admin@frota", SENHA_ADMIN, "Administrador")
        admin, csrf_admin = _entrar(base, "admin@frota", SENHA_ADMIN)
        status, payload, _ = _chamar(
            admin,
            base + "/api/auth/usuarios",
            {"nome": "Operador", "login": "operador@frota", "senha": SENHA_OP, "perfil": "operador"},
            headers={"X-CSRF-Token": csrf_admin},
            method="POST",
        )
        assert status == 200
        operador, csrf_op = _entrar(base, "operador@frota", SENHA_OP)
        for rota in rotas:
            status, payload, _ = _chamar(operador, base + rota)
            assert status == 403
            assert payload["erro"] == "Somente administradores podem acessar a frota."
        status, payload, _ = _chamar(
            operador,
            base + "/api/frota/ao-vivo",
            {},
            headers={"X-CSRF-Token": csrf_op},
            method="POST",
        )
        assert status == 403
        assert payload["erro"] == "Somente administradores podem acessar a frota."
        status, payload, _ = _chamar(
            admin,
            base + "/api/frota/ao-vivo",
            {},
            headers={"X-CSRF-Token": csrf_admin},
            method="POST",
        )
        assert status == 405
        status, payload, _ = _chamar(admin, base + "/api/frota/ao-vivo")
        assert status == 200
        assert payload["ok"] is True
        status, conteudo, headers = _bruto(admin, base + rotas[-1])
        assert status == 200
        assert conteudo.startswith(b"\xef\xbb\xbf")
        assert b";" in conteudo
        assert "csv" in (headers.get("Content-Type") or "")
    finally:
        httpd.shutdown()
