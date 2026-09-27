"""Comandos do grupo de WhatsApp, na mesma Wuzapi usada pelo Medzup."""

from __future__ import annotations

import json
import re
import threading
import unicodedata
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings, dia_operacional, load_settings

_VISTOS: set[str] = set()
_LOCK = threading.Lock()
def _caminho_config() -> Path:
    from .config import pasta_dados

    return pasta_dados() / "whatsapp.json"


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFD", texto or "")
    sem = "".join(c for c in base if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sem).strip().upper()


def comando_grupo(texto: str) -> tuple[str, str] | None:
    cmd = _norm(texto)
    if cmd in {"MENU", "AJUDA", "COMANDOS", "OI", "OLA", "INICIO"}:
        return "menu", ""
    if cmd in {"FROTA", "RELATORIO", "RELATORIO FROTA", "RELATORIO DA FROTA"}:
        return "frota", ""
    rota = re.fullmatch(r"(?:RELATORIO\s+)?ROTA\s+(.+)", cmd)
    if rota:
        return "rota", rota.group(1).strip()
    placa = re.fullmatch(r"(?:RELATORIO\s+)?(?:PLACA|CAMINHAO)\s+(.+)", cmd)
    if placa:
        return "placa", placa.group(1).strip()
    if re.fullmatch(r"\d{2}[A-Z]{2}", cmd):
        return "rota", cmd
    if re.fullmatch(r"[A-Z]{3}-?\d[A-Z0-9]\d{2}", cmd):
        return "placa", cmd
    return None


