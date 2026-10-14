"""Login, sessão, perfis e a garantia de que o SQLite do GPS não é alterado."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from inlog_agent.acesso import (
    IDLE,
    RECENTE,
    cabecalho_sessao,
    criar_primeiro_admin,
    entrar,
)
from inlog_agent.server import Handler, SalaHTTPServer


SENHA_ADMIN = "Admin-Rota-Vivo-1"
SENHA_OP = "Operador-Rota-2"


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("AUTH_ENFORCE", raising=False)
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    banco = tmp_path / "acesso.db"
    live = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(live))
    return {"auth": banco, "live": live}


def _chamar(opener, url, dados=None, headers=None, method=None):
    corpo = None if dados is None else json.dumps(dados).encode("utf-8")
    pedido = urllib.request.Request(
        url,
        data=corpo,
        method=method or ("POST" if corpo is not None else "GET"),
    )
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


def _cliente():
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _servidor():
    httpd = SalaHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def _token(jar: CookieJar) -> str:
    for cookie in jar:
        if cookie.name == "rav_sessao":
            return cookie.value
    return ""


def test_cookie_de_producao_e_httponly_secure():
    direto = cabecalho_sessao("abc", seguro=True, max_age=60)
    assert "HttpOnly" in direto
    assert "Secure" in direto
    assert "SameSite=None" in direto
    assert "Partitioned" in direto
    site = cabecalho_sessao("abc", seguro=True, max_age=60, primeira_parte=True)
    assert "HttpOnly" in site
    assert "Secure" in site
    assert "SameSite=Lax" in site
    assert "Partitioned" not in site
    local = cabecalho_sessao("abc", seguro=False, max_age=60)
    assert "SameSite=Lax" in local
    assert "Secure" not in local


def test_banco_acesso_fica_no_disco_do_render(tmp_path, monkeypatch):
    disco = tmp_path / "sagtrack"
    disco.mkdir()
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("SAGTRACK_AUTH_DB", raising=False)
    monkeypatch.setattr("inlog_agent.acesso.DISCO", disco)
    from inlog_agent.acesso import NOME_BANCO, caminho_banco

    destino = caminho_banco()
    assert destino == disco / NOME_BANCO
    assert destino.name != "sagtrack_live.db"
    monkeypatch.setenv("SAGTRACK_AUTH_DB", str(tmp_path / "fora.db"))
    with pytest.raises(RuntimeError):
        caminho_banco()


def test_migracao_aditiva_nao_apaga_usuario(ambiente):
    banco = ambiente["auth"]
    con = sqlite3.connect(banco)
    con.execute(
        """
        CREATE TABLE usuario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            login TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        INSERT INTO usuario (nome, login, senha_hash, perfil, ativo, criado_em, atualizado_em)
        VALUES ('Antigo', 'antigo@rota', 'scrypt$1$8$1$aa$bb', 'operador', 1, '', '')
        """
    )
    con.commit()
    con.close()

    from inlog_agent.acesso import _conectar

    con = _conectar()
    try:
        colunas = {row[1] for row in con.execute("PRAGMA table_info(usuario)")}
        assert "ultimo_login_em" in colunas
        assert "ultimo_acesso_em" in colunas
        row = con.execute("SELECT nome, login FROM usuario").fetchone()
        assert row["nome"] == "Antigo"
        assert row["login"] == "antigo@rota"
        tabelas = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "sessao" in tabelas
        assert "acesso_evento" in tabelas
        assert "ponto" not in tabelas
    finally:
        con.close()


def test_sqlite_historico_permanece_intacto(ambiente):
    from inlog_agent import live_db
    from inlog_agent.acesso import criar_primeiro_admin as criar

    criar("admin@rota", SENHA_ADMIN, "Admin")
    live_db.conectar().close()
    historico = sqlite3.connect(ambiente["live"])
    acesso = sqlite3.connect(ambiente["auth"])
    try:
        tabelas_live = {row[0] for row in historico.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        tabelas_acesso = {row[0] for row in acesso.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        historico.close()
        acesso.close()
    assert "ponto" in tabelas_live
    assert "turno" in tabelas_live
    assert "usuario" not in tabelas_live
    assert "sessao" not in tabelas_live
    assert "usuario" in tabelas_acesso
    assert "ponto" not in tabelas_acesso
    assert ambiente["auth"] != ambiente["live"]


def test_sincronizador_continua_importavel():
    from inlog_agent.live_sync import ocupada

    assert ocupada() in {True, False}


def test_fluxo_http_de_acesso(ambiente):
    httpd = _servidor()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon, _ = _cliente()
        status, payload, _ = _chamar(anon, base + "/api/health")
        assert status == 200 and payload["ok"] is True

        status, payload, _ = _chamar(anon, base + "/api/auth/sessao")
        assert status == 200 and payload["exigida"] is False and payload["usuario"] is None

        status, payload, _ = _chamar(anon, base + "/api/parar", {}, method="POST")
        assert status == 200

        status, payload, _ = _chamar(anon, base + "/api/auth/usuarios")
        assert status == 401

        status, payload, _ = _chamar(
            anon,
            base + "/api/chatbot/gerar",
            {},
            headers={"X-Chatbot-Secret": "nao"},
            method="POST",
        )
        assert status == 401
        assert payload["erro"] == "Token da API inválido."

        criar_primeiro_admin("admin@rota", SENHA_ADMIN, "Administrador")

        status, payload, _ = _chamar(anon, base + "/api/auth/sessao")
        assert status == 401 and payload["exigida"] is True

        status, payload, _ = _chamar(anon, base + "/api/live-report")
        assert status == 401
        assert "senha" not in json.dumps(payload).lower()

        status, payload, _ = _chamar(
            anon,
            base + "/api/auth/login",
            {"login": "admin@rota", "senha": "errada-demais"},
            method="POST",
        )
        assert status == 401
        assert payload["erro"] == "Usuário ou senha incorretos."

        status, _, headers = _chamar(
            anon,
            base + "/api/chatbot/gerar",
            {},
            headers={"X-Chatbot-Secret": "nao"},
            method="POST",
        )
        assert status == 401
        assert _["erro"] == "Token da API inválido."

        admin, jar = _cliente()
        status, payload, headers = _chamar(
            admin,
            base + "/api/auth/login",
            {"login": "Admin@Rota", "senha": SENHA_ADMIN},
            method="POST",
        )
        assert status == 200
        assert payload["usuario"]["perfil"] == "admin"
        assert "senha_hash" not in payload["usuario"]
        cookie = headers.get("Set-Cookie") or ""
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert SENHA_ADMIN not in cookie
        csrf = payload["csrf"]
        token = _token(jar)
        assert token and token.encode() not in ambiente["auth"].read_bytes()

        status, payload, _ = _chamar(admin, base + "/api/parar", {}, method="POST")
        assert status == 403

        status, payload, _ = _chamar(
            admin,
            base + "/api/parar",
            {},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200

        status, payload, _ = _chamar(
            admin,
            base + "/api/auth/usuarios",
            {
                "nome": "Operador Um",
                "login": "operador@rota",
                "senha": SENHA_OP,
                "perfil": "operador",
            },
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        operador_id = payload["usuario"]["id"]
        assert payload["usuario"]["presenca"] == "sem_sessao"
        assert payload["usuario"]["sessoes_ativas"] == 0

        op, jar_op = _cliente()
        status, op_payload, _ = _chamar(
            op,
            base + "/api/auth/login",
            {"login": "operador@rota", "senha": SENHA_OP},
            method="POST",
        )
        assert status == 200
        assert op_payload["usuario"]["perfil"] == "operador"
        op_csrf = op_payload["csrf"]

        status, payload, _ = _chamar(op, base + "/api/auth/usuarios")
        assert status == 403
        assert payload["erro"] == "Somente administradores podem gerenciar usuários."

        status, lista, _ = _chamar(admin, base + "/api/auth/usuarios")
        assert status == 200
        achado = next(item for item in lista["usuarios"] if item["id"] == operador_id)
        assert achado["atividade_recente"] is True
        assert achado["presenca"] == "ativo_agora"
        assert achado["sessoes_ativas"] == 1

        from inlog_agent.acesso import _iso, _agora

        con = sqlite3.connect(ambiente["auth"])
        con.execute(
            "UPDATE sessao SET ultima_atividade_em = ? WHERE usuario_id = ?",
            (_iso(_agora() - RECENTE - timedelta(minutes=10)), operador_id),
        )
        con.commit()
        con.close()
        status, lista, _ = _chamar(admin, base + "/api/auth/usuarios")
        achado = next(item for item in lista["usuarios"] if item["id"] == operador_id)
        assert achado["sessoes_ativas"] == 1
        assert achado["atividade_recente"] is False
        assert achado["presenca"] == "sessao_aberta"

        con = sqlite3.connect(ambiente["auth"])
        con.execute(
            "UPDATE sessao SET ultima_atividade_em = ? WHERE usuario_id = ?",
            (_iso(_agora() - IDLE - timedelta(minutes=5)), operador_id),
        )
        con.commit()
        con.close()
        status, payload, _ = _chamar(op, base + "/api/auth/sessao")
        assert status == 401
        assert "expirada" in payload["erro"].lower() or "entre novamente" in payload["erro"].lower()

        op2, _ = _cliente()
        status, op_payload, _ = _chamar(
            op2,
            base + "/api/auth/login",
            {"login": "operador@rota", "senha": SENHA_OP},
            method="POST",
        )
        assert status == 200
        op_csrf = op_payload["csrf"]

        status, _, _ = _chamar(
            admin,
            base + "/api/auth/usuarios/bloquear",
            {"id": operador_id},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        status, payload, _ = _chamar(
            op2,
            base + "/api/auth/login",
            {"login": "operador@rota", "senha": SENHA_OP},
            method="POST",
        )
        assert status == 403
        assert "bloqueado" in payload["erro"].lower()

        status, _, _ = _chamar(
            admin,
            base + "/api/auth/usuarios/ativar",
            {"id": operador_id},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200

        status, _, _ = _chamar(
            admin,
            base + "/api/auth/logout",
            {},
            headers={"X-CSRF-Token": csrf},
            method="POST",
        )
        assert status == 200
        status, payload, _ = _chamar(admin, base + "/api/auth/usuarios")
        assert status == 401

        status, auditoria, _ = _chamar(
            _entrar(base, "admin@rota", SENHA_ADMIN)[0],
            base + "/api/auth/auditoria",
        )
        texto = json.dumps(auditoria, ensure_ascii=False)
        assert SENHA_ADMIN not in texto
        assert SENHA_OP not in texto
        assert token not in texto
        tipos = {item["tipo"] for item in auditoria["eventos"]}
        assert "login_ok" in tipos
        assert "login_falha" in tipos
        assert "logout" in tipos
        assert "login_bloqueado" in tipos
        assert "acesso_negado" in tipos
        for parte in ambiente["auth"].parent.glob("acesso.db*"):
            bruto = parte.read_bytes()
            assert SENHA_ADMIN.encode() not in bruto
            assert SENHA_OP.encode() not in bruto
            assert token.encode() not in bruto
    finally:
        httpd.shutdown()
        httpd.server_close()


def _entrar(base, login, senha):
    opener, jar = _cliente()
    status, payload, _ = _chamar(
        opener,
        base + "/api/auth/login",
        {"login": login, "senha": senha},
        method="POST",
    )
    assert status == 200
    return opener, jar, payload


def test_limite_de_tentativas(ambiente):
    criar_primeiro_admin("admin@rota", SENHA_ADMIN, "Administrador")
    httpd = _servidor()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon, _ = _cliente()
        for _ in range(8):
            status, payload, _ = _chamar(
                anon,
                base + "/api/auth/login",
                {"login": "admin@rota", "senha": "tentativa-invalida"},
                method="POST",
            )
            assert status == 401
        status, payload, _ = _chamar(
            anon,
            base + "/api/auth/login",
            {"login": "admin@rota", "senha": SENHA_ADMIN},
            method="POST",
        )
        assert status == 429
        assert "15 minutos" in payload["erro"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_auth_enforce_zero_nao_fecha_a_api(ambiente, monkeypatch):
    monkeypatch.setenv("AUTH_ENFORCE", "0")
    criar_primeiro_admin("admin@rota", SENHA_ADMIN, "Administrador")
    httpd = _servidor()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        anon, _ = _cliente()
        status, payload, _ = _chamar(anon, base + "/api/auth/sessao")
        assert status == 200 and payload["exigida"] is False
        status, _, _ = _chamar(anon, base + "/api/parar", {}, method="POST")
        assert status == 200
        status, _, _ = _chamar(anon, base + "/api/auth/usuarios")
        assert status == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_cria_so_o_primeiro(ambiente, monkeypatch, capsys):
    from inlog_agent.cli import main

    monkeypatch.setenv("AUTH_ADMIN_USUARIO", "chefe@rota")
    monkeypatch.setenv("AUTH_ADMIN_SENHA", SENHA_ADMIN)
    monkeypatch.setenv("AUTH_ADMIN_NOME", "Chefe")
    main(["criar-admin"])
    saida = capsys.readouterr().out
    assert "chefe@rota" in saida
    assert SENHA_ADMIN not in saida
    with pytest.raises(SystemExit):
        main(["criar-admin"])
    from inlog_agent.acesso import entrar as autenticar

    usuario = autenticar("chefe@rota", SENHA_ADMIN)
    assert usuario["usuario"]["perfil"] == "admin"
    assert usuario["token"] not in saida
