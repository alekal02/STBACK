"""PDF das paradas. Não chama a Fulltrack."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar

from pypdf import PdfReader

from inlog_agent.acesso import criar_primeiro_admin
from inlog_agent.frota_pdf import _duracao, gerar_paradas_pdf
from inlog_agent.fulltrack_api import _norm_placa
from inlog_agent.server import Handler, SalaHTTPServer


SENHA = "Admin-Rota-Vivo-1"
BASE = datetime(2026, 10, 21, 8, 0, 0)
VEICULOS = [
    {"ras_vei_placa": "ABC1D23", "ras_vei_id": "1", "ras_vei_veiculo": "Caminhão Azul"},
    {"ras_vei_placa": "DEF2E34", "ras_vei_id": "2", "ras_vei_veiculo": "Caminhão Verde"},
]


def _ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "acesso.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(tmp_path / "live.db"))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent import frota

    frota._CACHE.update(chave="", corpo=None, expira=0.0)
    return frota


def _ponto(quando, placa, lat, lon, vel, ignicao=None):
    return {
        "placa": placa,
        "ts": quando.strftime("%Y-%m-%dT%H:%M:%S"),
        "lat": lat,
        "lon": lon,
        "vel": vel,
        "ignicao": ignicao,
    }


def _serie(placa):
    if _norm_placa(placa) == "DEF2E34":
        return [
            _ponto(datetime(2026, 10, 21, 10, 0), placa, -16.80, -49.25, 40),
            _ponto(datetime(2026, 10, 21, 10, 5), placa, -16.80, -49.24, 40),
        ]
    pontos = []
    cursor = BASE
    while cursor <= BASE + timedelta(minutes=20):
        pontos.append(_ponto(cursor, placa, -16.90, -49.25, 0))
        cursor += timedelta(minutes=4)
    pontos.append(_ponto(BASE + timedelta(minutes=25), placa, -16.80, -49.24, 40))
    pontos.append(_ponto(datetime(2026, 10, 21, 9, 0), placa, -16.90, -49.25, 0, False))
    pontos.append(_ponto(datetime(2026, 10, 21, 9, 16), placa, -16.90, -49.25, 0, False))
    return pontos


def _preparar(monkeypatch, frota):
    def historico(placa, inicio, fim, **kwargs):
        return [dict(item) for item in _serie(placa)]

    def classificar(lat, lon, dia, turno, cache):
        dentro = float(lat) > -16.85
        return dentro, "Rota 1" if dentro else None

    monkeypatch.setattr(frota, "load_settings", lambda: object())
    monkeypatch.setattr(frota, "login", lambda settings: "tok")
    monkeypatch.setattr(frota, "listar_veiculos", lambda settings, token=None: list(VEICULOS))
    monkeypatch.setattr(frota, "_eventos_atuais", lambda settings, token: [])
    monkeypatch.setattr(frota, "historico", historico)
    monkeypatch.setattr(frota, "endereco", lambda lat, lon: "Rua das Flores, 100")
    monkeypatch.setattr(frota, "_classificar_ponto", classificar)


def _qs(**itens):
    base = {"data": ["2026-10-21"], "turno": ["diurno"], "operacao": ["todas"], "min": ["5"], "max": ["60"]}
    base.update(itens)
    return base


def _ler(pdf: bytes):
    reader = PdfReader(io.BytesIO(pdf))
    texto = "\n".join(page.extract_text() or "" for page in reader.pages)
    links = []
    for page in reader.pages:
        for anotacao in page.annotations or []:
            obj = anotacao.get_object()
            acao = obj.get("/A")
            if acao and acao.get("/URI"):
                links.append(str(acao["/URI"]))
    return texto, links, len(reader.pages)


def test_duracao_curta_e_com_hora():
    assert _duracao(23) == "23 min"
    assert _duracao(65) == "1h05"
    assert _duracao(60) == "1h00"


def test_pdf_de_um_caminhao_com_paradas(tmp_path, monkeypatch):
    frota = _ambiente(tmp_path, monkeypatch)
    _preparar(monkeypatch, frota)
    pdf, nome = gerar_paradas_pdf(_qs(placa=["ABC1D23"]))
    texto, links, paginas = _ler(pdf)
    assert nome == "paradas-ABC-1D23-2026-10-21-diurno.pdf"
    assert paginas >= 1
    assert "ABC-1D23" in texto
    assert "Caminhão Azul" in texto
    assert "21/10/2026" in texto
    assert "Diurno" in texto
    assert "06:00" in texto and "18:59" in texto
    assert "5 a 60 min" in texto
    assert "Total de paradas: 2" in texto
    assert "Rua das Flores, 100" in texto
    assert "fora" in texto
    assert "desligada" in texto
    assert "sinal" in texto
    assert "10 min" in texto
    assert any(link.startswith("https://www.google.com/maps?q=-16.9") for link in links)
    assert "Página 1 de" in texto
    assert "Condução" in texto
    assert "valores estimados pelo GPS" in texto


def test_pdf_de_um_caminhao_sem_paradas(tmp_path, monkeypatch):
    frota = _ambiente(tmp_path, monkeypatch)
    _preparar(monkeypatch, frota)
    pdf, nome = gerar_paradas_pdf(_qs(placa=["DEF2E34"]))
    texto, links, _paginas = _ler(pdf)
    assert nome == "paradas-DEF-2E34-2026-10-21-diurno.pdf"
    assert "DEF-2E34" in texto
    assert "Nenhuma parada entre 5 e 60 min neste turno." in texto
    assert "Total de paradas: 0" in texto
    assert links == []


def test_pdf_da_frota_com_dois_caminhoes(tmp_path, monkeypatch):
    frota = _ambiente(tmp_path, monkeypatch)
    _preparar(monkeypatch, frota)
    pdf, nome = gerar_paradas_pdf(_qs())
    texto, links, paginas = _ler(pdf)
    assert nome == "paradas-Todas-2026-10-21-diurno.pdf"
    assert paginas >= 2
    assert "ABC-1D23" in texto
    assert "DEF-2E34" in texto
    assert "Total de paradas: 2" in texto
    assert "Caminhões: 2" in texto
    assert "Nenhuma parada entre 5 e 60 min neste turno." in texto
    assert any("google.com/maps?q=" in link for link in links)


def _chamar(opener, url, dados=None, headers=None, method=None):
    corpo = None if dados is None else json.dumps(dados).encode("utf-8")
    pedido = urllib.request.Request(url, data=corpo, method=method or ("POST" if corpo is not None else "GET"))
    pedido.add_header("Content-Type", "application/json")
    for chave, valor in (headers or {}).items():
        pedido.add_header(chave, valor)
    try:
        with opener.open(pedido, timeout=30) as resp:
            bruto = resp.read()
            if "application/pdf" in (resp.headers.get("Content-Type") or ""):
                return resp.status, bruto, resp.headers
            payload = json.loads(bruto.decode("utf-8") or "{}") if bruto else {}
            return resp.status, payload, resp.headers
    except urllib.error.HTTPError as exc:
        bruto = exc.read()
        try:
            payload = json.loads(bruto.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = bruto
        return exc.code, payload, exc.headers


def test_rotas_do_pdf_exigem_admin_e_job_da_operacao(tmp_path, monkeypatch):
    frota = _ambiente(tmp_path, monkeypatch)
    _preparar(monkeypatch, frota)
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _payload, _headers = _chamar(anon, base + "/api/frota/paradas.pdf?data=2026-10-21&turno=diurno&operacao=todas")
        assert status == 401
        criar_primeiro_admin("admin@pdf", SENHA, "Administrador")
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        status, sessao, _headers = _chamar(admin, base + "/api/auth/login", {"login": "admin@pdf", "senha": SENHA}, method="POST")
        assert status == 200
        status, pdf, headers = _chamar(
            admin,
            base + "/api/frota/paradas.pdf?placa=ABC1D23&data=2026-10-21&turno=diurno&operacao=todas",
        )
        assert status == 200
        assert pdf[:4] == b"%PDF"
        assert "paradas-ABC-1D23-2026-10-21-diurno.pdf" in headers.get("Content-Disposition", "")
        status, job, _headers = _chamar(
            admin,
            base + "/api/frota/paradas.pdf?data=2026-10-21&turno=diurno&operacao=todas",
        )
        assert status == 202
        job_id = job["job_id"]
        pronto = False
        for _ in range(50):
            status, andamento, _headers = _chamar(admin, base + f"/api/frota/paradas.pdf/andamento?job_id={job_id}")
            assert status == 200
            if andamento.get("erro"):
                raise AssertionError(andamento["erro"])
            if andamento.get("pronto"):
                pronto = True
                break
            time.sleep(0.05)
        assert pronto
        status, baixado, headers = _chamar(admin, base + f"/api/frota/paradas.pdf/baixar?job_id={job_id}")
        assert status == 200
        assert baixado[:4] == b"%PDF"
        texto, _links, _paginas = _ler(baixado)
        assert "ABC-1D23" in texto and "DEF-2E34" in texto
        assert "paradas-Todas-2026-10-21-diurno.pdf" in headers.get("Content-Disposition", "")
    finally:
        httpd.shutdown()
