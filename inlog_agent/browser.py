from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .config import Settings
from .navegador import abrir_contexto
from .network import NetworkSpy


class BrowserSession:
    def __init__(self, settings: Settings, headed: bool | None = None, perfil: str = "chromium") -> None:
        self.settings = settings
        self.headed = settings.headless is False if headed is None else headed
        self.perfil = perfil
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.spy = NetworkSpy()

    def start(self) -> Page:
        self.playwright = sync_playwright().start()
        self.context = abrir_contexto(
            self.playwright,
            str(self.settings.session_dir / self.perfil),
            headless=not self.headed,
            slow_mo=self.settings.slow_mo_ms or None,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 900},
            args=["--disable-dev-shm-usage"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.settings.timeout_ms)
        self.spy.attach(self.page)
        return self.page

    def screenshot(self, name: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = self.settings.output_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp}-{name}.png"
        if self.page:
            self.page.screenshot(path=str(path), full_page=True)
        return path

    def close(self) -> None:
        extra = getattr(self.context, "_inlog_browser", None) if self.context else None
        if self.context:
            self.context.close()
        if extra:
            try:
                extra.close()
            except Exception:
                pass
        if self.playwright:
            self.playwright.stop()


@contextmanager
def open_session(settings: Settings, headed: bool | None = None, perfil: str = "chromium") -> Iterator[BrowserSession]:
    session = BrowserSession(settings, headed=headed, perfil=perfil)
    try:
        session.start()
        yield session
    finally:
        session.close()