def _config_arquivo() -> dict[str, Any]:
    if not _caminho_config().is_file():
        return {}
    try:
        dado = json.loads(_caminho_config().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dado if isinstance(dado, dict) else {}


def grupo_configurado(settings: Settings | None = None) -> str:
    settings = settings or load_settings()
    return str(_config_arquivo().get("group_jid") or settings.whatsapp_group_jid or "").strip()


def permitidos(settings: Settings | None = None) -> list[str]:
    settings = settings or load_settings()
    arquivo = _config_arquivo().get("allowed_jids")
    if isinstance(arquivo, list) and arquivo:
        return [str(item).strip() for item in arquivo if str(item).strip()]
    return list(settings.whatsapp_allowed_jids)


def grupo_nome() -> str:
    return str(_config_arquivo().get("group_nome") or "").strip()


def salvar_configuracao(
    group_jid: str,
    allowed_jids: list[str] | None = None,
    group_nome: str | None = None,
) -> dict[str, Any]:
    atual = _config_arquivo()
    atual["group_jid"] = group_jid.strip()
    atual["travado"] = bool(atual["group_jid"])
    if group_nome is not None:
        atual["group_nome"] = group_nome.strip()
    if allowed_jids is not None:
        atual["allowed_jids"] = [item.strip() for item in allowed_jids if item.strip()]
    _caminho_config().parent.mkdir(parents=True, exist_ok=True)
    _caminho_config().write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")
    return atual


def _url(settings: Settings, caminho: str) -> str:
    return settings.wuzapi_url.rstrip("/") + caminho


def _json(pedido: urllib.request.Request, timeout: int = 20) -> Any:
    with urllib.request.urlopen(pedido, timeout=timeout) as resp:
        bruto = resp.read().decode("utf-8")
    if not bruto:
        return {}
    return json.loads(bruto)


def _pedido(
    settings: Settings,
    caminho: str,
    payload: dict[str, Any] | None = None,
    *,
    admin: bool = False,
    timeout: int = 20,
) -> Any:
    dados = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = settings.wuzapi_admin_token
    else:
        headers["token"] = settings.wuzapi_token
    metodo = "POST" if payload is not None else "GET"
    pedido = urllib.request.Request(_url(settings, caminho), data=dados, headers=headers, method=metodo)
    return _json(pedido, timeout)


def preparar(settings: Settings | None = None) -> str:
    settings = settings or load_settings()
    try:
        lista = _pedido(settings, "/admin/users", admin=True)
    except Exception as exc:
        return f"Wuzapi indisponível: {exc}"
    usuarios = (lista or {}).get("data") or []
    if not any(str(item.get("token") or "") == settings.wuzapi_token for item in usuarios):
        try:
            _pedido(
                settings,
                "/admin/users",
                {
                    "name": "sagtrack",
                    "token": settings.wuzapi_token,
                    "webhook": settings.webhook_url,
                    "events": "Message",
                },
                admin=True,
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                return f"Não criei o usuário da Sagtrack na Wuzapi: {exc}"
    for corpo in (
        {"webhookURL": settings.webhook_url, "events": ["Message"]},
        {"webhook": settings.webhook_url, "events": "Message"},
    ):
        try:
            _pedido(settings, "/webhook", corpo)
            break
        except Exception:
            continue
    return ""


def status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    aviso = preparar(settings)
    if aviso:
        return {"configurado": False, "erro": aviso, "grupo": grupo_configurado(settings)}
    try:
        corpo = _pedido(settings, "/session/status")
    except Exception as exc:
        return {"configurado": True, "erro": str(exc), "grupo": grupo_configurado(settings)}
    dados = corpo.get("data") if isinstance(corpo, dict) else corpo
    if not isinstance(dados, dict):
        dados = {}
    logado = bool(dados.get("loggedIn") or dados.get("LoggedIn"))
    socket = bool(dados.get("connected") or dados.get("Connected"))
    grupo = grupo_configurado(settings)
    return {
        "configurado": True,
        "conectado": logado and socket,
        "sessao": dados,
        "grupo": grupo,
        "grupo_nome": grupo_nome(),
        "travado": bool(grupo),
    }


def conectar(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    aviso = preparar(settings)
    if aviso:
        return {"erro": aviso}
    atual = status(settings)
    if atual.get("conectado"):
        return atual
    try:
        _pedido(settings, "/session/connect", {"Subscribe": ["Message"], "Immediate": False})
    except Exception as exc:
        return {"erro": str(exc)}
    return status(settings)


def desconectar(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    aviso = preparar(settings)
    if aviso:
        return {"erro": aviso}
    try:
        _pedido(settings, "/session/disconnect", {})
    except Exception as exc:
        return {"erro": str(exc)}
    return status(settings)


def qr(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    atual = status(settings)
    if atual.get("erro"):
        return atual
    if atual.get("conectado"):
        return {"conectado": True, "grupo": atual.get("grupo") or ""}
    sessao = atual.get("sessao") if isinstance(atual.get("sessao"), dict) else {}
    imagem = str(sessao.get("qrcode") or sessao.get("QRCode") or "")
    if imagem.startswith("data:image"):
        return {"QRCode": imagem, "conectado": False}
    try:
        corpo = _pedido(settings, "/session/qr")
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 404, 500}:
            return {"erro": str(exc)}
        conectar(settings)
        try:
            corpo = _pedido(settings, "/session/qr")
        except Exception as erro:
            return {"erro": str(erro)}
    except Exception as exc:
        return {"erro": str(exc)}
    dados = corpo.get("data") if isinstance(corpo, dict) else corpo
    return dados if isinstance(dados, dict) else {"qr": dados}


def grupos(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or load_settings()
    preparar(settings)
    corpo = _pedido(settings, "/group/list")
    dados = corpo.get("data") if isinstance(corpo, dict) else {}
    lista = []
    if isinstance(dados, dict):
        lista = dados.get("Groups") or dados.get("groups") or []
    elif isinstance(dados, list):
        lista = dados
    saida = []
    for grupo in lista:
        if not isinstance(grupo, dict):
            continue
        jid = str(grupo.get("JID") or grupo.get("jid") or "")
        if not jid.endswith("@g.us"):
            continue
        saida.append(
            {
                "jid": jid,
                "nome": str(grupo.get("Name") or grupo.get("name") or "Grupo sem nome"),
            }
        )
    saida.sort(key=lambda item: item["nome"].lower())
    return saida


def _enviar(settings: Settings, caminho: str, payload: dict[str, Any]) -> None:
    _pedido(settings, caminho, payload, timeout=90)


def enviar_texto(settings: Settings, destino: str, texto: str) -> None:
    _enviar(
        settings,
        "/chat/send/text",
        {"Phone": destino, "Body": texto},
    )


def enviar_pdf(settings: Settings, destino: str, arquivo: bytes, nome: str) -> None:
    import base64

    _enviar(
        settings,
        "/chat/send/document",
        {
            "Phone": destino,
            "Document": "data:application/pdf;base64," + base64.b64encode(arquivo).decode("ascii"),
            "FileName": nome,
        },
    )


def _menu() -> str:
    return (
        "*Relatórios Sagtrack*\n"
        "O turno é sempre das *06:00* até *05:59* do dia seguinte.\n\n"
        "*frota* — PDF da frota, com o mapa completo, todas as rotas e como cada uma foi feita\n"
        "*04AD* ou *rota 04AD* — como está a rota, se foi feita e qual caminhão coletou\n"
        "*SDG-1J76* ou *placa SDG-1J76* — trajeto do caminhão e os polígonos das rotas em que ele coletou\n\n"
        "_Digite o comando neste grupo._"
    )


def mapear_mensagem(body: Any) -> dict[str, str] | None:
    if isinstance(body, dict) and body.get("jsonData"):
        bruto = body.get("jsonData")
        if isinstance(bruto, str):
            try:
                body = json.loads(bruto)
            except json.JSONDecodeError:
                return None
        elif isinstance(bruto, dict):
            body = bruto
    if not isinstance(body, dict) or body.get("type") != "Message":
        return None
    evento = body.get("event") if isinstance(body.get("event"), dict) else {}
    info = evento.get("Info") if isinstance(evento.get("Info"), dict) else {}
    msg = evento.get("Message") if isinstance(evento.get("Message"), dict) else {}
    if not info or not msg:
        return None
    grupo = str(info.get("Chat") or info.get("RemoteJid") or "")
    if not grupo.endswith("@g.us"):
        return None
    texto = (
        msg.get("conversation")
        or (msg.get("extendedTextMessage") or {}).get("text")
        or ((msg.get("listResponseMessage") or {}).get("singleSelectReply") or {}).get("selectedRowId")
        or (msg.get("buttonsResponseMessage") or {}).get("selectedButtonId")
    )
    if not texto:
        return None
    participante = str(info.get("Sender") or info.get("Participant") or "")
    participante = re.sub(r":\d+@", "@", participante)
    return {
        "id": str(info.get("ID") or info.get("Id") or ""),
        "grupo": grupo,
        "participante": participante,
        "nome": str(info.get("PushName") or ""),
        "texto": str(texto),
    }


def _gerar(settings: Settings, tipo: str, consulta: str, dia: date) -> tuple[bytes, str]:
    from .pdfmapa import pdf_frota, pdf_placa, pdf_rota

    if tipo == "frota":
        return pdf_frota(settings.output_dir, dia)
    if tipo == "rota":
        return pdf_rota(settings.output_dir, dia, consulta)
    return pdf_placa(settings.output_dir, dia, consulta)


def processar(body: Any, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    mensagem = mapear_mensagem(body)
    if mensagem is None:
        return
    grupo = grupo_configurado(settings)
    if not grupo or mensagem["grupo"] != grupo:
        return
    liberados = permitidos(settings)
    if liberados and mensagem["participante"] not in liberados:
        return
    pedido = comando_grupo(mensagem["texto"])
    if pedido is None:
        return
    with _LOCK:
        if mensagem["id"] and mensagem["id"] in _VISTOS:
            return
        if mensagem["id"]:
            _VISTOS.add(mensagem["id"])
            if len(_VISTOS) > 400:
                _VISTOS.clear()
                _VISTOS.add(mensagem["id"])
    tipo, consulta = pedido
    print(f"WhatsApp: {tipo} {consulta}".strip(), flush=True)
    try:
        if tipo == "menu":
            enviar_texto(settings, mensagem["grupo"], _menu())
            return
        dia = dia_operacional()
        enviar_texto(settings, mensagem["grupo"], "Montando o PDF do turno. Já envio neste grupo.")
        arquivo, nome = _gerar(settings, tipo, consulta, dia)
        enviar_pdf(settings, mensagem["grupo"], arquivo, nome)
    except Exception as exc:
        try:
            enviar_texto(settings, mensagem["grupo"], f"Não consegui enviar o relatório: {exc}")
        except Exception:
            print(f"WhatsApp: {exc}", flush=True)


def agendar(body: Any) -> None:
    threading.Thread(target=processar, args=(body,), daemon=True).start()
