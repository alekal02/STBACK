from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import ROOT, Settings, aplicar_periodo, load_settings
from .motor import carregar_resumo, executar, ultimo_resumo

FRONT = ROOT / "front"

JOB: dict[str, Any] = {
    "lock": threading.Lock(),
    "rodando": False,
    "mensagens": [],
    "erro": "",
    "data": "",
}


def _aplicar(settings: Settings, corpo: dict[str, Any]) -> Settings:
    from .config import Settings as Cls

    novo = aplicar_periodo(
        settings,
        data=corpo.get("data") or None,
        hora_inicio=corpo.get("hora_inicio") or None,
        hora_fim=corpo.get("hora_fim") or None,
        data_fim=corpo.get("data_fim") or None,
    )
    return Cls(**{**settings.__dict__, **novo.__dict__})


def _pct_da_mensagem(msg: str) -> int | None:
    pulsos = re.search(r"Pulsos (\d+)/(\d+)", msg)
    if pulsos:
        return 8 + int(32 * int(pulsos.group(1)) / max(int(pulsos.group(2)), 1))
    mapa = re.search(r"Mapa oficial (\d+)/(\d+)", msg)
    if mapa:
        return 42 + int(34 * int(mapa.group(1)) / max(int(mapa.group(2)), 1))
    if "Medindo paradas" in msg:
        return 78
    if msg.startswith("Pronto") or "continua valendo" in msg:
        return 82
    if "Cruzando ruas" in msg or "Lendo o mapa" in msg:
        return 40
    if msg.startswith("Horário congelado") or msg.startswith("Segtrack em"):
        return 6
    return None


def _percentual(mensagens: list[str]) -> int:
    pct = 3
    for msg in mensagens:
        achado = _pct_da_mensagem(msg)
        if achado is not None:
            pct = max(pct, achado)
    return min(pct, 99)


def _andamento_arquivo(pasta) -> tuple[int, str] | None:
    arquivo = pasta / "andamento.json"
    if not arquivo.is_file():
        return None
    try:
        bruto = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(bruto, dict):
        return None
    return int(bruto.get("pct") or 0), str(bruto.get("texto") or "")


def _rodar(settings: Settings, com_pdf: bool = True) -> None:
    def progresso(msg: str) -> None:
        with JOB["lock"]:
            JOB["mensagens"].append(msg)
            del JOB["mensagens"][:-40]

    try:
        resumo = executar(settings, on_progress=progresso)
        from .whatsapp import avisar_coleta_pronta, preparar_pdf_frota

        if isinstance(resumo, dict) and resumo.get("falha_historico"):
            avisar_coleta_pronta(
                settings,
                falha="A Segtrack não devolveu o histórico dos caminhões. Peça de novo em alguns minutos.",
            )
        else:
            avisar_coleta_pronta(settings)
            if com_pdf:
                preparar_pdf_frota(settings, settings.data_rota, on_progress=progresso)
    except Exception as exc:
        with JOB["lock"]:
            JOB["erro"] = str(exc)
            JOB["mensagens"].append(f"Falhou: {exc}")
        from .whatsapp import avisar_coleta_pronta

        avisar_coleta_pronta(settings, falha=str(exc))
    finally:
        with JOB["lock"]:
            JOB["rodando"] = False


def _rota_vista(caminho: str) -> tuple[str, str] | None:
    import re

    pagina = re.fullmatch(r"/r/([A-Za-z0-9_-]{8,}\.[A-Za-z0-9]{16,})", caminho)
    if pagina:
        return pagina.group(1), "pagina"
    api = re.fullmatch(r"/api/r/([A-Za-z0-9_-]{8,}\.[A-Za-z0-9]{16,})(?:/(mapa|pdf))?", caminho)
    if api:
        return api.group(1), api.group(2) or "json"
    return None


def _origem_permitida(origem: str) -> bool:
    normal = origem.rstrip("/")
    liberadas = {
        item.strip().rstrip("/")
        for item in os.getenv("CORS_ORIGIN", "").split(",")
        if item.strip()
    }
    if normal in liberadas:
        return True
    host = urlparse(normal).hostname or ""
    if host in {"localhost", "127.0.0.1"}:
        return True
    previews = os.getenv("ALLOW_VERCEL_PREVIEWS", "true").strip().lower()
    return previews in {"1", "true", "yes", "sim"} and host.endswith(".vercel.app")


