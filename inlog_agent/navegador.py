from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Edge e Chrome ja vem assinados no Windows. O Chromium do Playwright
# e o que o Controle inteligente de aplicativos costuma bloquear.
_CANAIS = ("msedge", "chrome")


def _exes_windows() -> list[tuple[str, str]]:
    local = os.environ.get("LOCALAPPDATA", "")
    candidatos = (
        ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ("msedge", str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe") if local else ""),
        ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ("chrome", str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe") if local else ""),
    )
    vistos: set[str] = set()
    saida: list[tuple[str, str]] = []
    for canal, caminho in candidatos:
        if not caminho or caminho in vistos or not Path(caminho).is_file():
            continue
        vistos.add(caminho)
        saida.append((canal, caminho))
    return saida


def _tentativas() -> list[dict[str, str]]:
    opcoes: list[dict[str, str]] = [{"channel": canal} for canal in _CANAIS]
    for canal, caminho in _exes_windows():
        opcoes.append({"channel": canal, "executable_path": caminho})
        opcoes.append({"executable_path": caminho})
    return opcoes


def abrir_chromium(pw: Any, **kwargs: Any):
    ultimo: Exception | None = None
    for extra in _tentativas():
        try:
            return pw.chromium.launch(**kwargs, **extra)
        except Exception as exc:
            ultimo = exc
    try:
        return pw.chromium.launch(**kwargs)
    except Exception as exc:
        ultimo = exc
        detalhe = str(ultimo).splitlines()[0] if ultimo else ""
        raise RuntimeError(
            "Nao foi possivel abrir o Edge nem o Chrome. "
            "Instale o Microsoft Edge ou o Google Chrome e tente de novo."
            + (f" ({detalhe})" if detalhe else "")
        ) from ultimo


def abrir_contexto(pw: Any, user_data_dir: str, **kwargs: Any):
    ultimo: Exception | None = None
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    for extra in _tentativas():
        try:
            return pw.chromium.launch_persistent_context(user_data_dir, **kwargs, **extra)
        except Exception as exc:
            ultimo = exc
    try:
        return pw.chromium.launch_persistent_context(user_data_dir, **kwargs)
    except Exception as exc:
        ultimo = exc
    browser = None
    try:
        launch_kw: dict[str, Any] = {"headless": kwargs.get("headless", True)}
        if kwargs.get("args"):
            launch_kw["args"] = kwargs["args"]
        browser = abrir_chromium(pw, **launch_kw)
        contexto = browser.new_context(
            locale=kwargs.get("locale"),
            timezone_id=kwargs.get("timezone_id"),
            viewport=kwargs.get("viewport"),
        )
        contexto._inlog_browser = browser  # type: ignore[attr-defined]
        return contexto
    except Exception as exc:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        ultimo = exc
        detalhe = str(ultimo).splitlines()[0] if ultimo else ""
        raise RuntimeError(
            "Nao foi possivel abrir o Edge nem o Chrome. "
            "Feche as janelas do navegador do robô e tente de novo."
            + (f" ({detalhe})" if detalhe else "")
        ) from ultimo