class Handler(BaseHTTPRequestHandler):
    settings: Settings

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _cors(self) -> None:
        origem = (self.headers.get("Origin") or "").strip()
        if not origem or not _origem_permitida(origem):
            return
        self.send_header("Access-Control-Allow-Origin", origem.rstrip("/"))
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, codigo: int, corpo: Any) -> None:
        raw = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _bytes(self, codigo: int, dados: bytes, tipo: str, nome: str = "") -> None:
        self.send_response(codigo)
        self._cors()
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "no-store")
        if nome:
            self.send_header("Content-Disposition", f'attachment; filename="{nome}"')
        self.end_headers()
        self.wfile.write(dados)

    def _vista(self, destino: tuple[str, str], url: Any) -> None:
        from .vista import abrir, dados, html_pagina, mapa, pdf

        token, modo = destino
        if modo == "pagina":
            self._bytes(200, html_pagina().encode("utf-8"), "text/html; charset=utf-8")
            return
        claims = abrir(token)
        if claims is None:
            self._json(410, {"erro": "Este link expirou. Peça o relatório de novo no grupo."})
            return
        if modo == "json":
            self._json(200, dados(self.settings, claims))
            return
        if modo == "mapa":
            codigo = (parse_qs(url.query).get("codigo") or [""])[0]
            self._json(200, mapa(self.settings, claims, codigo))
            return
        try:
            codigo, arquivo, nome = pdf(self.settings, claims)
        except Exception as exc:
            self._json(500, {"erro": str(exc)})
            return
        if codigo == 202:
            self._json(202, {"estado": "montando"})
            return
        self._bytes(200, arquivo, "application/pdf", nome)

    def _arquivo(self, caminho: Path) -> None:
        if not caminho.is_file():
            self.send_error(404)
            return
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        dados = caminho.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _corpo(self) -> dict[str, Any]:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho <= 0:
            return {}
        bruto = self.rfile.read(tamanho)
        if not bruto:
            return {}
        dado = json.loads(bruto.decode("utf-8"))
        return dado if isinstance(dado, dict) else {}

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/health":
            self._json(200, {"status": "ok", "servico": "segtrack"})
            return
        vista = _rota_vista(url.path)
        if vista is not None:
            self._vista(vista, url)
            return
        if url.path == "/config.js":
            self._arquivo(FRONT / "config.js")
            return
        if url.path in {"/", "/index.html"}:
            self._arquivo(FRONT / "index.html")
            return
        if url.path in {"/whatsapp", "/whatsapp.html"}:
            self._arquivo(FRONT / "whatsapp.html")
            return
        if url.path == "/api/whatsapp/status":
            from .whatsapp import status

            self._json(200, status(self.settings))
            return
        if url.path == "/api/whatsapp/qr":
            from .whatsapp import qr

            self._json(200, qr(self.settings))
            return
        if url.path == "/api/whatsapp/grupos":
            from .whatsapp import grupos

            try:
                self._json(200, {"grupos": grupos(self.settings)})
            except Exception as exc:
                self._json(200, {"grupos": [], "erro": str(exc)})
            return
        if url.path == "/api/whatsapp/configuracao":
            from .whatsapp import grupo_configurado, grupo_nome, permitidos

            self._json(
                200,
                {
                    "group_jid": grupo_configurado(self.settings),
                    "group_nome": grupo_nome(),
                    "travado": bool(grupo_configurado(self.settings)),
                    "allowed_jids": permitidos(self.settings),
                },
            )
            return
        if url.path == "/api/progresso":
            with JOB["lock"]:
                mensagens = list(JOB["mensagens"])
                rodando = bool(JOB["rodando"])
                desde = float(JOB.get("desde") or 0)
                erro = JOB["erro"]
                data = JOB["data"]
            pct = _percentual(mensagens) if rodando else 0
            texto = mensagens[-1] if mensagens else ""
            arquivo = self.settings.output_dir / "andamento.json"
            if rodando and arquivo.is_file() and arquivo.stat().st_mtime >= desde - 1:
                extra = _andamento_arquivo(self.settings.output_dir)
                if extra and extra[0] >= pct:
                    pct, texto = extra
            self._json(
                200,
                {
                    "rodando": rodando,
                    "erro": erro,
                    "data": data,
                    "pct": pct if rodando else 0,
                    "texto": texto if rodando else "",
                    "mensagens": mensagens,
                },
            )
            return
        if url.path == "/api/ultimo":
            self._json(200, ultimo_resumo(self.settings.output_dir) or {})
            return
        if url.path == "/api/relatorio":
            qs = parse_qs(url.query)
            bruto = (qs.get("data") or [""])[0]
            try:
                dia = date.fromisoformat(bruto) if bruto else self.settings.data_rota
            except ValueError:
                self._json(400, {"erro": "Data inválida"})
                return
            from .relatorios import montar

            tipo = (qs.get("tipo") or ["frota"])[0]
            consulta = (qs.get("q") or [""])[0]
            self._json(200, montar(self.settings.output_dir, dia, tipo, consulta))
            return
        if url.path == "/api/locais":
            arquivo = ROOT / "data" / "locais.json"
            if not arquivo.is_file():
                self._json(200, {"locais": []})
                return
            self._json(200, json.loads(arquivo.read_text(encoding="utf-8")))
            return
        if url.path == "/api/dia":
            qs = parse_qs(url.query)
            bruto = (qs.get("data") or [""])[0]
            try:
                dia = date.fromisoformat(bruto)
            except ValueError:
                self._json(400, {"erro": "Data inválida"})
                return
            achado = carregar_resumo(self.settings.output_dir, dia)
            self._json(200, achado or {})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/webhooks/whatsapp":
            try:
                corpo = self._corpo()
            except json.JSONDecodeError:
                self._json(400, {"erro": "JSON inválido"})
                return
            from .whatsapp import agendar

            agendar(corpo)
            self._json(200, {"ok": True})
            return
        if url.path == "/api/whatsapp/conectar":
            from .whatsapp import conectar

            self._json(200, conectar(self.settings))
            return
        if url.path == "/api/whatsapp/desconectar":
            from .whatsapp import desconectar

            self._json(200, desconectar(self.settings))
            return
        if url.path == "/api/whatsapp/configuracao":
            try:
                corpo = self._corpo()
            except json.JSONDecodeError:
                self._json(400, {"erro": "JSON inválido"})
                return
            from .whatsapp import salvar_configuracao

            salvo = salvar_configuracao(
                str(corpo.get("group_jid") or ""),
                corpo.get("allowed_jids") if isinstance(corpo.get("allowed_jids"), list) else None,
                str(corpo.get("group_nome") or "") or None,
            )
            self._json(200, salvo)
            return
        if url.path != "/api/coletar":
            self.send_error(404)
            return
        try:
            corpo = self._corpo()
        except json.JSONDecodeError:
            self._json(400, {"erro": "JSON inválido"})
            return
        with JOB["lock"]:
            if JOB["rodando"]:
                self._json(409, {"erro": "Já existe uma coleta em andamento."})
                return
            settings = _aplicar(self.settings, corpo)
            JOB["rodando"] = True
            JOB["desde"] = time.time()
            JOB["erro"] = ""
            JOB["data"] = settings.data_rota.isoformat()
            JOB["mensagens"] = [f"Iniciando {JOB['data']}"]
        threading.Thread(target=_rodar, args=(settings,), daemon=True).start()
        self._json(202, {"ok": True, "data": settings.data_rota.isoformat()})


def servir(porta: int | None = None, host: str = "0.0.0.0") -> None:
    settings = load_settings()
    Handler.settings = settings
    porta = porta or settings.porta

    def preparar_whatsapp() -> None:
        from .whatsapp import manter, relogio_pdf

        threading.Thread(target=relogio_pdf, args=(settings,), daemon=True).start()
        from .whatsapp import relogio_frota

        threading.Thread(target=relogio_frota, args=(settings,), daemon=True).start()
        manter()

    threading.Thread(target=preparar_whatsapp, daemon=True).start()
    servidor = ThreadingHTTPServer((host, porta), Handler)
    print(f"Sala técnica Segtrack em http://127.0.0.1:{porta}", flush=True)
    print(f"Configuração do grupo em http://127.0.0.1:{porta}/whatsapp", flush=True)
    servidor.serve_forever()
